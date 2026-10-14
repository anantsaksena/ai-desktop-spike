#!/usr/bin/env bash
# Fill the @SHA256_*@ placeholders in packaging/ from a published release.
# The .deb hash comes from the release's own SHA256SUMS and is then re-checked
# against a fresh download, so the package files can't drift from the real asset.
# Usage: packaging/update-checksums.sh 0.1.1
set -euo pipefail
cd "$(dirname "$0")"
VER="${1:?version, e.g. 0.1.1}"
REL="https://github.com/anantsaksena/ai-desktop-spike/releases/download/v$VER"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
sha() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

curl -fsSL -o "$TMP/SHA256SUMS" "$REL/SHA256SUMS"
DEB="ai-desktop-spike_${VER}_amd64.deb"
DEB_SHA="$(awk -v f="$DEB" '$2==f || $2=="*"f {print $1}' "$TMP/SHA256SUMS")"
[[ ${#DEB_SHA} -eq 64 ]] || { echo "no $DEB in SHA256SUMS"; exit 1; }
curl -fsSL -o "$TMP/$DEB" "$REL/$DEB"
[[ "$(sha "$TMP/$DEB")" == "$DEB_SHA" ]] || { echo "downloaded $DEB does not match SHA256SUMS"; exit 1; }
curl -fsSL -o "$TMP/LICENSE" "https://raw.githubusercontent.com/anantsaksena/ai-desktop-spike/v$VER/LICENSE"
LIC_SHA="$(sha "$TMP/LICENSE")"

for f in aur/PKGBUILD flatpak/com.aidesktop.spike.yml snap/snapcraft.yaml; do
  sed -i.bak -e "s/@SHA256_DEB@/$DEB_SHA/g" -e "s/@SHA256_LICENSE@/$LIC_SHA/g" "$f" && rm -f "$f.bak"
done
echo "deb     $DEB_SHA"; echo "LICENSE $LIC_SHA"
grep -rn "@SHA256_" aur flatpak snap && { echo "placeholders left"; exit 1; } || echo "all placeholders filled"
