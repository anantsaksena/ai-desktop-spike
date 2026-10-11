#!/usr/bin/env bash
# Fail if any build script or workflow downloads something outside the
# verified paths. Runs first in ci/linux/build_all.sh (so in CI too).
#
# Allowed download paths, and what verifies each:
#   scripts/lib/fetch_verified.sh   pinned SHA-256 + exact size + format check
#   git clone (fetch_pinned / build_llama_server.sh)
#                                   tag must resolve to a pinned commit; git
#                                   objects are content-addressed
#   pip install --require-hashes --only-binary :all:   PyPI upload-time hashes
#   npm ci                          package-lock.json sha512 integrity
#   cargo --locked                  Cargo.lock checksums
#   apt-get                         signed repository metadata
#   uses: owner/action@<40-hex SHA> immutable commit
#   FROM image@sha256:...           immutable image digest
set -euo pipefail
cd "$(dirname "$0")/.."
fail=0
bad() { echo "AUDIT FAIL: $*" >&2; fail=1; }

FILES=$(find . -name '*.sh' \
  -not -path './node_modules/*' -not -path './llama.cpp/*' -not -path './.tools/*' \
  -not -path './python-backend/venv/*' -not -path './python-backend/sqlcipher/*' \
  -not -path './python-backend/build_sqlcipher3/*' -not -path './src-tauri/target/*' \
  | sed 's|^\./||')
FILES="$FILES ci/linux/Dockerfile .github/workflows/build.yml"

# Strip comments, then look for network fetches.
code() { sed -E 's/(^|[[:space:]])#.*$//' "$1"; }

for f in $FILES; do
  [[ -f "$f" ]] || continue
  [[ "$f" == "scripts/lib/fetch_verified.sh" || "$f" == "ci/audit_downloads.sh" ]] && continue
  c="$(code "$f")"
  # curl/wget to anything but the local sidecars
  if grep -nE '\b(curl|wget)\b[^|]*https?://' <<<"$c" | grep -vE 'https?://(127\.0\.0\.1|localhost)' ; then
    bad "$f: direct curl/wget download; use fetch_verified"
  fi
  grep -nE '\|[[:space:]]*(sudo[[:space:]]+(-E[[:space:]]+)?)?(ba)?sh\b' <<<"$c" && bad "$f: piping a download into a shell"
  if grep -nE '\bgit clone\b' <<<"$c" >/dev/null; then
    grep -qE 'rev-parse HEAD' <<<"$c" || bad "$f: git clone without pinned-commit verification"
  fi
  grep -nE '\bpip3? install\b' <<<"$c" | grep -vE -- '--require-hashes|--no-deps [^ ]*\.whl|--no-deps build_sqlcipher3/wheels' \
    && bad "$f: pip install without --require-hashes"
  grep -nE '\bnpm (install|i)\b' <<<"$c" && bad "$f: npm install (use npm ci)"
  grep -nE '(^|[[:space:];&|(])npx[[:space:]]+' <<<"$c" | grep -vE 'npx (--no-install )?tauri' && bad "$f: npx may download packages"
done

# Workflow actions pinned to full commit SHAs
grep -nE '^\s*-?\s*uses:' .github/workflows/build.yml | grep -vE '@[0-9a-f]{40}(\s|$)' \
  && bad "workflow action not pinned to a commit SHA"
# Base image pinned by digest
grep -E '^FROM ' ci/linux/Dockerfile | grep -vq '@sha256:' && bad "Dockerfile FROM without @sha256 digest"

if [[ "$fail" == 0 ]]; then echo "== download audit: PASS"; else exit 1; fi
