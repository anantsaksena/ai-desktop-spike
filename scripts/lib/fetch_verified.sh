# shellcheck shell=bash
# The ONLY place build scripts may download files (ci/audit_downloads.sh
# enforces this).
#
# Lesson from the model file: the original pin was the SHA-256 of whatever
# arrived, which happened to be a 29-byte HTTP 401 page. A hash only proves the
# file hasn't changed; it does not prove it was ever right. So every download
# must pass three independent checks:
#   1. SHA-256 pinned in the calling script, taken from an independent source
#      where one exists (publisher's SHASUMS file, GitHub release digest,
#      Hugging Face LFS metadata), not computed from our own download.
#   2. Exact expected size (or a minimum, where noted).
#   3. A content/format check for the declared kind: magic bytes, header fields,
#      target CPU architecture, and no HTML error page.
#
# Usage: source scripts/lib/fetch_verified.sh
#        fetch_verified URL DEST SHA256 SIZE KIND
#   SIZE  exact byte count, or "min:N" for a lower bound
#   KIND  gguf | elf-x86_64 | elf-aarch64 | appimage-x86_64 | appimage-aarch64
#         | tar.xz | tar.gz | pdf
# An existing DEST that passes all checks is reused. Anything that fails is
# deleted, never left in place.

_fv_sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
_fv_size() { wc -c < "$1" | tr -d ' '; }
_fv_hex() { od -An -tx1 -j "$2" -N "$3" "$1" | tr -d ' \n'; }

# Content/format check only. Prints a reason on failure.
verify_content() {
  local f="$1" kind="$2"
  local head; head="$(_fv_hex "$f" 0 16)"
  # Error pages from CDNs and Git hosts are HTML or JSON, whatever the URL says.
  case "$(head -c 64 "$f" | LC_ALL=C tr -d '\000\n' | LC_ALL=C tr '[:upper:]' '[:lower:]')" in
    *"<!doctype"*|*"<html"*|*"{\"error"*|*"{\"message"*|"invalid username"*|"not found"*|"entry not found"*)
      echo "looks like an HTML/JSON error page, not a $kind"; return 1;;
  esac
  case "$kind" in
    gguf)
      # "GGUF" + uint32 LE version (2 or 3 are the published versions).
      [[ "${head:0:8}" == "47475546" ]] || { echo "missing GGUF magic"; return 1; }
      [[ "${head:8:8}" == "02000000" || "${head:8:8}" == "03000000" ]] \
        || { echo "unexpected GGUF version bytes ${head:8:8}"; return 1; } ;;
    elf-*|appimage-*)
      [[ "${head:0:8}" == "7f454c46" ]] || { echo "missing ELF magic"; return 1; }
      [[ "${head:8:2}" == "02" ]] || { echo "not a 64-bit ELF"; return 1; }
      local machine; machine="$(_fv_hex "$f" 18 2)"
      case "${kind#*-}" in
        x86_64)  [[ "$machine" == "3e00" ]] || { echo "ELF machine $machine is not x86_64"; return 1; } ;;
        aarch64) [[ "$machine" == "b700" ]] || { echo "ELF machine $machine is not aarch64"; return 1; } ;;
        *) echo "unknown arch in kind $kind"; return 1 ;;
      esac
      if [[ "$kind" == appimage-* ]]; then
        # AppImage type 2 marks itself with "AI\x02" at offset 8.
        [[ "${head:16:6}" == "414902" ]] || { echo "missing AppImage type-2 magic"; return 1; }
      fi ;;
    tar.xz)
      [[ "${head:0:12}" == "fd377a585a00" ]] || { echo "missing xz magic"; return 1; }
      xz -t "$f" 2>/dev/null || { echo "xz integrity test failed"; return 1; } ;;
    tar.gz)
      [[ "${head:0:4}" == "1f8b" ]] || { echo "missing gzip magic"; return 1; }
      gzip -t "$f" 2>/dev/null || { echo "gzip integrity test failed"; return 1; } ;;
    pdf)
      [[ "$(head -c 5 "$f")" == "%PDF-" ]] || { echo "missing %PDF- header"; return 1; }
      tail -c 2048 "$f" | grep -q '%%EOF' || { echo "no %%EOF trailer (truncated?)"; return 1; } ;;
    *) echo "unknown kind $kind"; return 1 ;;
  esac
}

# All three checks. Prints a reason on failure.
verify_file() {
  local f="$1" sha="$2" size="$3" kind="$4" actual
  [[ -f "$f" ]] || { echo "missing"; return 1; }
  actual="$(_fv_size "$f")"
  if [[ "$size" == min:* ]]; then
    (( actual >= ${size#min:} )) || { echo "size $actual < ${size#min:}"; return 1; }
  else
    [[ "$actual" == "$size" ]] || { echo "size $actual != expected $size"; return 1; }
  fi
  verify_content "$f" "$kind" || return 1
  actual="$(_fv_sha256 "$f")"
  [[ "$actual" == "$sha" ]] || { echo "sha256 $actual != pinned $sha"; return 1; }
}

fetch_verified() {
  local url="$1" dest="$2" sha="$3" size="$4" kind="$5" why
  if why="$(verify_file "$dest" "$sha" "$size" "$kind")"; then
    echo "verified (cached): $dest"; return 0
  fi
  rm -f "$dest" "$dest.part"
  mkdir -p "$(dirname "$dest")"
  echo "downloading: $url"
  curl -fL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 2 -o "$dest.part" "$url"
  if ! why="$(verify_file "$dest.part" "$sha" "$size" "$kind")"; then
    echo "FATAL: $url failed verification: $why" >&2
    rm -f "$dest.part"; return 1
  fi
  mv "$dest.part" "$dest"
  echo "verified: $dest ($kind, $(_fv_size "$dest") bytes)"
}
