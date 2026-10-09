#!/usr/bin/env bash
# Fetch the bundled placeholder model and verify it.
#
# History: the original spike downloaded a URL that returned an HTTP 401 error
# page, saved it as tiny-model.gguf, and nobody noticed: llama-server simply
# failed to start. Hence three independent checks below:
#   1. URL pinned to a repository COMMIT (not `main`)
#   2. SHA-256 must match (taken from Hugging Face's LFS metadata)
#   3. File must start with the GGUF magic bytes and be > 1 MB
set -euo pipefail
cd "$(dirname "$0")/.."

# llama.cpp's own CI test model: TinyLlama "stories15M", Q4_0, llama architecture.
REPO_COMMIT="499bc8821c6b12b4e53c5bffcb21ec206f212d81"
URL="https://huggingface.co/ggml-org/models/resolve/$REPO_COMMIT/tinyllamas/stories15M-q4_0.gguf"
SHA256="66967fbece6dbe97886593fdbb73589584927e29119ec31f08090732d1861739"
OUT="src-tauri/tiny-model.gguf"

sum() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
valid() {
  [[ -f "$1" ]] && [[ "$(sum "$1")" == "$SHA256" ]] \
    && [[ "$(head -c 4 "$1")" == "GGUF" ]] && [[ "$(wc -c < "$1")" -gt 1000000 ]]
}

if valid "$OUT"; then echo "model present and valid"; exit 0; fi
rm -f "$OUT"
curl -fL --retry 3 -o "$OUT.part" "$URL"
if ! valid "$OUT.part"; then
  echo "FATAL: downloaded model failed validation (size $(wc -c < "$OUT.part"), magic '$(head -c 4 "$OUT.part")')" >&2
  rm -f "$OUT.part"; exit 1
fi
mv "$OUT.part" "$OUT"
echo "model fetched and valid ($(wc -c < "$OUT") bytes)"
