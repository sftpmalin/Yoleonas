#!/bin/bash
set -Eeuo pipefail

# A placer dans le meme dossier que system.sh.
# Tous les chemins restent relatifs a ce dossier.

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

PYTHON="./.venv/bin/python"
REQUIREMENTS="./requirements.txt"
WHEELHOUSE="../offline/wheels"
DEB_REPO="../offline/debs"
APT_MANIFEST_DIR="../offline/manifests"
APT_REQUESTED="$APT_MANIFEST_DIR/apt-requested.txt"
APT_RESOLVED="$APT_MANIFEST_DIR/apt-resolved.txt"
APT_AVAILABLE="$APT_MANIFEST_DIR/apt-available.txt"
APT_MISSING="$APT_MANIFEST_DIR/apt-missing.txt"
APT_REPO_SHA="$APT_MANIFEST_DIR/apt-repo.sha256"
SYSTEM="./system.sh"
INDEX_URL="${INDEX_URL:-https://pypi.org/simple}"
APT_FORCE_DOWNLOAD="${APT_FORCE_DOWNLOAD:-0}"

die() {
    echo "ERREUR : $*" >&2
    exit 1
}

[ -x "$PYTHON" ] || die "venv introuvable : ./.venv"
[ -f "$REQUIREMENTS" ] || die "requirements.txt introuvable"
[ -f "$SYSTEM" ] || die "system.sh introuvable"

ensure_apt_requested_package() {
    local package="$1"

    mkdir -p "$APT_MANIFEST_DIR"
    touch "$APT_REQUESTED"

    if ! grep -Fxq "$package" "$APT_REQUESTED" 2>/dev/null; then
        echo "Ajout paquet Debian offline : $package"
        printf '%s\n' "$package" >> "$APT_REQUESTED"
        LC_ALL=C sort -u -o "$APT_REQUESTED" "$APT_REQUESTED"
    fi
}

update_apt_offline_repo() {
    local tmp_resolved tmp_available tmp_missing tmp_download_missing package download_failed
    local candidate download_available download_missing
    local total_resolved total_available total_missing
    local -a apt_requested_packages

    if ! command -v apt-get >/dev/null 2>&1 || ! command -v apt-cache >/dev/null 2>&1; then
        echo "APT absent : mise a jour des .deb offline ignoree."
        return 0
    fi

    ensure_apt_requested_package "putty-tools"

    mapfile -t apt_requested_packages < <(
        sed -E 's/[[:space:]]+#.*$//' "$APT_REQUESTED" |
            sed -E 's/^[[:space:]]+|[[:space:]]+$//g' |
            grep -Ev '^(#|$)' |
            LC_ALL=C sort -u
    )

    if [ "${#apt_requested_packages[@]}" -eq 0 ]; then
        echo "Aucun paquet Debian demande dans $APT_REQUESTED."
        return 0
    fi

    echo
    echo "Verification des paquets Debian offline..."
    echo "Paquets demandes : ${#apt_requested_packages[@]}"
    LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get update

    mkdir -p "$APT_MANIFEST_DIR"
    tmp_resolved="$APT_MANIFEST_DIR/.apt-resolved.tmp"
    tmp_available="$APT_MANIFEST_DIR/.apt-available.tmp"
    tmp_missing="$APT_MANIFEST_DIR/.apt-missing.tmp"
    tmp_download_missing="$APT_MANIFEST_DIR/.apt-download-missing.tmp"
    rm -f "$tmp_resolved" "$tmp_available" "$tmp_missing" "$tmp_download_missing"

    echo "Resolution des dependances APT..."
    {
        printf '%s\n' "${apt_requested_packages[@]}"
        { LC_ALL=C apt-cache depends --recurse \
            --no-recommends \
            --no-suggests \
            --no-conflicts \
            --no-breaks \
            --no-replaces \
            --no-enhances \
            "${apt_requested_packages[@]}" || true; } |
            awk '
                /^[[:space:]]*$/ { next }
                /^[[:space:]]*(Pre)?Depends:[[:space:]]*/ {
                    sub(/^[[:space:]]*(Pre)?Depends:[[:space:]]*/, "")
                    gsub(/[<>|].*/, "")
                    gsub(/[[:space:]].*/, "")
                    if ($0 != "") print $0
                    next
                }
                /^[A-Za-z0-9][A-Za-z0-9+.-]*(:[A-Za-z0-9.-]+)?$/ { print $1 }
            '
    } |
        sed -E 's/:any$//' |
        grep -E '^[A-Za-z0-9][A-Za-z0-9+.-]*$' |
        LC_ALL=C sort -u > "$tmp_resolved"

    total_resolved="$(wc -l < "$tmp_resolved" | tr -d ' ')"
    echo "Dependances resolues : $total_resolved paquet(s)"
    echo "Verification de la disponibilite des paquets..."

    : > "$tmp_available"
    : > "$tmp_missing"
    while IFS= read -r package; do
        [ -n "$package" ] || continue
        candidate="$(LC_ALL=C apt-cache policy "$package" 2>/dev/null | awk -F': ' '/^[[:space:]]*Candidate:/ {print $2; exit}')"
        if [ -n "$candidate" ] && [ "$candidate" != "(none)" ]; then
            printf '%s\n' "$package" >> "$tmp_available"
        else
            printf '%s\n' "$package" >> "$tmp_missing"
        fi
    done < "$tmp_resolved"

    LC_ALL=C sort -u -o "$tmp_available" "$tmp_available"
    LC_ALL=C sort -u -o "$tmp_missing" "$tmp_missing"

    mv "$tmp_resolved" "$APT_RESOLVED"
    mv "$tmp_available" "$APT_AVAILABLE"
    mv "$tmp_missing" "$APT_MISSING"

    total_available="$(wc -l < "$APT_AVAILABLE" | tr -d ' ')"
    total_missing="$(wc -l < "$APT_MISSING" | tr -d ' ')"
    echo "Disponibles : $total_available paquet(s)"
    if [ "$total_missing" -gt 0 ]; then
        echo "Manquants : $total_missing paquet(s) dans $APT_MISSING"
    fi

    mkdir -p "$DEB_REPO" "$DEB_REPO/partial"
    : > "$tmp_download_missing"
    download_available="../manifests/apt-available.txt"
    download_missing="../manifests/.apt-download-missing.tmp"
    download_failed=0
    if ! (
        cd "$DEB_REPO"
        count=0
        skipped=0
        downloaded=0
        echo "Telechargement / controle des .deb offline..."
        while IFS= read -r package; do
            [ -n "$package" ] || continue
            count=$((count + 1))
            if [ "$APT_FORCE_DOWNLOAD" != "1" ] && compgen -G "${package}_*.deb" >/dev/null; then
                skipped=$((skipped + 1))
                if [ $((count % 50)) -eq 0 ] || [ "$count" -eq "$total_available" ]; then
                    printf '  [%s/%s] deja present : %s\n' "$count" "$total_available" "$package"
                fi
                continue
            fi
            printf '  [%s/%s] telechargement : %s\n' "$count" "$total_available" "$package"
            if ! LC_ALL=C apt-get download "$package"; then
                printf '%s\n' "$package" >> "$download_missing"
                download_failed=1
            else
                downloaded=$((downloaded + 1))
            fi
        done < "$download_available"
        printf 'Telechargement Debian : %s telecharge(s), %s deja present(s).\n' "$downloaded" "$skipped"
        exit "$download_failed"
    ); then
        if [ -s "$tmp_download_missing" ]; then
            cat "$tmp_download_missing" >> "$APT_MISSING"
            LC_ALL=C sort -u -o "$APT_MISSING" "$APT_MISSING"
        fi
        rm -f "$tmp_download_missing"
        die "certains paquets Debian n'ont pas ete telecharges, voir $APT_MISSING"
    fi
    rm -f "$tmp_download_missing"

    LC_ALL=C sort -u -o "$APT_MISSING" "$APT_MISSING"

    if command -v dpkg-scanpackages >/dev/null 2>&1; then
        echo "Generation de l'index APT offline..."
        (
            cd "$DEB_REPO"
            dpkg-scanpackages . > Packages
            gzip -kf Packages
            chmod 0644 Packages Packages.gz
            find . -maxdepth 1 -type f -name '*.deb' -exec chmod 0644 {} +
        )
    else
        echo "ATTENTION : dpkg-scanpackages absent, index APT offline non regenere."
    fi

    if command -v sha256sum >/dev/null 2>&1; then
        echo "Generation des empreintes SHA256 offline..."
        (
            cd "$DEB_REPO"
            find . -maxdepth 1 -type f \( -name '*.deb' -o -name 'Packages' -o -name 'Packages.gz' \) -printf '%P\n' |
                LC_ALL=C sort |
                while IFS= read -r file; do
                    [ -n "$file" ] || continue
                    sha256sum "$file"
                done
        ) > "$APT_REPO_SHA"
        chmod 0644 "$APT_REPO_SHA"
    fi

    printf 'Paquets Debian offline : demandes=%s, resolus=%s, disponibles=%s, manquants=%s\n' \
        "${#apt_requested_packages[@]}" \
        "$(wc -l < "$APT_RESOLVED" | tr -d ' ')" \
        "$(wc -l < "$APT_AVAILABLE" | tr -d ' ')" \
        "$(wc -l < "$APT_MISSING" | tr -d ' ')"
}

update_apt_offline_repo

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

echo
PACKAGES=()
if [ "${#UPDATES[@]}" -eq 0 ]; then
    echo "Tout est a jour cote paquets deja installes."
else
    echo "Mises a jour disponibles :"
    for update in "${UPDATES[@]}"; do
        IFS='|' read -r package old_version new_version <<< "$update"
        PACKAGES+=("$package")
        printf '  %-28s %s -> %s\n' "$package" "$old_version" "$new_version"
    done
fi

mkdir -p "$WHEELHOUSE"

echo
echo "Telechargement des dependances requirements.txt dans le dossier offline..."
"$PYTHON" -m pip --isolated download \
    --prefer-binary \
    --dest "$WHEELHOUSE" \
    --index-url "$INDEX_URL" \
    -r "$REQUIREMENTS"

if [ "${#PACKAGES[@]}" -gt 0 ]; then
    echo
    echo "Telechargement des mises a jour dans le dossier offline..."
"$PYTHON" -m pip --isolated download \
    --only-binary=:all: \
    --dest "$WHEELHOUSE" \
    --index-url "$INDEX_URL" \
    "${PACKAGES[@]}"
fi

echo
echo "Mise a jour du venv depuis le dossier offline..."
"$PYTHON" -m pip install \
    --no-index \
    --find-links "$WHEELHOUSE" \
    --upgrade \
    --upgrade-strategy eager \
    -r "$REQUIREMENTS" \
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
