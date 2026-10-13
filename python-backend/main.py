import argparse
import os
import sys
import tempfile
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

import ingest
import qa
from model_download import DEFAULT_MODEL, ModelDownloader
from storage import KeyError_, Storage

# Dependencies are stdlib-only beyond requirements.lock on purpose: CI builds
# the frozen backend from that hash-locked file, so anything installed ad hoc
# in a local venv silently breaks the shipped binary. Uploads arrive as a raw
# request body (no python-multipart), HTTP calls use urllib (no requests).

APP_ID = "com.aidesktop.spike"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
# Retrieval is keyword-based (see qa.py). The vector table needs a dimension;
# keep it tiny until a dedicated embedding model fills it.
EMBED_DIM = 8

app = FastAPI()

# The UI runs from tauri://localhost (Linux/macOS) or http://tauri.localhost
# (Windows). The server itself only listens on 127.0.0.1.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def default_data_dir() -> str:
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_ID)


class Config:
    data_dir = default_data_dir()
    model_path: str | None = None

    @classmethod
    def resolved_model_path(cls) -> str:
        return cls.model_path or os.path.join(cls.data_dir, "models", DEFAULT_MODEL.filename)


class NoEmbedder:
    """Placeholder vectors; retrieval uses FTS5 keyword search (qa.py)."""
    dim = EMBED_DIM

    def embed(self, texts):
        return [[0.0] * EMBED_DIM for _ in texts]


# Opened lazily on first use, so --selftest-* never touches the user's library.
_storage: Storage | None = None
_storage_lock = threading.Lock()   # one SQLCipher connection, serialized access
_downloader: ModelDownloader | None = None


def storage() -> Storage:
    global _storage
    if _storage is None:
        lib_dir = os.path.join(Config.data_dir, "library")
        os.makedirs(lib_dir, mode=0o700, exist_ok=True)
        try:
            _storage = Storage(os.path.join(lib_dir, "library.db"), embedding_dim=EMBED_DIM)
        except KeyError_ as e:
            raise HTTPException(500, str(e))
    return _storage


def downloader() -> ModelDownloader:
    global _downloader
    if _downloader is None:
        _downloader = ModelDownloader(Config.resolved_model_path())
    return _downloader


@app.get("/health")
def health():
    return {"status": "backend OK"}


@app.get("/model/status")
def model_status():
    return downloader().status()


@app.post("/model/download")
def model_download():
    return downloader().start()


@app.get("/llm/health")
def llm_health():
    return {"ready": qa.llm_healthy()}


def _ingest(path: str):
    with _storage_lock:
        return ingest.ingest_pdf(path, storage(), NoEmbedder())


@app.post("/upload")
async def upload_pdf(request: Request, filename: str = "document.pdf"):
    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (limit 200 MB).")
    if b"%PDF-" not in body[:1024]:
        raise HTTPException(400, "That file is not a PDF.")
    name = os.path.basename(filename.replace("\\", "/")) or "document.pdf"
    # Temp copy only for parsing; text and index go into the encrypted DB.
    with tempfile.TemporaryDirectory(prefix="aidesk-upload-") as d:
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(body)
        report = await run_in_threadpool(_ingest, path)
    return {
        "status": report.status,
        "document_id": report.document_id,
        "page_count": report.page_count,
        "chunk_count": report.chunk_count,
        "message": report.message,
        "filename": name,
    }


class AskRequest(BaseModel):
    document_id: int
    question: str = Field(min_length=1, max_length=2000)


@app.post("/ask")
def ask_question(req: AskRequest):
    with _storage_lock:
        passages = qa.retrieve(storage(), req.document_id, req.question)
    if not passages:
        return {"answer": "This document has no searchable text (it may be a scan that needs OCR).",
                "citations": []}
    if not qa.llm_healthy():
        raise HTTPException(503, "The AI model isn't running yet. Finish the model download, "
                                 "or wait a few seconds while it loads.")
    try:
        answer = qa.generate(qa.build_messages(req.question, passages))
    except Exception as e:
        raise HTTPException(502, f"The local model failed to answer ({type(e).__name__}: {e}).")
    return {"answer": answer, "citations": qa.citations_for(answer, passages)}



def _bundle_dir() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def run_full_selftest() -> int:
    """Storage/crypto self-test + the pytest suite, run INSIDE this binary."""
    import selftest_storage
    rc = selftest_storage.run()
    import pytest
    tests = os.path.join(_bundle_dir(), "tests")
    # -p no:cacheprovider: don't write .pytest_cache into the bundle dir.
    rc_tests = pytest.main([tests, "-q", "-p", "no:cacheprovider", "--rootdir", tests])
    print(f"SELFTEST storage_rc={rc} pytest_rc={int(rc_tests)}")
    return 0 if rc == 0 and int(rc_tests) == 0 else 1


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_llama_server(pid: int) -> bool:
    """Only ever signal our own llama-server, never a recycled PID."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"llama-server" in f.read()
    except OSError:
        pass
    try:  # macOS: no /proc
        import subprocess
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
        return "llama-server" in out
    except Exception:
        return False


def _pids_from_file(path: str | None) -> list[int]:
    if not path:
        return []
    try:
        with open(path) as f:
            return [int(x) for x in f.read().split() if x.isdigit()]
    except OSError:
        return []


def start_parent_watchdog(parent_pid: int, also_kill: list[int], pid_file: str | None = None) -> None:
    """Exit if the desktop app dies without shutting us down (crash, SIGKILL).
    PyInstaller one-file runs us as a child of a bootloader, so a SIGKILL to
    the bootloader would otherwise orphan this process and keep port 8000
    bound. On the way out, also terminate llama-server (`also_kill`, plus any
    PID in `pid_file`: llama-server is started late, after the model download,
    so its PID can't be passed on our command line)."""
    import signal

    def loop():
        while _pid_alive(parent_pid):
            time.sleep(1.0)
        for pid in set(also_kill) | {p for p in _pids_from_file(pid_file) if _is_llama_server(p)}:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        os._exit(0)

    threading.Thread(target=loop, daemon=True, name="parent-watchdog").start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest-storage", action="store_true",
                        help="Run encrypted-storage self-test and exit")
    parser.add_argument("--selftest-full", action="store_true",
                        help="Storage self-test + full pytest suite, then exit")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="PID of the desktop app; exit when it disappears")
    parser.add_argument("--kill-on-exit", type=int, action="append", default=[],
                        help="PID to SIGTERM if the parent dies (llama-server)")
    parser.add_argument("--kill-pid-file", default=None,
                        help="File with llama-server PIDs to SIGTERM if the parent dies")
    parser.add_argument("--data-dir", default=None,
                        help="App data directory (library database, models)")
    parser.add_argument("--model-path", default=None,
                        help="Where the chat model lives / is downloaded to")
    args, _ = parser.parse_known_args()

    if args.selftest_storage:
        import selftest_storage
        sys.exit(selftest_storage.run())
    if args.selftest_full:
        sys.exit(run_full_selftest())

    if args.data_dir:
        Config.data_dir = args.data_dir
    if args.model_path:
        Config.model_path = args.model_path

    if args.parent_pid:
        start_parent_watchdog(args.parent_pid, args.kill_on_exit, args.kill_pid_file)

    # Localhost-only sidecar. Never bind 0.0.0.0.
    uvicorn.run(app, host="127.0.0.1", port=8000)
