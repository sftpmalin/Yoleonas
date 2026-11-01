#!/bin/bash
set -Eeuo pipefail

# YOLEO_SCRIPT_VERSION=2.0.0
# YOLEO_SCRIPT_DATE=2026-07-31

# A placer dans le meme dossier que system.sh.
# Sources de verite uniques :
#   - APT : ../offline/manifests/apt-requested.txt
#   - PIP : ./requirements.txt
# Aucun paquet applicatif n'est ajoute en dur par ce script.

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

PYTHON="${PYTHON:-./.venv/bin/python}"
REQUIREMENTS="${REQUIREMENTS:-./requirements.txt}"
WHEELHOUSE="${WHEELHOUSE:-../offline/wheels}"
DEB_REPO="${DEB_REPO:-../offline/debs}"
APT_MANIFEST_DIR="${APT_MANIFEST_DIR:-../offline/manifests}"
APT_REQUESTED="${APT_REQUESTED:-${APT_MANIFEST_DIR}/apt-requested.txt}"
APT_RESOLVED="${APT_RESOLVED:-${APT_MANIFEST_DIR}/apt-resolved.txt}"
APT_AVAILABLE="${APT_AVAILABLE:-${APT_MANIFEST_DIR}/apt-available.txt}"
APT_MISSING="${APT_MISSING:-${APT_MANIFEST_DIR}/apt-missing.txt}"
APT_REPO_SHA="${APT_REPO_SHA:-${APT_MANIFEST_DIR}/apt-repo.sha256}"
SYSTEM="${SYSTEM:-./system.sh}"
INDEX_URL="${INDEX_URL:-https://pypi.org/simple}"
APT_FORCE_DOWNLOAD="${APT_FORCE_DOWNLOAD:-0}"
APT_BATCH_SIZE="${APT_BATCH_SIZE:-50}"
SHA_JOBS="${SHA_JOBS:-4}"
TMP_DIR=""
APT_REPO_CHANGED=0

# Nettoyage automatique des repertoires temporaires.
cleanup() {
    if [[ -n "${TMP_DIR}" && "${TMP_DIR}" == /tmp/yoleo-update.* ]]; then
        rm -rf -- "${TMP_DIR}"
    fi
}
trap cleanup EXIT

die() {
    echo "ERREUR : $*" >&2
    exit 1
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "commande introuvable : $1"
}

count_lines() {
    awk 'NF {count++} END {print count+0}' "$1"
}

prepare_tmp() {
    TMP_DIR="$(mktemp -d /tmp/yoleo-update.XXXXXX)"
    # Le sous-dossier de telechargement appartient a _apt. Il doit pouvoir
    # traverser ce parent, sans pour autant pouvoir en lister le contenu.
    chmod 0711 "$TMP_DIR"
}

read_manifest_packages() {
    local manifest="$1"
    sed -E 's/[[:space:]]+#.*$//' "$manifest" |
        sed -E 's/^[[:space:]]+|[[:space:]]+$//g' |
        grep -Ev '^(#|$)' |
        LC_ALL=C sort -u
}

# Genere Packages/Packages.gz en une seule lecture de tous les .deb.
generate_apt_index() {
    local output="$1"

    need_cmd dpkg-scanpackages
    mkdir -p "$DEB_REPO"

    echo "Indexation locale des .deb..."
    (
        cd "$DEB_REPO"
        dpkg-scanpackages --multiversion . /dev/null > "$output"
    )
}

# Extrait Package + Version de l'index local. Chaque archive n'est donc
# analysee qu'une fois, au lieu d'etre rescannée pour chaque paquet demandé.

install_apt_index_if_changed() {
    local source_index="$1"
    local current_index="$DEB_REPO/Packages"

    if [[ ! -f "$current_index" ]] || ! cmp -s "$source_index" "$current_index"; then
        cp -f -- "$source_index" "$current_index"
        gzip -n -kf "$current_index"
        chmod 0644 "$current_index" "${current_index}.gz"
        APT_REPO_CHANGED=1
        echo "Index APT local actualise."
    elif [[ ! -f "${current_index}.gz" ]]; then
        gzip -n -kf "$current_index"
        chmod 0644 "${current_index}.gz"
        APT_REPO_CHANGED=1
        echo "Index compresse APT recree."
    else
        echo "Index APT local deja a jour."
    fi
}

index_to_local_versions() {
    local index_file="$1"
    local output="$2"

    awk '
        BEGIN { RS=""; FS="\n"; OFS="\t" }
        {
            package=""; version=""
            for (i=1; i<=NF; i++) {
                if ($i ~ /^Package: /) {
                    package=$i
                    sub(/^Package: /, "", package)
                } else if ($i ~ /^Version: /) {
                    version=$i
                    sub(/^Version: /, "", version)
                }
            }
            if (package != "" && version != "") print package, version
        }
    ' "$index_file" | LC_ALL=C sort -u > "$output"
}

# Verifie apres telechargement que chaque version candidate resolue existe
# vraiment dans le depot local. apt-missing.txt est ainsi un diagnostic reel,
# et non seulement le resultat d'une tentative de telechargement.
verify_final_local_versions() {
    local index_file="$1"
    local candidates_file="$2"
    local missing_file="$3"
    local versions_file="$TMP_DIR/apt-final-local-versions.tsv"
    local package candidate key
    local -A exact_versions=()

    index_to_local_versions "$index_file" "$versions_file"

    while IFS=$'\t' read -r package candidate; do
        [[ -n "$package" && -n "$candidate" ]] || continue
        exact_versions["$package|$candidate"]=1
    done < "$versions_file"

    while IFS=$'\t' read -r package candidate; do
        [[ -n "$package" && -n "$candidate" ]] || continue
        key="$package|$candidate"
        if [[ -z "${exact_versions[$key]+x}" ]]; then
            printf '%s\n' "$package" >> "$missing_file"
        fi
    done < "$candidates_file"

    LC_ALL=C sort -u -o "$missing_file" "$missing_file"
}

# Interroge APT une seule fois pour tous les paquets, au lieu de lancer
# apt-cache policy plus de mille fois.
create_candidate_map() {
    local resolved_file="$1"
    local output="$2"
    local -a packages

    mapfile -t packages < "$resolved_file"
    (( ${#packages[@]} > 0 )) || die "aucune dependance APT resolue"

    LC_ALL=C apt-cache policy "${packages[@]}" |
        awk '
            /^[^[:space:]].*:$/ {
                package=$0
                sub(/:$/, "", package)
                next
            }
            /^[[:space:]]+Candidate:/ {
                candidate=$2
                if (package != "" && candidate != "" && candidate != "(none)") {
                    print package "\t" candidate
                }
            }
        ' | LC_ALL=C sort -u > "$output"
}

resolve_apt_dependencies() {
    local output="$1"
    shift
    local -a requested=("$@")

    echo "Resolution des dependances APT et des recommandations Debian..."
    {
        printf '%s\n' "${requested[@]}"
        {
            LC_ALL=C apt-cache depends --recurse \
                --no-suggests \
                --no-conflicts \
                --no-breaks \
                --no-replaces \
                --no-enhances \
                "${requested[@]}" || true
        } |
            awk '
                /^[[:space:]]*$/ { next }
                /^[[:space:]]*[|]?((Pre)?Depends|Recommends):[[:space:]]*/ {
                    sub(/^[[:space:]]*[|]?((Pre)?Depends|Recommends):[[:space:]]*/, "")
                    gsub(/[<>|].*/, "")
                    gsub(/[[:space:]].*/, "")
                    if ($0 != "") print $0
                    next
                }
                /^[A-Za-z0-9][A-Za-z0-9+.-]*(:[A-Za-z0-9.-]+)?$/ { print $1 }
            '
    } |
        sed -E 's/:[A-Za-z0-9.-]+$//' |
        grep -E '^[A-Za-z0-9][A-Za-z0-9+.-]*$' |
        LC_ALL=C sort -u > "$output"
}

prepare_download_dir() {
    local dir="$1"
    mkdir -p "$dir"

    # apt-get download abandonne normalement les privileges au compte _apt.
    # On lui donne son propre repertoire temporaire pour eviter les avertissements
    # "Download is performed unsandboxed as root".
    if id _apt >/dev/null 2>&1; then
        chown _apt:root "$dir"
        chmod 0755 "$dir"
    else
        chmod 0777 "$dir"
    fi
}

download_apt_batch() {
    local download_dir="$1"
    local failed_file="$2"
    shift 2
    local -a specs=("$@")
    local spec package

    (( ${#specs[@]} > 0 )) || return 0

    if (
        cd "$download_dir"
        LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get download "${specs[@]}"
    ); then
        return 0
    fi

    echo "Un lot a echoue : nouvelle tentative paquet par paquet pour identifier l'erreur." >&2
    for spec in "${specs[@]}"; do
        if ! (
            cd "$download_dir"
            LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get download "$spec"
        ); then
            package="${spec%%=*}"
            printf '%s\n' "$package" >> "$failed_file"
        fi
    done
}

download_missing_debs() {
    local specs_file="$1"
    local failed_file="$2"
    local download_dir="$3"
    local total count=0 batch_no=0
    local spec
    local -a batch=()

    total="$(count_lines "$specs_file")"
    if (( total == 0 )); then
        echo "Depot APT : toutes les versions candidates sont deja presentes."
        return 0
    fi

    prepare_download_dir "$download_dir"
    echo "Telechargement APT : ${total} paquet(s), lots de ${APT_BATCH_SIZE}."

    while IFS= read -r spec; do
        [[ -n "$spec" ]] || continue
        batch+=("$spec")
        count=$((count + 1))

        if (( ${#batch[@]} >= APT_BATCH_SIZE )); then
            batch_no=$((batch_no + 1))
            printf '  Lot %s : %s/%s\n' "$batch_no" "$count" "$total"
            download_apt_batch "$download_dir" "$failed_file" "${batch[@]}"
            batch=()
        fi
    done < "$specs_file"

    if (( ${#batch[@]} > 0 )); then
        batch_no=$((batch_no + 1))
        printf '  Lot %s : %s/%s\n' "$batch_no" "$count" "$total"
        download_apt_batch "$download_dir" "$failed_file" "${batch[@]}"
    fi

    shopt -s nullglob
    local downloaded=("$download_dir"/*.deb)
    shopt -u nullglob

    if (( ${#downloaded[@]} > 0 )); then
        chown root:root "${downloaded[@]}" 2>/dev/null || true
        chmod 0644 "${downloaded[@]}"
        mv -f -- "${downloaded[@]}" "$DEB_REPO/"
        echo "Paquets .deb integres : ${#downloaded[@]}"
    fi
}

generate_repo_sha256() {
    local tmp_sha="$TMP_DIR/apt-repo.sha256.tmp"
    local jobs="$SHA_JOBS"

    command -v sha256sum >/dev/null 2>&1 || {
        echo "ATTENTION : sha256sum absent, empreintes non regenerees."
        return 0
    }

    [[ "$jobs" =~ ^[1-9][0-9]*$ ]] || jobs=1
    echo "Generation SHA256 du depot (${jobs} tache(s) en parallele)..."

    (
        cd "$DEB_REPO"
        find . -maxdepth 1 -type f \
            \( -name '*.deb' -o -name 'Packages' -o -name 'Packages.gz' \) \
            -printf '%P\0' |
            xargs -0 -r -n 1 -P "$jobs" sha256sum |
            LC_ALL=C sort -k2,2
    ) > "$tmp_sha"

    mv "$tmp_sha" "$APT_REPO_SHA"
    chmod 0644 "$APT_REPO_SHA"
}

update_apt_offline_repo() {
    local tmp_resolved="$TMP_DIR/apt-resolved.tmp"
    local tmp_candidates="$TMP_DIR/apt-candidates.tsv"
    local tmp_local_index="$TMP_DIR/Packages.before"
    local tmp_local_versions="$TMP_DIR/apt-local-versions.tsv"
    local tmp_available="$TMP_DIR/apt-available.tmp"
    local tmp_missing="$TMP_DIR/apt-missing.tmp"
    local tmp_specs="$TMP_DIR/apt-download-specs.txt"
    local tmp_failed="$TMP_DIR/apt-download-failed.txt"
    local tmp_final_index="$TMP_DIR/Packages.final"
    local download_dir="$TMP_DIR/downloads"
    local total_resolved total_available total_missing present_count download_count
    local package candidate key
    local -a requested
    local -A candidate_by_package=()
    local -A local_versions=()

    need_cmd apt-get
    need_cmd apt-cache
    need_cmd dpkg-scanpackages

    [[ -s "$APT_REQUESTED" ]] || die "manifeste APT absent ou vide : $APT_REQUESTED"
    mapfile -t requested < <(read_manifest_packages "$APT_REQUESTED")
    (( ${#requested[@]} > 0 )) || die "aucun paquet Debian demande dans $APT_REQUESTED"

    mkdir -p "$APT_MANIFEST_DIR" "$DEB_REPO"

    echo
    echo "=============================================================================="
    echo "Mise a jour du depot APT universel"
    echo "Source de verite : $APT_REQUESTED"
    echo "Paquets demandes : ${#requested[@]}"

    # L'index local est construit avant apt-get update. Le depot reste donc
    # utilisable meme lors de la toute premiere execution ou apres une copie.
    echo "Lecture unique des archives .deb locales..."
    generate_apt_index "$tmp_local_index"
    install_apt_index_if_changed "$tmp_local_index"

    LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get update

    resolve_apt_dependencies "$tmp_resolved" "${requested[@]}"
    total_resolved="$(count_lines "$tmp_resolved")"
    echo "Dependances resolues : $total_resolved"

    echo "Lecture groupee des versions candidates APT..."
    create_candidate_map "$tmp_resolved" "$tmp_candidates"

    : > "$tmp_available"
    : > "$tmp_missing"
    while IFS=$'\t' read -r package candidate; do
        [[ -n "$package" && -n "$candidate" ]] || continue
        candidate_by_package["$package"]="$candidate"
    done < "$tmp_candidates"

    while IFS= read -r package; do
        [[ -n "$package" ]] || continue
        candidate="${candidate_by_package[$package]-}"
        if [[ -n "$candidate" ]]; then
            printf '%s\n' "$package" >> "$tmp_available"
        else
            printf '%s\n' "$package" >> "$tmp_missing"
        fi
    done < "$tmp_resolved"

    LC_ALL=C sort -u -o "$tmp_available" "$tmp_available"
    LC_ALL=C sort -u -o "$tmp_missing" "$tmp_missing"

    index_to_local_versions "$tmp_local_index" "$tmp_local_versions"

    while IFS=$'\t' read -r package candidate; do
        [[ -n "$package" && -n "$candidate" ]] || continue
        local_versions["$package|$candidate"]=1
    done < "$tmp_local_versions"

    : > "$tmp_specs"
    present_count=0
    while IFS= read -r package; do
        [[ -n "$package" ]] || continue
        candidate="${candidate_by_package[$package]}"
        key="$package|$candidate"
        if [[ "$APT_FORCE_DOWNLOAD" != "1" && -n "${local_versions[$key]+x}" ]]; then
            present_count=$((present_count + 1))
        else
            printf '%s=%s\n' "$package" "$candidate" >> "$tmp_specs"
        fi
    done < "$tmp_available"

    total_available="$(count_lines "$tmp_available")"
    total_missing="$(count_lines "$tmp_missing")"
    download_count="$(count_lines "$tmp_specs")"

    echo "Comparaison terminee :"
    echo "  Versions candidates disponibles : $total_available"
    echo "  Versions exactes deja locales    : $present_count"
    echo "  Paquets a telecharger            : $download_count"
    echo "  Paquets sans candidat            : $total_missing"

    : > "$tmp_failed"
    download_missing_debs "$tmp_specs" "$tmp_failed" "$download_dir"

    if [[ -s "$tmp_failed" ]]; then
        cat "$tmp_failed" >> "$tmp_missing"
    fi

    # Une seconde lecture est faite uniquement lorsque de nouveaux .deb ont ete
    # ajoutes. On controle ensuite toutes les versions exactes, recommandations
    # comprises, avant de publier les manifestes.
    if (( download_count > 0 )); then
        generate_apt_index "$tmp_final_index"
        install_apt_index_if_changed "$tmp_final_index"
    else
        cp -f -- "$tmp_local_index" "$tmp_final_index"
    fi

    verify_final_local_versions "$tmp_final_index" "$tmp_candidates" "$tmp_missing"
    LC_ALL=C sort -u -o "$tmp_missing" "$tmp_missing"

    mv "$tmp_resolved" "$APT_RESOLVED"
    mv "$tmp_available" "$APT_AVAILABLE"
    mv "$tmp_missing" "$APT_MISSING"

    find "$DEB_REPO" -maxdepth 1 -type f -name '*.deb' -exec chmod 0644 {} +

    if (( APT_REPO_CHANGED == 1 )) || [[ ! -s "$APT_REPO_SHA" ]]; then
        generate_repo_sha256
    else
        echo "SHA256 APT deja a jour : aucun .deb ni index modifie."
    fi

    total_missing="$(count_lines "$APT_MISSING")"
    printf 'Depot APT universel : demandes=%s, resolus=%s, disponibles=%s, deja_locaux=%s, telecharges=%s, manquants=%s\n' \
        "${#requested[@]}" "$total_resolved" "$total_available" "$present_count" "$download_count" "$total_missing"

    if (( total_missing > 0 )); then
        echo "Paquets Debian encore manquants :" >&2
        sed -E '/^[[:space:]]*($|#)/d; s/^/  - /' "$APT_MISSING" >&2
        die "depot APT offline incomplet"
    fi
}

update_pip_universal_repo() {
    echo
    echo "=============================================================================="
    echo "Mise a jour du depot PIP universel"
    echo "Source de verite : $REQUIREMENTS"

    [[ -x "$PYTHON" ]] || die "venv introuvable : $PYTHON"
    [[ -s "$REQUIREMENTS" ]] || die "requirements.txt absent ou vide : $REQUIREMENTS"
    [[ -f "$SYSTEM" ]] || die "system.sh introuvable : $SYSTEM"

    echo "Verification des versions des dependances directes de requirements.txt..."

    local outdated_json
    outdated_json="$("$PYTHON" -m pip --isolated list \
        --outdated \
        --format=json \
        --index-url "$INDEX_URL" \
        --disable-pip-version-check)" || die "impossible de consulter PyPI"

    OUTDATED_JSON="$outdated_json" "$PYTHON" - "$REQUIREMENTS" <<'PYREQ'
from pathlib import Path
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name
import json
import os
import sys

path = Path(sys.argv[1])
outdated = {
    canonicalize_name(item["name"]): item["latest_version"]
    for item in json.loads(os.environ.get("OUTDATED_JSON", "[]"))
}
lines = []
changes = []

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

    latest = outdated.get(canonicalize_name(req.name))
    specs = list(req.specifier)
    if latest and len(specs) == 1 and specs[0].operator == "==" and "*" not in specs[0].version:
        old = specs[0].version
        extras = "[" + ",".join(sorted(req.extras)) + "]" if req.extras else ""
        marker = f"; {req.marker}" if req.marker is not None else ""
        lines.append(f"{req.name}{extras}=={latest}{marker}")
        if old != latest:
            changes.append((req.name, old, latest))
    else:
        lines.append(raw)

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
if changes:
    print("Mises a jour des dependances directes :")
    for name, old, latest in changes:
        print(f"  {name}: {old} -> {latest}")
else:
    print("Aucune version figee directe a actualiser.")
PYREQ

    mkdir -p "$WHEELHOUSE"

    echo
    echo "Telechargement strict de requirements.txt dans le depot PIP universel..."
    "$PYTHON" -m pip --isolated download \
        --prefer-binary \
        --dest "$WHEELHOUSE" \
        --index-url "$INDEX_URL" \
        -r "$REQUIREMENTS"

    echo
    echo "Mise a jour du venv uniquement depuis requirements.txt..."
    "$PYTHON" -m pip install \
        --no-index \
        --find-links "$WHEELHOUSE" \
        --upgrade \
        --upgrade-strategy eager \
        -r "$REQUIREMENTS"

    "$PYTHON" -m pip check
}

main() {
    [[ "$APT_BATCH_SIZE" =~ ^[1-9][0-9]*$ ]] || die "APT_BATCH_SIZE invalide : $APT_BATCH_SIZE"
    [[ "$SHA_JOBS" =~ ^[1-9][0-9]*$ ]] || die "SHA_JOBS invalide : $SHA_JOBS"

    prepare_tmp
    update_apt_offline_repo
    update_pip_universal_repo

    echo
    echo "Enregistrement des nouveaux fichiers systeme..."
    bash "$SYSTEM" -add

    echo
    echo "Redemarrage du service avec les nouvelles dependances..."
    bash "$SYSTEM" -restart

    echo
    echo "Mise a jour terminee : depots APT/PIP universels et venv a jour."
}

main "$@"
