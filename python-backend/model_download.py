"""One-time download of the chat model, verified before it is ever used.

This is the ONLY network access the app makes, and only when the user clicks
"Download model". Documents and questions never leave the machine.

Same three checks as scripts/lib/fetch_verified.sh (the build-time rule):
  1. URL pinned to a repository COMMIT, not a moving branch.
  2. SHA-256 and size taken from Hugging Face's LFS metadata (independent of
     our own download), checked over the complete file.
  3. GGUF magic + version bytes (an HTML error page can never pass).
A file that fails any check is deleted and never moved into place, so
llama-server only ever sees a verified model.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import ssl
import threading
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    filename: str
    url: str
    sha256: str
    size: int
    license: str


# Qwen2.5-1.5B-Instruct, Q4_K_M. Apache-2.0. Pinned to repo commit 91cad511.
# SHA-256/size from https://huggingface.co/api/models/Qwen/Qwen2.5-1.5B-Instruct-GGUF/tree/main
DEFAULT_MODEL = ModelSpec(
    name="Qwen2.5 1.5B Instruct (Q4_K_M)",
    filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
    url=("https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/"
         "91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf"),
    sha256="6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e",
    size=1117320736,
    license="Apache-2.0",
)

# Frozen builds carry their own OpenSSL, whose compiled-in CA path may not
# match the user's distro. Load the distro bundle explicitly as well.
_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",       # Debian, Ubuntu, Arch, Gentoo
    "/etc/pki/tls/certs/ca-bundle.crt",         # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",                   # openSUSE
    "/etc/ssl/cert.pem",                        # Alpine, macOS
)

_CHUNK = 1024 * 1024


class VerifyError(RuntimeError):
    pass


def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for path in _CA_BUNDLES:
        if os.path.exists(path):
            try:
                ctx.load_verify_locations(cafile=path)
                break
            except (ssl.SSLError, OSError):
                continue
    return ctx


def check_gguf_header(path: str) -> None:
    with open(path, "rb") as f:
        head = f.read(8)
    if head[:4] != b"GGUF":
        raise VerifyError("downloaded file is not a GGUF model (bad magic)")
    if head[4:8] not in (b"\x02\x00\x00\x00", b"\x03\x00\x00\x00"):
        raise VerifyError(f"unexpected GGUF version bytes {head[4:8].hex()}")


def verify_file(path: str, spec: ModelSpec) -> None:
    """Raise VerifyError unless `path` is exactly the pinned model."""
    size = os.path.getsize(path)
    if size != spec.size:
        raise VerifyError(f"size {size} != expected {spec.size}")
    check_gguf_header(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            h.update(block)
    if h.hexdigest() != spec.sha256:
        raise VerifyError("SHA-256 mismatch: the download is corrupt or was tampered with")


class ModelDownloader:
    """Background download with progress. Thread-safe status()."""

    def __init__(self, model_path: str, spec: ModelSpec = DEFAULT_MODEL):
        self.model_path = model_path
        self.spec = spec
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = "missing"
        self._downloaded = 0
        self._error: str | None = None

    @property
    def part_path(self) -> str:
        return self.model_path + ".part"

    def status(self) -> dict:
        with self._lock:
            state = self._state
            if state not in ("downloading", "verifying") and os.path.exists(self.model_path):
                state = "ready"
            return {
                "state": state,             # missing | downloading | verifying | ready | error
                "downloaded": self._downloaded,
                "total": self.spec.size,
                "error": self._error,
                "model": self.spec.name,
                "license": self.spec.license,
                "path": self.model_path,
            }

    def start(self) -> dict:
        with self._lock:
            busy = self._thread is not None and self._thread.is_alive()
            if not busy and not os.path.exists(self.model_path):
                self._state, self._error = "downloading", None
                self._thread = threading.Thread(target=self._run, daemon=True, name="model-download")
                self._thread.start()
        return self.status()

    # -- worker -------------------------------------------------------------

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, "_" + k, v)

    def _run(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
            have = os.path.getsize(self.part_path) if os.path.exists(self.part_path) else 0
            if have > self.spec.size:
                os.remove(self.part_path)
                have = 0
            free = shutil.disk_usage(os.path.dirname(self.model_path)).free
            if free < (self.spec.size - have) + 200 * 1024 * 1024:
                raise VerifyError(f"not enough disk space: need ~{(self.spec.size - have) // 2**20} MB "
                                  f"plus headroom, {free // 2**20} MB free")
            if have < self.spec.size:
                self._fetch(have)
            self._set(state="verifying")
            try:
                verify_file(self.part_path, self.spec)
            except VerifyError:
                os.remove(self.part_path)   # never keep or resume from a bad file
                raise
            os.replace(self.part_path, self.model_path)
            self._set(state="ready", downloaded=self.spec.size)
        except Exception as e:  # surfaced to the UI as-is
            self._set(state="error", error=f"{type(e).__name__}: {e}")

    def _fetch(self, have: int) -> None:
        req = urllib.request.Request(self.spec.url, headers={"User-Agent": "ai-desktop-spike"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        handlers = [urllib.request.HTTPSHandler(context=ssl_context())]
        opener = urllib.request.build_opener(*handlers)
        with opener.open(req, timeout=60) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            mode = "ab" if (have and code == 206) else "wb"
            if mode == "wb":
                have = 0
            self._set(downloaded=have)
            with open(self.part_path, mode) as out:
                while True:
                    block = resp.read(_CHUNK)
                    if not block:
                        break
                    out.write(block)
                    have += len(block)
                    if have > self.spec.size:
                        raise VerifyError("server sent more bytes than the pinned size")
                    self._set(downloaded=have)
