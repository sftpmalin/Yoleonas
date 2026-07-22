#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE_URL="${YOLEONAS_RELEASE_URL:-https://github.com/sftpmalin/Yoleonas/releases/download/latest/install.zip}"
INSTALL_ROOT="${YOLEONAS_DIR:-/opt/yoleonas}"
STATE_DIR="${YOLEONAS_STATE_DIR:-/var/lib/yoleonas-update}"

[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }
command -v curl >/dev/null || { echo "curl est requis." >&2; exit 1; }
command -v unzip >/dev/null || { echo "unzip est requis." >&2; exit 1; }
command -v rsync >/dev/null || { echo "rsync est requis." >&2; exit 1; }

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
curl -fL "$RELEASE_URL" -o "$tmp_dir/install.zip"
new_hash="$(sha256sum "$tmp_dir/install.zip" | awk '{print $1}')"
mkdir -p "$STATE_DIR"

if [ -f "$STATE_DIR/install.sha256" ] && [ "$(cat "$STATE_DIR/install.sha256")" = "$new_hash" ]; then
  echo "Yoleonas est déjà à jour."
  exit 0
fi

unzip -q "$tmp_dir/install.zip" -d "$tmp_dir/package"
[ -d "$tmp_dir/package/system" ] || { echo "Archive invalide : system absent." >&2; exit 1; }

if [ -x "$INSTALL_ROOT/system/system.sh" ]; then
  bash "$INSTALL_ROOT/system/system.sh" -stop || true
fi

for folder in system scripts bin appli install; do
  [ -d "$tmp_dir/package/$folder" ] || continue
  mkdir -p "$INSTALL_ROOT/$folder"
  rsync -a --delete "$tmp_dir/package/$folder/" "$INSTALL_ROOT/$folder/"
done

cd "$INSTALL_ROOT"
bash system/system.sh -add
bash system/system.sh -restart
printf '%s\n' "$new_hash" > "$STATE_DIR/install.sha256"
echo "Mise à jour Yoleonas terminée."
