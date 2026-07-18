#!/bin/bash
set -Eeuo pipefail

# A placer dans le meme dossier que system.sh.
# Tous les chemins restent relatifs a ce dossier.

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

PYTHON="./.venv/bin/python"
REQUIREMENTS="./requirements.txt"
WHEELHOUSE="../offline/wheels"
SYSTEM="./system.sh"
INDEX_URL="${INDEX_URL:-https://pypi.org/simple}"

die() {
    echo "ERREUR : $*" >&2
    exit 1
}

[ -x "$PYTHON" ] || die "venv introuvable : ./.venv"
[ -f "$REQUIREMENTS" ] || die "requirements.txt introuvable"
[ -f "$SYSTEM" ] || die "system.sh introuvable"

echo "Verification des mises a jour Python sur Internet..."

OUTDATED_JSON="$("$PYTHON" -m pip --isolated list \
    --outdated \
    --format=json \
    --index-url "$INDEX_URL" \
    --disable-pip-version-check)" || die "impossible de consulter PyPI"

mapfile -t UPDATES < <(
    printf '%s' "$OUTDATED_JSON" |
        "$PYTHON" -c 'import json, sys
for p in json.load(sys.stdin):
    print("{}|{}|{}".format(p["name"], p["version"], p["latest_version"]))'
)

if [ "${#UPDATES[@]}" -eq 0 ]; then
    echo "Tout est a jour : aucune mise a jour disponible."
    exit 0
fi

echo
echo "Mises a jour disponibles :"
PACKAGES=()
for update in "${UPDATES[@]}"; do
    IFS='|' read -r package old_version new_version <<< "$update"
    PACKAGES+=("$package")
    printf '  %-28s %s -> %s\n' "$package" "$old_version" "$new_version"
done

mkdir -p "$WHEELHOUSE"

echo
echo "Telechargement des nouveaux fichiers .whl dans le dossier offline..."
"$PYTHON" -m pip --isolated download \
    --only-binary=:all: \
    --dest "$WHEELHOUSE" \
    --index-url "$INDEX_URL" \
    "${PACKAGES[@]}"

echo
echo "Mise a jour du venv depuis le dossier offline..."
"$PYTHON" -m pip install \
    --no-index \
    --find-links "$WHEELHOUSE" \
    --upgrade \
    --upgrade-strategy eager \
    "${PACKAGES[@]}"

"$PYTHON" -m pip check

# Actualise uniquement les lignes deja figees avec == dans requirements.txt.
# Cela evite qu'un prochain system.sh -install remette une ancienne version.
"$PYTHON" - "$REQUIREMENTS" <<'PY'
from importlib.metadata import version
from pathlib import Path
from pip._vendor.packaging.requirements import Requirement
import sys

path = Path(sys.argv[1])
lines = []

for raw in path.read_text(encoding="utf-8").splitlines():
    stripped = raw.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        lines.append(raw)
        continue

    try:
        req = Requirement(stripped)
    except Exception:
        lines.append(raw)
        continue

    specs = list(req.specifier)
    if len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
        lines.append(raw)
        continue

    extras = ""
    if req.extras:
        extras = "[" + ",".join(sorted(req.extras)) + "]"
    marker = f"; {req.marker}" if req.marker is not None else ""
    lines.append(f"{req.name}{extras}=={version(req.name)}{marker}")

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

echo
echo "Enregistrement des nouveaux fichiers dans le systeme..."
bash "$SYSTEM" -add

echo
echo "Redemarrage du service avec les nouvelles dependances..."
bash "$SYSTEM" -restart

echo
echo "Mise a jour terminee : le dossier offline et le venv sont a jour."
