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

# SmolLM2-135M-Instruct, Q4_K_M (105 MB): a real chat model with a chat
# template and an 8K training context. The previous stand-in, stories15M, was
# trained on 128 tokens; llama-server caps each slot at the training context,
# so the real Q&A prompt overflowed it and /ask returned 502 in CI.
REPO_COMMIT="09816acd5d99df7be770d85ea30822623dab342c"
fetch_verified \
  "https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/resolve/$REPO_COMMIT/SmolLM2-135M-Instruct-Q4_K_M.gguf" \
  src-tauri/tiny-model.gguf \
  2e8040ceae7815abe0dcb3540b9995eaa1fa0d2ca9e797d0a635ae4433c68c2d \
  105454432 gguf
