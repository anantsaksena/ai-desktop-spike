#!/usr/bin/env bash
# Fetch the CI stand-in model and verify it. NOT shipped: since v0.1.1 users
# download the real model on first run (python-backend/model_download.py).
# ci/linux/test_bundles.sh seeds this file as the "downloaded" model so the
# lifecycle tests can start llama-server without a 1 GB download.
#
# History: the original spike downloaded a URL that returned an HTTP 401 error
# page, saved it as tiny-model.gguf, and pinned the hash of that page. Nobody
# noticed: llama-server simply failed to start. fetch_verified now requires:
#   1. URL pinned to a repository COMMIT (not `main`)
#   2. SHA-256 from Hugging Face's LFS metadata (independent of our download)
#   3. Exact size, GGUF magic and a valid GGUF version field
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/lib/fetch_verified.sh

# llama.cpp's own CI test model: TinyLlama "stories15M", Q4_0, llama architecture.
REPO_COMMIT="499bc8821c6b12b4e53c5bffcb21ec206f212d81"
fetch_verified \
  "https://huggingface.co/ggml-org/models/resolve/$REPO_COMMIT/tinyllamas/stories15M-q4_0.gguf" \
  src-tauri/tiny-model.gguf \
  66967fbece6dbe97886593fdbb73589584927e29119ec31f08090732d1861739 \
  19077344 gguf
