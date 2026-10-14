# Build & release

## Targets

| Platform | Status | Artifacts | Signing |
|---|---|---|---|
| Linux x86_64 | **Active.** Built + tested in CI | `.deb`, `.AppImage`, `SHA256SUMS` | Optional GPG (free) |
| macOS arm64 | Builds locally; CI job off | `.dmg` | Off until Apple secrets are added |
| Windows | **Not ported** | n/a | Hook documented below |

## One-command builds

```bash
# Linux, locally (Docker is a developer tool here; users never need it)
docker build -t aidesk-linux -f ci/linux/Dockerfile ci/linux
docker run --rm -v "$PWD":/src -w /src -e ISOLATE=1 aidesk-linux \
  bash -c 'ci/linux/build_all.sh && ci/linux/test_bundles.sh out/linux-$(uname -m)-unknown-linux-gnu'

# macOS, locally
scripts/fetch_model.sh && scripts/build_llama_server.sh && python-backend/build_backend.sh
npm ci && CI=true npx tauri build --bundles dmg
```

CI (`.github/workflows/build.yml`) runs the same scripts: `ci/linux/install_deps.sh`,
`ci/linux/build_all.sh`, `ci/linux/test_bundles.sh`.

## What "tested" means for Linux

`ci/linux/test_bundles.sh` runs against the **built** `.deb` and `.AppImage`:

1. **glibc floor:** every ELF must need glibc ≤ 2.35.
2. **Self-test inside the frozen backend** (`aidesk-backend --selftest-full`):
   storage/crypto checks plus the full pytest suite, for both the `.deb` and
   the `.AppImage` copy, in two key setups:
   - headless (no D-Bus): expects `key-file-0600`
   - D-Bus session + gnome-keyring: expects `os-keychain`
3. **Lifecycle:** `apt install` the `.deb` (this also checks its declared
   dependencies), launch the app under Xvfb, wait until both sidecars answer
   `/health`, then:
   - SIGTERM the app: both sidecars gone, ports 8000/8080 freed
   - SIGKILL the app: the backend's parent watchdog cleans up both
   - the same SIGTERM test for the AppImage

The build also runs the self-test once at build time (`build_backend.sh` step 4),
so a broken freeze fails before packaging.

## Why the Linux build runs on Ubuntu 22.04, not ubuntu-latest

A Linux binary needs a glibc at least as new as the one it was built against.
Building on 22.04 (glibc 2.35) gives bundles that run on Ubuntu 22.04+,
Debian 12+, Fedora 36+ and Mint 21+. Building on 24.04 (glibc 2.39) would
silently drop all of those. The `test-ubuntu-latest` CI job runs the
22.04-built bundles on ubuntu-latest, so both are covered.

## Pins

| Component | Pin | Where |
|---|---|---|
| SQLCipher | v4.19.0 @ c4b275a4 | `python-backend/build_backend.sh` |
| sqlcipher3 | 0.6.3 @ 3020374b | `python-backend/build_backend.sh` |
| llama.cpp | b11382 @ 11fe0215 | `scripts/build_llama_server.sh` |
| Python deps | exact versions | `python-backend/requirements.txt` |
| CI test model (not shipped) | bartowski/SmolLM2-135M-Instruct-GGUF @ 09816acd, `SmolLM2-135M-Instruct-Q4_K_M.gguf`, SHA-256 2e8040ce… | `scripts/fetch_model.sh` |
| Python | 3.12 | `build_backend.sh`, workflow |

A tag that moves to a different commit fails the build. `fetch_model.sh` also
checks the GGUF magic bytes and a minimum size. The previous pin was the hash of
a 29-byte HTTP 401 page, so the old "model OK" check proved nothing.

**Not pinned (known gap):** Tauri's AppImage bundler downloads
`linuxdeploy-plugin-appimage` from the `continuous` release at build time.
The `linuxdeploy` binary itself is pinned by Tauri (`linuxdeploy-07333c6`).

## Known portability limits

- **llama-server on x86_64 needs AVX2/FMA/F16C** (CPUs from about 2013 on).
  See `scripts/build_llama_server.sh`.
- **llama-server is built with `LLAMA_OPENSSL=OFF`.** It only serves plain
  HTTP on 127.0.0.1. With the default (ON), it linked the distro's
  `libssl.so.3` dynamically. It now links only libc and libm, and the build
  fails if libssl or libcrypto shows up again.
- **OpenSSL is statically linked** into the sqlcipher3 extension from the build
  host's `libcrypto.a` (Ubuntu 22.04: OpenSSL 3.0.x). Security updates to
  OpenSSL therefore require a rebuild and a new release. The system OpenSSL is
  not used.
- **Expected log noise:** the wrong-key test makes SQLCipher print
  `ERROR CORE sqlcipher_page_cipher: hmac check failed for pgno=1`. This is
  intentional and left enabled, because in the field the same line means a
  wrong key or a tampered or corrupt file.
- **Local verification so far is arm64 only** (Docker on Apple Silicon,
  Ubuntu 22.04 image). The x86_64 build is first exercised by the GitHub
  Actions `linux` job.

---

## Enabling signing later (credentials only, no code changes)

### Linux: GPG (free)
Add the repo secrets `LINUX_GPG_PRIVATE_KEY` (ASCII-armored) and
`LINUX_GPG_PASSPHRASE`. CI then writes a detached `.asc` for each `.deb`,
`.AppImage` and `SHA256SUMS`. Users verify with `gpg --verify`.

### macOS: Apple Developer Program ($99/yr)
1. Set the repo variable `ENABLE_MACOS=true`.
2. Add the secrets `APPLE_CERTIFICATE` (base64 .p12), `APPLE_CERTIFICATE_PASSWORD`,
   `APPLE_SIGNING_IDENTITY`. Tauri then code-signs.
3. Add `APPLE_ID`, `APPLE_PASSWORD` (app-specific password), `APPLE_TEAM_ID`.
   Tauri then notarizes.

> **Known blocker to fix before notarizing:** the backend is a PyInstaller
> **one-file** binary that unpacks native libraries to a temp dir at run time.
> Under the hardened runtime that notarization requires, those unpacked
> libraries aren't signed, and library validation will reject them. Before
> enabling notarization, switch macOS to PyInstaller **one-dir** mode and sign
> every bundled `.so`/`.dylib`. Linux is unaffected.

### Windows: code-signing certificate (paid, or Azure Trusted Signing)
Not ported yet: the backend needs a Windows SQLCipher/OpenSSL static build.
When it's ported, signing is configured in `src-tauri/tauri.windows.conf.json`
(auto-merged by Tauri on Windows) via `bundle.windows.signCommand`, with the
credentials passed in as secrets.
