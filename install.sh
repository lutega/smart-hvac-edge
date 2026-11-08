#!/usr/bin/env bash
# Pemasang 1 baris (jalankan di Pi, mis. dari terminal Raspberry Pi Connect):
#   curl -fsSL https://raw.githubusercontent.com/lutega/smart-hvac-edge/main/install.sh | sudo bash
set -euo pipefail
REPO="${REPO:-lutega/smart-hvac-edge}"
REF="${REF:-main}"
[[ $EUID -eq 0 ]] || { echo "Jalankan dengan sudo" >&2; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Mengunduh $REPO@$REF ..."
for i in 1 2 3 4 5; do
  curl -fsSL --retry 3 "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" -o "$TMP/src.tgz" && break
  echo "  unduh gagal, coba lagi ($i)"; sleep 5
done
tar -xzf "$TMP/src.tgz" -C "$TMP"
SRC="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -1)"
bash "$SRC/edge/update_edge.sh"
