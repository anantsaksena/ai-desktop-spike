#!/usr/bin/env bash
# Build everything for Linux: model, llama-server, backend (with in-binary
# tests), Rust unit tests, frontend, then .deb + .AppImage. Same script locally
# (Docker) and in CI.
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
    --exclude=./src-tauri/bin --exclude=./src-tauri/gen --exclude=./.tools \
    --exclude=./python-backend/venv --exclude=./python-backend/build \
    --exclude=./python-backend/dist --exclude=./python-backend/build_sqlcipher3 \
    --exclude=./python-backend/sqlcipher --exclude='./llama.cpp/build*' \
    --exclude=./testdata/realdocs/pdfs \
    -cf - . | tar -C /work -xf -
  cd /work
else
  cd "$ROOT"
fi
export TARGET_TRIPLE="$TRIPLE"
echo "== build_all: triple=$TRIPLE out=$OUT cwd=$(pwd)"
bash ci/audit_downloads.sh

scripts/fetch_model.sh
scripts/build_llama_server.sh
python-backend/build_backend.sh
npm ci
# --locked everywhere: Cargo.lock (with crate checksums) must not change.
cargo test --locked --manifest-path src-tauri/Cargo.toml --lib
source scripts/seed_appimage_tools.sh
seed_appimage_tools
npx tauri build --bundles deb,appimage -- --locked

# Content check on the result: the AppImage must start with the pinned runtime
# (proves appimagetool used LDAI_RUNTIME_FILE, not a downloaded one). Every byte
# must match except the 16-byte .digest_md5 section, which appimagetool fills
# with the image's MD5 by design. (.upd_info/.sha256_sig/.sig_key stay zero:
# no update info, no AppImage signing.)
APPIMAGE="$(ls src-tauri/target/release/bundle/appimage/*.AppImage)"
RT="$LDAI_RUNTIME_FILE"; RT_SIZE="$(wc -c < "$RT")"
read -r MD5_OFF_HEX MD5_SIZE_HEX < <(readelf -SW "$RT" | awk '{for (i=1;i<=NF;i++) if ($i==".digest_md5") print $(i+3), $(i+4)}')
[[ -n "${MD5_OFF_HEX:-}" ]] || { echo "FATAL: .digest_md5 not found in runtime" >&2; exit 1; }
MD5_OFF=$((16#$MD5_OFF_HEX)); MD5_SIZE=$((16#$MD5_SIZE_HEX))
[[ "$MD5_SIZE" == 16 ]] || { echo "FATAL: unexpected .digest_md5 size $MD5_SIZE" >&2; exit 1; }
DIFFS="$( { cmp -l -n "$RT_SIZE" "$RT" "$APPIMAGE" || true; } | awk -v lo="$MD5_OFF" -v hi="$((MD5_OFF + MD5_SIZE))" '{o=$1-1} o<lo || o>=hi' | wc -l | tr -d ' ')"
if [[ "$DIFFS" == 0 ]]; then
  echo "== AppImage runtime: identical to pinned type2-runtime $SEED_RUNTIME_TAG (except .digest_md5)"
else
  echo "FATAL: AppImage runtime differs from the pinned runtime in $DIFFS bytes outside .digest_md5" >&2; exit 1
fi

mkdir -p "$OUT"
cp src-tauri/target/release/bundle/deb/*.deb "$OUT/"
cp "$APPIMAGE" "$OUT/"
cp python-backend/selftest_frozen.log "$OUT/build-time-selftest.log"
# Test fixture only (NOT shipped): test_bundles.sh seeds it as the downloaded
# model so llama-server starts in the lifecycle tests.
cp src-tauri/tiny-model.gguf "$OUT/test-model.gguf"
( cd "$OUT" && sha256sum *.deb *.AppImage > SHA256SUMS )
ls -la "$OUT"
