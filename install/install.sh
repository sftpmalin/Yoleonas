#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE_URL="${YOLEONAS_RELEASE_URL:-https://github.com/sftpmalin/Yoleonas/releases/download/latest/install.zip}"
INSTALL_ROOT="${YOLEONAS_DIR:-/opt/yoleonas}"

[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }

apt-get update
apt-get install -y curl unzip rsync ca-certificates

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
curl -fL "$RELEASE_URL" -o "$tmp_dir/install.zip"
unzip -q "$tmp_dir/install.zip" -d "$tmp_dir/package"

mkdir -p "$INSTALL_ROOT"
for folder in system scripts bin appli install; do
  [ -d "$tmp_dir/package/$folder" ] || continue
  mkdir -p "$INSTALL_ROOT/$folder"
  rsync -a --delete "$tmp_dir/package/$folder/" "$INSTALL_ROOT/$folder/"
done

cd "$INSTALL_ROOT"
bash system/system.sh -add
bash system/system.sh -install

echo "Yoleonas est installé dans $INSTALL_ROOT"
