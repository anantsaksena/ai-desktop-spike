#!/usr/bin/env bash
# Build everything for Linux: model, llama-server, backend (with in-binary
# tests), frontend, then .deb + .AppImage. Same script locally (Docker) and in CI.
#
#   ISOLATE=1  copy the source tree to /work first, so a bind-mounted macOS
#              checkout (with macOS venv/node_modules/build dirs) isn't touched.
#   OUT        output dir (default: <repo>/out/linux-<triple>)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TRIPLE="$(rustc -vV | sed -n 's/^host: //p')"
OUT="${OUT:-$ROOT/out/linux-$TRIPLE}"

if [[ "${ISOLATE:-0}" == "1" ]]; then
  mkdir -p /work
  # Refresh sources, keep build caches (venv, sqlcipher, cargo target) if /work is a volume.
  [[ "${CLEAN:-0}" == "1" ]] && find /work -mindepth 1 -delete
  tar -C "$ROOT" \
    --exclude=./node_modules --exclude=./out --exclude=./src-tauri/target \
    --exclude=./src-tauri/bin --exclude=./src-tauri/gen \
    --exclude=./python-backend/venv --exclude=./python-backend/build \
    --exclude=./python-backend/dist --exclude=./python-backend/build_sqlcipher3 \
    --exclude=./python-backend/sqlcipher --exclude='./llama.cpp/build*' \
    -cf - . | tar -C /work -xf -
  cd /work
else
  cd "$ROOT"
fi
export TARGET_TRIPLE="$TRIPLE"
echo "== build_all: triple=$TRIPLE out=$OUT cwd=$(pwd)"

scripts/fetch_model.sh
scripts/build_llama_server.sh
python-backend/build_backend.sh
npm ci
npx tauri build --bundles deb,appimage

mkdir -p "$OUT"
cp src-tauri/target/release/bundle/deb/*.deb "$OUT/"
cp src-tauri/target/release/bundle/appimage/*.AppImage "$OUT/"
cp python-backend/selftest_frozen.log "$OUT/build-time-selftest.log"
( cd "$OUT" && sha256sum *.deb *.AppImage > SHA256SUMS )
ls -la "$OUT"
