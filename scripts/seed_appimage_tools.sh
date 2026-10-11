# shellcheck shell=bash
# Pre-seed Tauri's AppImage tool cache with pinned, verified files, so
# `tauri build --bundles appimage` downloads nothing.
#
# Tauri 2.12.1 (crates/tauri-bundler/src/bundle/linux/appimage/linuxdeploy.rs,
# fn prepare_tools) skips a download whenever the file already exists in
# ${XDG_CACHE_HOME:-~/.cache}/tauri:
#   AppRun-<arch>                          (tauri binary-releases "apprun-old")
#   linuxdeploy-07333c6-<arch>.AppImage    (tauri binary-releases)
#   linuxdeploy-plugin-appimage.AppImage   (upstream "continuous": UNPINNED in Tauri)
# The gtk/gstreamer plugin scripts are embedded in the CLI, so they're never
# downloaded.
#
# One more hidden download: appimagetool (inside the plugin) fetches the AppImage
# runtime from AppImage/type2-runtime "continuous" unless given one. We pass a
# pinned runtime via LDAI_RUNTIME_FILE (documented in the plugin's README).
#
# Proof: the build is run once with networking disabled for the bundling step
# (see BUILD.md, "Verifying no hidden downloads").
#
# Usage (from build_all.sh): source scripts/seed_appimage_tools.sh; seed_appimage_tools
# Exports LDAI_RUNTIME_FILE.

# Pins. Changing the Tauri CLI version means re-checking the file names above.
SEED_TAURI_CLI_VERSION="2.12.1"
SEED_LINUXDEPLOY_TAG="linuxdeploy-07333c6"
SEED_PLUGIN_TAG="1-alpha-20250213-1"   # latest fixed (non-continuous) release
SEED_RUNTIME_TAG="20251108"

# Hash sources:
#   AppRun, linuxdeploy, runtime: GitHub release asset digests (API "digest"
#     field, computed by GitHub). Our downloads matched them.
#   plugin-appimage: that release predates GitHub asset digests. The hash was
#     computed from our download after checking the size against the GitHub API
#     and the content (ELF, correct arch, AppImage type-2 magic). Weaker than
#     the others; noted in BUILD.md.
_seed_pins() {
  case "$1" in
    x86_64)
      APPRUN_SHA=f30140a43a0a59e46db21bdefdf749b9e9f2c6946e92afabbacf98b8ae73fb4f;  APPRUN_SIZE=31552
      LD_SHA=36a2d7e274d12e1050d0e9ecfe11d339ed54720b2bec464c286d53f8b07f5c62;      LD_SIZE=19810808
      PLUGIN_SHA=992d502a248e14ab185448ddf6f6e7d25558cb84d4623c354c3af350c25fccb3;  PLUGIN_SIZE=15889136
      RUNTIME_SHA=2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d; RUNTIME_SIZE=944632 ;;
    aarch64)
      APPRUN_SHA=072f17c0895a85c490282fe5395c5007e5fc75da727e553b3b8fb680feb11578;  APPRUN_SIZE=35360
      LD_SHA=556ab80baa98e600aa80f0dcedfb70bca0e1ce7e9f147fb345be3fcc3e91b2b1;      LD_SIZE=19048968
      PLUGIN_SHA=83c292149274965a865dcd44c135cfca8ba28c6b7de3eb628d4b8b5f248af17c;  PLUGIN_SIZE=15405840
      RUNTIME_SHA=00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444; RUNTIME_SIZE=936456 ;;
    *) echo "FATAL: no AppImage tool pins for arch $1" >&2; return 1 ;;
  esac
}

seed_appimage_tools() {
  local root; root="$(pwd)"
  source "$root/scripts/lib/fetch_verified.sh"
  local arch="${TARGET_TRIPLE%%-*}"; arch="${arch:-$(uname -m)}"
  _seed_pins "$arch" || return 1

  local cli; cli="$(npx --no-install tauri --version | awk '{print $2}')"
  if [[ "$cli" != "$SEED_TAURI_CLI_VERSION" ]]; then
    echo "FATAL: tauri CLI is $cli, seed pins are for $SEED_TAURI_CLI_VERSION." >&2
    echo "       Re-check prepare_tools() file names/URLs for the new version, then update scripts/seed_appimage_tools.sh." >&2
    return 1
  fi

  # Verified originals live in our own store. Tauri patches the cached
  # linuxdeploy in place (zeroes the AppImage magic), so the Tauri cache gets a
  # fresh copy from the store on every build.
  local store="${AIDESK_TOOLS_DIR:-$root/.tools}/appimage-$arch"
  local gh=https://github.com
  fetch_verified "$gh/tauri-apps/binary-releases/releases/download/apprun-old/AppRun-$arch" \
    "$store/AppRun-$arch" "$APPRUN_SHA" "$APPRUN_SIZE" "elf-$arch"
  fetch_verified "$gh/tauri-apps/binary-releases/releases/download/$SEED_LINUXDEPLOY_TAG/linuxdeploy-$arch.AppImage" \
    "$store/linuxdeploy-$arch.AppImage" "$LD_SHA" "$LD_SIZE" "appimage-$arch"
  fetch_verified "$gh/linuxdeploy/linuxdeploy-plugin-appimage/releases/download/$SEED_PLUGIN_TAG/linuxdeploy-plugin-appimage-$arch.AppImage" \
    "$store/linuxdeploy-plugin-appimage-$arch.AppImage" "$PLUGIN_SHA" "$PLUGIN_SIZE" "appimage-$arch"
  fetch_verified "$gh/AppImage/type2-runtime/releases/download/$SEED_RUNTIME_TAG/runtime-$arch" \
    "$store/runtime-$arch" "$RUNTIME_SHA" "$RUNTIME_SIZE" "appimage-$arch"

  local cache="${XDG_CACHE_HOME:-$HOME/.cache}/tauri"
  mkdir -p "$cache"
  install -m 0755 "$store/AppRun-$arch" "$cache/AppRun-$arch"
  install -m 0755 "$store/linuxdeploy-$arch.AppImage" "$cache/${SEED_LINUXDEPLOY_TAG}-$arch.AppImage"
  install -m 0755 "$store/linuxdeploy-plugin-appimage-$arch.AppImage" "$cache/linuxdeploy-plugin-appimage.AppImage"
  export LDAI_RUNTIME_FILE="$store/runtime-$arch"
  echo "seeded Tauri AppImage tools in $cache; LDAI_RUNTIME_FILE=$LDAI_RUNTIME_FILE"
}
