#!/usr/bin/env bash
# System packages and toolchains for building AND testing the Linux bundles.
# Used verbatim by ci/linux/Dockerfile (local) and .github/workflows/build.yml (CI),
# so local and CI builds can't drift apart.
# Target base: Ubuntu 22.04 (glibc 2.35). See BUILD.md for why not 24.04.
#
# Download policy (see BUILD.md, "Build-time downloads"):
#   apt packages   - apt verifies signed repository metadata + per-package hashes
#   Node           - official tarball, pinned version, SHA-256 from nodejs.org
#                    SHASUMS256.txt, xz integrity + `node --version` check
#   rustup-init    - pinned version, SHA-256 from static.rust-lang.org, ELF/arch check
#   Rust toolchain - pinned in rust-toolchain.toml; rustup verifies components
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../../scripts/lib/fetch_verified.sh"
SUDO=""; [[ "$(id -u)" -ne 0 ]] && SUDO="sudo"

$SUDO apt-get update
$SUDO apt-get install -y --no-install-recommends \
  ca-certificates curl git file build-essential pkg-config cmake xz-utils \
  libssl-dev zlib1g-dev software-properties-common gpg-agent \
  libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev \
  libsoup-3.0-dev libjavascriptcoregtk-4.1-dev patchelf libfuse2 \
  xvfb xauth dbus dbus-x11 gnome-keyring procps iproute2 binutils

# Python 3.12 from deadsnakes (22.04 ships 3.10). apt-signed. The patch version
# follows deadsnakes (they keep only the latest 3.12.x).
$SUDO add-apt-repository -y ppa:deadsnakes/ppa
$SUDO apt-get update
$SUDO apt-get install -y --no-install-recommends python3.12 python3.12-venv python3.12-dev

ARCH="$(uname -m)"

# ---- Node (pinned). Node 20 reached end-of-life in April 2026. -------------
NODE_VERSION="24.21.0"
case "$ARCH" in
  x86_64)  NODE_ARCH=x64;   NODE_SHA=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6 ;;
  aarch64) NODE_ARCH=arm64; NODE_SHA=6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2 ;;
  *) echo "FATAL: no Node pin for $ARCH" >&2; exit 1 ;;
esac
NODE_DIR="/opt/node-v$NODE_VERSION"
if [[ "$("$NODE_DIR/bin/node" --version 2>/dev/null)" != "v$NODE_VERSION" ]]; then
  TARBALL="/tmp/node-v$NODE_VERSION-linux-$NODE_ARCH.tar.xz"
  # Size isn't published separately; SHASUMS256 + xz integrity + version check cover it.
  fetch_verified "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$NODE_ARCH.tar.xz" \
    "$TARBALL" "$NODE_SHA" "min:20000000" tar.xz
  $SUDO mkdir -p "$NODE_DIR"
  $SUDO tar -xJf "$TARBALL" -C "$NODE_DIR" --strip-components=1
  rm -f "$TARBALL"
fi
[[ "$("$NODE_DIR/bin/node" --version)" == "v$NODE_VERSION" ]] || { echo "FATAL: node version mismatch" >&2; exit 1; }
$SUDO ln -sf "$NODE_DIR/bin/node" "$NODE_DIR/bin/npm" "$NODE_DIR/bin/npx" /usr/local/bin/
# GitHub runners preinstall another Node earlier on PATH; put ours first.
[[ -n "${GITHUB_PATH:-}" ]] && echo "$NODE_DIR/bin" >> "$GITHUB_PATH"

# ---- rustup (pinned) --------------------------------------------------------
# GitHub runners already have rustup; rust-toolchain.toml then selects 1.99.0.
if ! command -v rustup >/dev/null && [[ ! -x "$HOME/.cargo/bin/rustup" ]]; then
  RUSTUP_VERSION="1.29.1"
  case "$ARCH" in
    x86_64)  RUSTUP_SHA=dda7234360b7f578ca8b0ddcb80145646fa61a67c1720a5abc7051b35c9fcb71 ;;
    aarch64) RUSTUP_SHA=15f6e4ce9f583b929c996c91562bad6d4454f3281de858b02cdfdef615fac433 ;;
  esac
  fetch_verified "https://static.rust-lang.org/rustup/archive/$RUSTUP_VERSION/$ARCH-unknown-linux-gnu/rustup-init" \
    /tmp/rustup-init "$RUSTUP_SHA" "min:5000000" "elf-$ARCH"
  chmod +x /tmp/rustup-init
  /tmp/rustup-init -y --profile minimal --default-toolchain none
  rm -f /tmp/rustup-init
fi
# Install the pinned toolchain now (not lazily mid-build), so a failure is
# obvious, and make it the default too (version read from rust-toolchain.toml).
RUSTUP="$(command -v rustup || echo "$HOME/.cargo/bin/rustup")"
RUST_CHANNEL="$(sed -n 's/^channel *= *"\(.*\)"/\1/p' "$HERE/../../rust-toolchain.toml")"
"$RUSTUP" toolchain install "$RUST_CHANNEL" --profile minimal
"$RUSTUP" default "$RUST_CHANNEL"
