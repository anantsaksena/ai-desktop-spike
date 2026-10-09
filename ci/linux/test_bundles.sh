#!/usr/bin/env bash
# Test the BUILT Linux bundles (not the source tree):
#   1. Portability: highest glibc symbol version each ELF needs.
#   2. Full self-test (storage/crypto checks + all pytest tests) inside the
#      frozen backend shipped in the .deb AND the .AppImage, under two key
#      setups: headless (no D-Bus -> key-file-0600) and a D-Bus session with
#      gnome-keyring (-> os-keychain).
#   3. Lifecycle: install the .deb, launch the app under Xvfb, wait for both
#      sidecars to be healthy, then
#        a) SIGTERM the app  -> both sidecars must be gone, ports freed
#        b) SIGKILL the app  -> watchdog must clean up both sidecars
#      Then the same SIGTERM test for the AppImage.
# Usage: ci/linux/test_bundles.sh <dir with .deb and .AppImage>
set -uo pipefail
DIR="$(cd "$1" && pwd)"
DEB="$(ls "$DIR"/*.deb | head -1)"
APPIMAGE="$(ls "$DIR"/*.AppImage | head -1)"
GLIBC_MAX="${GLIBC_MAX:-2.35}"   # Ubuntu 22.04 / Debian 12 = 2.35 / 2.36
SUDO=""; [[ "$(id -u)" -ne 0 ]] && SUDO="sudo"
export APPIMAGE_EXTRACT_AND_RUN=1
FAIL=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAIL=1; }
WORK="$(mktemp -d)"
REPORT="$DIR/test-report.txt"; : > "$REPORT"
log() { echo "$*" | tee -a "$REPORT"; }

# ---------------------------------------------------------------- extract
dpkg-deb -x "$DEB" "$WORK/deb"
( cd "$WORK" && "$APPIMAGE" --appimage-extract >/dev/null )
DEB_BIN="$WORK/deb/usr/bin"; APP_BIN="$WORK/squashfs-root/usr/bin"
MAIN_NAME="$(ls "$DEB_BIN" | grep -v '^aidesk-' | head -1)"
log "deb: $(basename "$DEB")  appimage: $(basename "$APPIMAGE")  main binary: $MAIN_NAME"

# ---------------------------------------------------------------- 1. glibc
log "== 1. glibc requirements (must be <= $GLIBC_MAX)"
for f in "$DEB_BIN"/*; do
  need="$(objdump -T "$f" 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1)"
  log "  $(basename "$f"): ${need:-none}"
  if [[ -n "$need" ]] && [[ "$(printf '%s\n' "${need#GLIBC_}" "$GLIBC_MAX" | sort -V | tail -1)" != "$GLIBC_MAX" ]]; then
    fail "$(basename "$f") needs $need > $GLIBC_MAX"
  fi
done
# The libraries packed INSIDE the PyInstaller binary (libpython, sqlcipher3,
# cryptography, pillow) aren't visible to objdump here. They can't need a newer
# glibc than the build host, though: they were either compiled on it or are
# pip wheels that pip only installs if the host's glibc satisfies them. So the
# build host's glibc (22.04 = 2.35) is the floor for the whole bundle.

# ---------------------------------------------------------------- 2. selftests
selftest() {  # $1=label $2=backend binary $3=mode(headless|keychain) $4=expected key backend
  local home="$WORK/home-$1-$3"; mkdir -p "$home"; local out="$DIR/selftest-$1-$3.log"
  if [[ "$3" == "headless" ]]; then
    env -i HOME="$home" PATH=/usr/bin:/bin TMPDIR=/tmp "$2" --selftest-full > "$out" 2>&1
  else
    env -i HOME="$home" PATH=/usr/bin:/bin TMPDIR=/tmp \
      dbus-run-session -- sh -c '
        echo -n "ci-test-password" | gnome-keyring-daemon --unlock --components=secrets >/dev/null 2>&1
        exec "$0" --selftest-full' "$2" > "$out" 2>&1
  fi
  local rc=$?
  local kb; kb="$(grep -o '"key_backend": "[^"]*"' "$out" | head -1 | cut -d'"' -f4)"
  local summary; summary="$(grep -E '^[0-9]+ passed|failed|SELFTEST' "$out" | tr '\n' ' ')"
  log "  [$1/$3] rc=$rc key_backend=$kb :: $summary"
  [[ $rc -eq 0 ]] || fail "selftest $1/$3 rc=$rc (see $(basename "$out"))"
  [[ "$kb" == "$4" ]] || fail "selftest $1/$3 key backend '$kb', expected '$4'"
}
log "== 2. full self-test inside frozen binaries"
selftest deb      "$DEB_BIN/aidesk-backend" headless key-file-0600
selftest deb      "$DEB_BIN/aidesk-backend" keychain os-keychain
selftest appimage "$APP_BIN/aidesk-backend" headless key-file-0600
selftest appimage "$APP_BIN/aidesk-backend" keychain os-keychain

# ---------------------------------------------------------------- 3. lifecycle
log "== 3. lifecycle"
Xvfb :99 -screen 0 1280x800x24 >/dev/null 2>&1 &
XVFB=$!; export DISPLAY=:99; sleep 2

sidecar_pids() { pgrep -f 'aidesk-backend|aidesk-llama-server' | tr '\n' ' '; }
ports_open() { ss -ltn | grep -cE '127.0.0.1:(8000|8080)\b'; }
wait_healthy() {
  for _ in $(seq 1 90); do
    curl -fs http://127.0.0.1:8000/health >/dev/null && curl -fs http://127.0.0.1:8080/health >/dev/null && return 0
    sleep 1
  done; return 1
}
app_pid() {  # the real app = parent of the backend's PyInstaller bootloader
  local b; b="$(pgrep -f -o 'aidesk-backend')" || return 1
  ps -o ppid= -p "$b" | tr -d ' '
}
lifecycle() {  # $1=label $2=signal $3...=command
  local label="$1" sig="$2"; shift 2
  local home="$WORK/home-life-$label-$sig"; mkdir -p "$home"
  HOME="$home" WEBKIT_DISABLE_COMPOSITING_MODE=1 LIBGL_ALWAYS_SOFTWARE=1 \
    "$@" > "$DIR/app-$label-$sig.log" 2>&1 &
  if ! wait_healthy; then
    fail "$label: sidecars not healthy within 90s"; log "  [$label/$sig] not healthy; pids: $(sidecar_pids)"
    pkill -KILL -f 'aidesk-' ; pkill -KILL -f "$MAIN_NAME"; return
  fi
  local pid; pid="$(app_pid)"
  log "  [$label/$sig] healthy. app pid=$pid sidecars: $(sidecar_pids)"
  kill -"$sig" "$pid"
  local left=""
  for _ in $(seq 1 10); do left="$(sidecar_pids)"; [[ -z "$left" && "$(ports_open)" == "0" ]] && break; sleep 1; done
  if [[ -z "$left" && "$(ports_open)" == "0" ]]; then
    pass "$label/$sig: all sidecars exited, ports freed"; log "  [$label/$sig] clean"
  else
    fail "$label/$sig: leftover sidecars [$left] ports_open=$(ports_open)"; log "  [$label/$sig] LEFTOVER: $left"
    pkill -KILL -f 'aidesk-'
  fi
  pkill -KILL -f "$MAIN_NAME" 2>/dev/null; sleep 1
}

$SUDO apt-get install -y "$DEB" >/dev/null 2>&1 && pass "apt installed .deb with its declared dependencies" \
  || fail "apt could not install .deb"
lifecycle deb TERM "/usr/bin/$MAIN_NAME"
lifecycle deb KILL "/usr/bin/$MAIN_NAME"
lifecycle appimage TERM "$APPIMAGE"

kill "$XVFB" 2>/dev/null
log "== RESULT: $([[ $FAIL -eq 0 ]] && echo PASS || echo FAIL)"
exit $FAIL
