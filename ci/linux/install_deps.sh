#!/usr/bin/env bash
# System packages for building AND testing the Linux bundles.
# Used verbatim by ci/linux/Dockerfile (local) and .github/workflows/build.yml (CI),
# so local and CI builds can't drift apart.
# Target base: Ubuntu 22.04 (glibc 2.35). See BUILD.md for why not 24.04.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
SUDO=""; [[ "$(id -u)" -ne 0 ]] && SUDO="sudo"

$SUDO apt-get update
$SUDO apt-get install -y --no-install-recommends \
  ca-certificates curl git file build-essential pkg-config cmake \
  libssl-dev zlib1g-dev software-properties-common gpg-agent \
  libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev \
  libsoup-3.0-dev libjavascriptcoregtk-4.1-dev patchelf libfuse2 \
  xvfb xauth dbus dbus-x11 gnome-keyring procps iproute2 binutils

# Python 3.12 (22.04 ships 3.10). CI may instead use actions/setup-python and
# set SKIP_PYTHON=1.
if [[ "${SKIP_PYTHON:-0}" != "1" ]]; then
  $SUDO add-apt-repository -y ppa:deadsnakes/ppa
  $SUDO apt-get update
  $SUDO apt-get install -y --no-install-recommends python3.12 python3.12-venv python3.12-dev
fi

# Node 20 (for the Vite frontend) and Rust, if not already present.
if ! command -v node >/dev/null; then
  if [[ -n "$SUDO" ]]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  else
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  fi
  $SUDO apt-get install -y nodejs
fi
if ! command -v cargo >/dev/null; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
fi
