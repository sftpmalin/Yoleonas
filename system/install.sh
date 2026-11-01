#!/usr/bin/env bash
set -Eeuo pipefail

# YOLEO_SCRIPT_VERSION=2.0.0
# YOLEO_SCRIPT_DATE=2026-07-31

# title=Install cache local APT PIP
# description=Installe les paquets Yoleo depuis /yoleo/offline sans Internet.
# icon=logo.png

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

SYSTEM_DIR="$(pwd)"
YOLEO_DIR="$(cd -- "${SYSTEM_DIR}/.." && pwd)"
DEB_REPO="${DEB_REPO:-${YOLEO_DIR}/offline/debs}"
WHEELHOUSE="${WHEELHOUSE:-${YOLEO_DIR}/offline/wheels}"
MANIFEST_DIR="${MANIFEST_DIR:-${YOLEO_DIR}/offline/manifests}"
APT_REQUESTED="${APT_REQUESTED:-${MANIFEST_DIR}/apt-requested.txt}"
APT_MISSING="${APT_MISSING:-${MANIFEST_DIR}/apt-missing.txt}"
REQUIREMENTS="${REQUIREMENTS:-${SYSTEM_DIR}/requirements.txt}"
PYTHON="${PYTHON:-${SYSTEM_DIR}/.venv/bin/python}"
TMP_DIR=""
APT_CHANGED=0
PIP_CHANGED=0

die() {
    echo "ERREUR : $*" >&2
    exit 1
}

cleanup() {
    if [[ -n "${TMP_DIR}" && "${TMP_DIR}" == /tmp/yoleo-install-cache.* ]]; then
        rm -rf -- "${TMP_DIR}"
    fi
}
trap cleanup EXIT

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "commande introuvable : $1"
}

section() {
    echo
    echo "=============================================================================="
    echo "$1"
}

prepare_tmp() {
    TMP_DIR="$(mktemp -d /tmp/yoleo-install-cache.XXXXXX)"
}

prepare_apt_repo() {
    local deb_count

    [[ -d "${DEB_REPO}" ]] || die "dossier .deb introuvable : ${DEB_REPO}"

    shopt -s nullglob
    deb_files=("${DEB_REPO}"/*.deb)
    shopt -u nullglob

    deb_count="${#deb_files[@]}"
    (( deb_count > 0 )) || die "aucun fichier .deb dans ${DEB_REPO}"

    if command -v dpkg-scanpackages >/dev/null 2>&1; then
        echo "APT : generation / verification de l'index local (${deb_count} .deb)..."
        (
            cd "${DEB_REPO}"
            dpkg-scanpackages . /dev/null > Packages
            gzip -kf Packages
            chmod 0644 Packages Packages.gz
            find . -maxdepth 1 -type f -name '*.deb' -exec chmod 0644 {} +
        )
    elif [[ ! -s "${DEB_REPO}/Packages" ]]; then
        die "dpkg-scanpackages absent et ${DEB_REPO}/Packages introuvable"
    fi
}

load_apt_packages() {
    [[ -f "${APT_REQUESTED}" ]] || die "manifeste APT introuvable : ${APT_REQUESTED}"
    [[ -s "${APT_REQUESTED}" ]] || die "manifeste APT vide : ${APT_REQUESTED}"

    if [[ -f "${APT_MISSING}" ]] && grep -Eq '^[[:space:]]*[^#[:space:]]' "${APT_MISSING}"; then
        echo "Paquets APT manquants :" >&2
        sed -E '/^[[:space:]]*($|#)/d; s/^/  - /' "${APT_MISSING}" >&2
        die "depot APT offline incomplet : relance update_pip.sh"
    fi

    mapfile -t APT_PACKAGES < <(
        sed -E 's/[[:space:]]+#.*$//' "${APT_REQUESTED}" |
            sed -E 's/^[[:space:]]+|[[:space:]]+$//g' |
            grep -Ev '^(#|$)' |
            LC_ALL=C sort -u
    )
}

install_apt_cache() {
    local sources_file sourceparts lists_dir sim_file changes
    local -a apt_opts install_args

    section "Installation APT depuis le cache local"

    need_cmd apt-get
    need_cmd dpkg-query
    prepare_apt_repo
    load_apt_packages

    if (( ${#APT_PACKAGES[@]} == 0 )); then
        echo "APT : aucun paquet demande, rien a installer."
        return 0
    fi

    sources_file="${TMP_DIR}/local-apt.list"
    sourceparts="${TMP_DIR}/empty-sources"
    lists_dir="${TMP_DIR}/apt-lists"
    sim_file="${TMP_DIR}/apt-simulation.txt"

    mkdir -p "${sourceparts}" "${lists_dir}/partial"
    printf 'deb [trusted=yes] file:%s ./\n' "${DEB_REPO}" > "${sources_file}"

    apt_opts=(
        -o "Dir::Etc::sourcelist=${sources_file}"
        -o "Dir::Etc::sourceparts=${sourceparts}"
        -o "Dir::State::lists=${lists_dir}"
        -o "APT::Get::List-Cleanup=0"
        -o "APT::Sandbox::User=root"
        -o "Acquire::Retries=0"
        -o "APT::Install-Recommends=true"
    )
    install_args=(install -y)

    echo "APT : source locale unique : ${DEB_REPO}"
    echo "APT : paquets demandes : ${#APT_PACKAGES[@]}"

    DEBIAN_FRONTEND=noninteractive apt-get "${apt_opts[@]}" update

    if ! DEBIAN_FRONTEND=noninteractive apt-get "${apt_opts[@]}" -s \
        "${install_args[@]}" "${APT_PACKAGES[@]}" > "${sim_file}" 2>&1; then
        cat "${sim_file}" >&2
        die "simulation APT impossible avec le cache local"
    fi

    changes="$(grep -c '^Inst ' "${sim_file}" || true)"
    if [[ "${changes}" -eq 0 ]]; then
        echo "APT : tout est deja a jour par rapport au cache local."
    else
        echo "APT : ${changes} paquet(s) a installer ou mettre a jour."
        grep '^Inst ' "${sim_file}" | sed 's/^/  /'

        DEBIAN_FRONTEND=noninteractive apt-get "${apt_opts[@]}" \
            "${install_args[@]}" "${APT_PACKAGES[@]}"
        APT_CHANGED=1
    fi

    # Verification finale obligatoire. Elle prend aussi en compte les paquets
    # recommandes par Debian, afin qu'un paquet comme docker-cli ne puisse plus
    # etre omis silencieusement par docker.io.
    DEBIAN_FRONTEND=noninteractive apt-get "${apt_opts[@]}" check

    local post_file="${TMP_DIR}/apt-post-simulation.txt"
    if ! DEBIAN_FRONTEND=noninteractive apt-get "${apt_opts[@]}" -s \
        "${install_args[@]}" "${APT_PACKAGES[@]}" > "${post_file}" 2>&1; then
        cat "${post_file}" >&2
        die "verification APT finale impossible avec le cache local"
    fi

    if grep -q '^Inst ' "${post_file}"; then
        echo "Paquets encore non installes apres la passe APT :" >&2
        grep '^Inst ' "${post_file}" | sed 's/^/  /' >&2
        die "installation APT incomplete"
    fi

    local package status
    local -a requested_missing=()
    for package in "${APT_PACKAGES[@]}"; do
        status="$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)"
        [[ "$status" == "install ok installed" ]] || requested_missing+=("$package")
    done
    if (( ${#requested_missing[@]} > 0 )); then
        echo "Paquets demandes mais non installes :" >&2
        printf '  - %s\n' "${requested_missing[@]}" >&2
        die "installation APT incomplete par rapport a apt-requested.txt"
    fi

    echo "APT : manifeste complet, dependances et recommandations installees."
}

install_pip_cache() {
    local report dry_log changes
    local -a pip_args

    section "Installation PIP depuis le cache local"

    [[ -x "${PYTHON}" ]] || die "venv introuvable : ${PYTHON}"
    [[ -f "${REQUIREMENTS}" ]] || die "requirements.txt introuvable : ${REQUIREMENTS}"
    [[ -d "${WHEELHOUSE}" ]] || die "dossier wheels introuvable : ${WHEELHOUSE}"

    shopt -s nullglob
    wheel_files=("${WHEELHOUSE}"/*.whl "${WHEELHOUSE}"/*.tar.gz "${WHEELHOUSE}"/*.zip)
    shopt -u nullglob

    (( ${#wheel_files[@]} > 0 )) || die "aucun paquet Python dans ${WHEELHOUSE}"

    report="${TMP_DIR}/pip-report.json"
    dry_log="${TMP_DIR}/pip-dry-run.log"
    pip_args=(
        --no-index
        --find-links "${WHEELHOUSE}"
        --upgrade
        --upgrade-strategy eager
        -r "${REQUIREMENTS}"
    )
    echo "PIP : wheelhouse local : ${WHEELHOUSE}"
    echo "PIP : requirements : ${REQUIREMENTS}"

    if ! "${PYTHON}" -m pip install --dry-run --report "${report}" \
        "${pip_args[@]}" > "${dry_log}" 2>&1; then
        cat "${dry_log}" >&2
        die "simulation PIP impossible avec le cache local"
    fi

    changes="$("${PYTHON}" -c 'import json, sys; print(len(json.load(open(sys.argv[1])).get("install", [])))' "${report}")"
    if [[ "${changes}" -eq 0 ]]; then
        echo "PIP : tout est deja a jour par rapport au cache local."
        "${PYTHON}" -m pip check
        return 0
    fi

    echo "PIP : ${changes} paquet(s) a installer ou mettre a jour."
    "${PYTHON}" -c '
import json, sys
for item in json.load(open(sys.argv[1])).get("install", []):
    meta = item.get("metadata", {})
    name = meta.get("name", "paquet")
    version = meta.get("version", "?")
    print(f"  {name}=={version}")
' "${report}"

    "${PYTHON}" -m pip install "${pip_args[@]}"
    "${PYTHON}" -m pip check
    PIP_CHANGED=1
}

main() {
    if (( EUID != 0 )); then
        die "lance ce script en root pour pouvoir installer les paquets APT"
    fi

    prepare_tmp

    echo "Yoleo : installation du cache local APT/PIP"
    echo "Internet n'est pas utilise par ce script."

    install_apt_cache
    install_pip_cache

    echo
    echo "=============================================================================="
    if (( APT_CHANGED == 0 && PIP_CHANGED == 0 )); then
        echo "Tout est deja installe : rien a installer."
    else
        echo "Installation terminee depuis le cache local."
    fi
}

main "$@"
