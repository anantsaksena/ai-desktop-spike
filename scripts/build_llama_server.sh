#!/usr/bin/env bash
# Build llama.cpp's llama-server as a portable, statically linked sidecar.
#   * Pinned tag + commit.
#   * GGML_NATIVE=OFF: do NOT tune for the build machine's CPU. A native build
#     on a CI runner with AVX-512 would crash with "illegal instruction" on
#     users' older CPUs. ggml picks CPU features at runtime instead.
#   * Static ggml/llama libs, no libcurl, no OpenMP (avoids libgomp at runtime),
#     static libstdc++/libgcc on Linux (avoids GLIBCXX version errors on
#     older distros).
#   * LLAMA_OPENSSL=OFF: the server only listens on 127.0.0.1 over plain HTTP,
#     so TLS isn't needed. Left ON (the default), cpp-httplib links libssl.so.3
#     and libcrypto.so.3 dynamically, which breaks on distros without OpenSSL 3.
set -euo pipefail
cd "$(dirname "$0")/.."

LLAMA_TAG="b11382"
LLAMA_COMMIT="11fe02151f79c41d0d4af7da708755d73b9c0da6"
TARGET_TRIPLE="${TARGET_TRIPLE:-$(rustc -vV | sed -n 's/^host: //p')}"
SRC="${LLAMA_SRC:-llama.cpp}"

if [[ -d "$SRC" ]] && [[ "$(git -C "$SRC" rev-parse HEAD 2>/dev/null)" != "$LLAMA_COMMIT" ]]; then
  echo "$SRC not at pinned commit, re-cloning"; rm -rf "$SRC"
fi
[[ -d "$SRC" ]] || git clone --depth 1 --branch "$LLAMA_TAG" https://github.com/ggml-org/llama.cpp.git "$SRC"
[[ "$(git -C "$SRC" rev-parse HEAD)" == "$LLAMA_COMMIT" ]] || { echo "FATAL: llama.cpp tag moved" >&2; exit 1; }

EXTRA=()
if [[ "$(uname)" == "Linux" ]]; then
  EXTRA+=(-DCMAKE_EXE_LINKER_FLAGS="-static-libstdc++ -static-libgcc")
fi
if [[ "$TARGET_TRIPLE" == x86_64-* ]]; then
  # Explicit instruction set from the single source of truth, which the app
  # also checks at startup (src-tauri/src/cpu.rs). Everything not listed is
  # forced OFF. Runtime CPU variants (GGML_BACKEND_DL + GGML_CPU_ALL_VARIANTS)
  # would lift the AVX2 floor but ship several .so files instead of one static
  # binary; revisit if older CPUs matter.
  for opt in GGML_SSE42 GGML_AVX GGML_AVX2 GGML_BMI2 GGML_FMA GGML_F16C GGML_AVX_VNNI \
             GGML_AVX512 GGML_AVX512_VBMI GGML_AVX512_VNNI GGML_AVX512_BF16 \
             GGML_AMX_TILE GGML_AMX_INT8 GGML_AMX_BF16; do
    if grep -qE "^[a-z0-9.]+[[:space:]]+$opt\$" scripts/llama_x86_features.txt; then
      EXTRA+=("-D$opt=ON")
    else
      EXTRA+=("-D$opt=OFF")
    fi
  done
  echo "== x86_64 instruction set: ${EXTRA[*]}"
fi

BUILD="$SRC/build-$TARGET_TRIPLE"
cmake -S "$SRC" -B "$BUILD" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DGGML_NATIVE=OFF \
  -DGGML_OPENMP=OFF \
  -DLLAMA_CURL=OFF \
  -DLLAMA_OPENSSL=OFF \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON \
  ${EXTRA[@]+"${EXTRA[@]}"}
cmake --build "$BUILD" --target llama-server -j"$(getconf _NPROCESSORS_ONLN)"

BIN="$BUILD/bin/llama-server"
"$BIN" --version 2>&1 | head -2
BAD_DEPS="libstdc\+\+|libgomp|libcurl|libllama|libggml|libssl|libcrypto"
if [[ "$(uname)" == "Linux" ]]; then
  echo "== dynamic deps:"; ldd "$BIN"
  if ldd "$BIN" | grep -qE "$BAD_DEPS"; then
    echo "FATAL: llama-server has unexpected shared deps" >&2; exit 1
  fi
elif [[ "$(uname)" == "Darwin" ]]; then
  echo "== dynamic deps:"; otool -L "$BIN"
  # libc++ is a system library on macOS and is fine; Homebrew paths are not.
  if otool -L "$BIN" | grep -qE "/opt/homebrew|/usr/local|libssl|libcrypto|libomp|libllama|libggml"; then
    echo "FATAL: llama-server has unexpected shared deps" >&2; exit 1
  fi
fi
if [[ "$TARGET_TRIPLE" == x86_64-* ]]; then
  # Content check: the startup CPU check (cpu.rs) only tests the features in
  # llama_x86_features.txt, so the binary must not use anything beyond them.
  # AVX-512 is the realistic leak (zmm registers).
  ZMM="$(objdump -d --no-show-raw-insn "$BIN" | grep -c '%zmm' || true)"
  echo "== AVX-512 (zmm) instructions in llama-server: $ZMM"
  [[ "$ZMM" == "0" ]] || { echo "FATAL: llama-server contains AVX-512 instructions" >&2; exit 1; }
fi
mkdir -p src-tauri/bin
cp "$BIN" "src-tauri/bin/aidesk-llama-server-$TARGET_TRIPLE"
echo "Installed src-tauri/bin/aidesk-llama-server-$TARGET_TRIPLE"
