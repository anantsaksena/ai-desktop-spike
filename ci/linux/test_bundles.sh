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
#   4. CPU gate: with AIDESK_SIMULATE_MISSING_CPU=avx2 the app must start, the
#      backend must be healthy, llama-server must NOT be started, the log must
#      say why, and SIGTERM must still clean up.
# Usage: ci/linux/test_bundles.sh <dir with .deb and .AppImage>
set -uo pipefail
DIR="$(cd "$1" && pwd)"
DEB="$(ls "$DIR"/*.deb 2>/dev/null | head -1)"
APPIMAGE="$(ls "$DIR"/*.AppImage 2>/dev/null | head -1)"
# Without bundles every later step degrades into nonsense; one of them,
# `pkill -f "$MAIN_NAME"` with an empty name, matches EVERY process and kills
# the CI runner itself ("The hosted runner lost communication"). Stop here.
[[ -f "$DEB" && -f "$APPIMAGE" ]] || { echo "FATAL: no .deb/.AppImage in $DIR (did the build step fail?)"; exit 2; }
# Small stand-in for the real model (which users download on first run).
# Seeded into each test HOME so llama-server starts as it would after a download.
TEST_MODEL="$DIR/test-model.gguf"
MODEL_FILE="qwen2.5-1.5b-instruct-q4_k_m.gguf"   # = MODEL_FILE in src-tauri/src/lib.rs
APP_ID="com.aidesktop.spike"
[[ -f "$TEST_MODEL" ]] || { echo "FATAL: $TEST_MODEL missing"; exit 2; }
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
[[ -n "$MAIN_NAME" && -x "$APP_BIN/aidesk-backend" ]] || { echo "FATAL: could not find binaries in the bundles"; exit 2; }
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
    env -i HOME="$home" PATH=/usr/bin:/bin TMPDIR=/tmp timeout 600 "$2" --selftest-full > "$out" 2>&1
  else
    env -i HOME="$home" PATH=/usr/bin:/bin TMPDIR=/tmp \
      timeout 600 dbus-run-session -- sh -c '
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
    curl -fs --max-time 3 http://127.0.0.1:8000/health >/dev/null && curl -fs --max-time 3 http://127.0.0.1:8080/health >/dev/null && return 0
    sleep 1
  done; return 1
}
app_pid() {  # the real app = parent of the backend's PyInstaller bootloader
  local b; b="$(pgrep -f -o 'aidesk-backend')" || return 1
  ps -o ppid= -p "$b" | tr -d ' '
}
seed_model() {  # $1=HOME: put the stand-in model where a finished download would be
  local d="$1/.local/share/$APP_ID/models"; mkdir -p "$d" && cp "$TEST_MODEL" "$d/$MODEL_FILE"
}
make_pdf() {  # $1=path. 2 pages; only page 2 mentions notices + the canary.
  python3 - "$1" <<'PY'
import sys
texts = ["Commercial lease between Harbor Point Properties and Greenleaf Clinic. The term is five years.",
         "Notices must be sent to 900 Commercial Street. Reference CANARYXYZZY7 applies."]
objs, kids, n = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}, [], 3
font = 3 + 2 * len(texts)
for t in texts:
    stream = f"BT /F1 12 Tf 72 720 Td ({t}) Tj ET".encode()
    objs[n] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
               f"<< /Font << /F1 {font} 0 R >> >> /Contents {n + 1} 0 R >>").encode()
    objs[n + 1] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    kids.append(f"{n} 0 R"); n += 2
objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(texts)} >>".encode()
objs[font] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
out, offs = bytearray(b"%PDF-1.4\n"), {}
for i in sorted(objs):
    offs[i] = len(out); out += b"%d 0 obj\n" % i + objs[i] + b"\nendobj\n"
xref, size = len(out), max(objs) + 1
out += b"xref\n0 %d\n0000000000 65535 f \n" % size
for i in range(1, size):
    out += b"%010d 00000 n \n" % offs[i]
out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
open(sys.argv[1], "wb").write(out)
PY
}
json_get() { python3 -c 'import json,sys
try:
    d = json.load(sys.stdin); exec("print(" + sys.argv[1] + ")")
except Exception: print("")' "$1"; }
api_smoke() {  # $1=label $2=HOME. Real HTTP path the UI uses: upload -> ask -> cite.
  local label="$1" home="$2" pdf="$WORK/smoke.pdf" r id page
  make_pdf "$pdf" || { fail "$label/api: could not generate the fixture PDF"; return; }
  r="$(curl -fsS --max-time 10 http://127.0.0.1:8000/model/status)"
  [[ "$(json_get 'd["state"]' <<<"$r")" == ready ]] || fail "$label/api: seeded model not reported ready: $r"
  r="$(curl -fsS --max-time 60 -X POST -H 'Content-Type: application/pdf' --data-binary @"$pdf" \
        "http://127.0.0.1:8000/upload?filename=smoke.pdf")" || { fail "$label/api: upload failed"; return; }
  id="$(json_get 'd["document_id"] if d.get("chunk_count") else ""' <<<"$r")"
  [[ -n "$id" ]] || { fail "$label/api: upload indexed nothing: $r"; return; }
  # No -f: on an HTTP error we want the backend's message in the report.
  local code
  code="$(curl -sS --max-time 240 -o "$WORK/ask.json" -w '%{http_code}' -X POST \
        -H 'Content-Type: application/json' \
        -d "{\"document_id\": $id, \"question\": \"Where must notices be sent?\"}" \
        http://127.0.0.1:8000/ask)"; r="$(cat "$WORK/ask.json" 2>/dev/null)"
  [[ "$code" == 200 ]] || { fail "$label/api: /ask HTTP $code: ${r:0:400}"; log "  [$label/api] /ask HTTP $code: ${r:0:400}"; return; }
  page="$(json_get 'd["citations"][0]["page"]' <<<"$r")"
  if [[ "$page" == 2 ]]; then pass "$label/api: upload -> ask -> answer cites page 2"
  else fail "$label/api: expected first citation on page 2: ${r:0:400}"; fi
  log "  [$label/api] answer (stand-in model, content not checked): $(json_get 'd["answer"][:120]' <<<"$r")"
  # Everything the app wrote lives under HOME. The text must only exist inside SQLCipher.
  local leaks; leaks="$(grep -rlaF CANARYXYZZY7 "$home" 2>/dev/null)"
  if [[ -z "$leaks" ]]; then pass "$label/api: document text not on disk in plaintext"
  else fail "$label/api: plaintext document text found in: $leaks"; fi
}
lifecycle() {  # $1=label $2=signal $3...=command
  local label="$1" sig="$2"; shift 2
  local home="$WORK/home-life-$label-$sig"; mkdir -p "$home"; seed_model "$home"
  HOME="$home" WEBKIT_DISABLE_COMPOSITING_MODE=1 LIBGL_ALWAYS_SOFTWARE=1 \
    "$@" > "$DIR/app-$label-$sig.log" 2>&1 &
  if ! wait_healthy; then
    fail "$label: sidecars not healthy within 90s"; log "  [$label/$sig] not healthy; pids: $(sidecar_pids)"
    pkill -KILL -f 'aidesk-' ; pkill -KILL -f "$MAIN_NAME"; return
  fi
  [[ -s "$home/.local/share/$APP_ID/llama-server.pid" ]] \
    && pass "$label/$sig: llama-server PID file written for the watchdog" \
    || fail "$label/$sig: no llama-server.pid in the app data dir"
  [[ "$label" == deb && "$sig" == TERM ]] && api_smoke "$label" "$home"
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

# ---------------------------------------------------------------- 4. CPU gate
log "== 4. unsupported-CPU gate (simulated missing AVX2)"
cpu_gate() {
  local home="$WORK/home-cpugate"; mkdir -p "$home"; local out="$DIR/app-deb-cpugate.log"
  HOME="$home" WEBKIT_DISABLE_COMPOSITING_MODE=1 LIBGL_ALWAYS_SOFTWARE=1 AIDESK_SIMULATE_MISSING_CPU=avx2 \
    "/usr/bin/$MAIN_NAME" > "$out" 2>&1 &
  local ok=0
  for _ in $(seq 1 60); do curl -fs http://127.0.0.1:8000/health >/dev/null && { ok=1; break; }; sleep 1; done
  [[ $ok == 1 ]] || { fail "cpugate: backend not healthy"; pkill -KILL -f 'aidesk-'; pkill -KILL -f "$MAIN_NAME"; return; }
  sleep 5   # give a (wrongly) started llama-server time to show up
  local pid; pid="$(app_pid)"
  if pgrep -f aidesk-llama-server >/dev/null || ss -ltn | grep -q '127.0.0.1:8080\b'; then
    fail "cpugate: llama-server was started despite the missing CPU feature"
  elif ! grep -q 'unsupported CPU: missing avx2' "$out"; then
    fail "cpugate: no 'unsupported CPU' log line"
  elif ! kill -0 "$pid" 2>/dev/null; then
    fail "cpugate: app exited instead of reporting"
  else
    pass "cpugate: app up, backend healthy, llama-server not started, reason logged"
    log "  [cpugate] $(grep -o 'unsupported CPU: missing [^;]*' "$out" | head -1)"
  fi
  kill -TERM "$pid" 2>/dev/null
  for _ in $(seq 1 10); do [[ -z "$(sidecar_pids)" ]] && break; sleep 1; done
  if [[ -z "$(sidecar_pids)" ]]; then pass "cpugate/TERM: clean"; else fail "cpugate/TERM: leftover $(sidecar_pids)"; pkill -KILL -f 'aidesk-'; fi
  pkill -KILL -f "$MAIN_NAME" 2>/dev/null; sleep 1
}
cpu_gate

kill "$XVFB" 2>/dev/null
log "== RESULT: $([[ $FAIL -eq 0 ]] && echo PASS || echo FAIL)"
exit $FAIL
