#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DOSSIERS=(
  "$SCRIPT_DIR"
  "$SCRIPT_DIR/../bin"
  "$SCRIPT_DIR/../system"
)

echo "================================"
echo " Mise en executable des scripts "
echo "================================"
echo

for dossier in "${DOSSIERS[@]}"; do
  if [ -d "$dossier" ]; then
    echo "[OK] chmod -R +x : $dossier"
    chmod -R +x "$dossier"
  else
    echo "[SKIP] dossier introuvable : $dossier"
  fi
done

echo
echo "Terminé."
