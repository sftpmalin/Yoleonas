#!/bin/bash
set -Eeuo pipefail

# ============================================================
# docker_http_relatif.sh
#
# Autorise Docker à utiliser un ou plusieurs registres HTTP locaux.
#
# Structure attendue :
#   docker_labo/
#   ├── scripts/docker_http_relatif.sh
#   └── conf/http.conf
#
# Le fichier ../conf/http.conf accepte deux formats :
#   1=192.168.1.2:5000
#   2=192.168.1.176:7777
#
# Ancien format encore accepté :
#   192.168.1.2:5000
#
# Lignes vides et commentaires # acceptés.
# ============================================================

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
CONF_DIR="$BASE_DIR/conf"
CONF_FILE="$CONF_DIR/http.conf"

DOCKER_DIR="/etc/docker"
DAEMON_JSON="$DOCKER_DIR/daemon.json"

mkdir -p "$CONF_DIR" "$DOCKER_DIR"

# Crée un fichier de conf exemple si absent.
if [ ! -f "$CONF_FILE" ]; then
    cat > "$CONF_FILE" <<'CONF'
# Registres Docker HTTP autorisés
# Format compatible éditeur key=value :
# 1=IP:PORT ou 1=NOM:PORT
#
# Ancien format encore accepté :
# IP:PORT
1=192.168.1.2:5000
CONF
    echo "INFO: fichier de configuration créé : $CONF_FILE"
    echo "INFO: modifie ce fichier si besoin, puis relance le script."
fi

# Lecture propre :
# - enlève les commentaires
# - enlève les espaces autour de la ligne
# - accepte le format key=value du genre : 1=192.168.1.126:7777
# - accepte encore l'ancien format direct : 192.168.1.126:7777
# - déduplique les adresses
mapfile -t REGISTRIES < <(
    sed 's/#.*$//' "$CONF_FILE" \
    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
    | grep -v '^$' \
    | sed 's/^[^=]*=//' \
    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
    | grep -v '^$' \
    | awk '!seen[$0]++'
)

if [ "${#REGISTRIES[@]}" -eq 0 ]; then
    echo "ERREUR: aucune adresse trouvée dans $CONF_FILE"
    echo "Ajoute au moins une ligne du type : 1=192.168.1.2:5000"
    exit 1
fi

# Sécurité simple : évite de casser le JSON avec des caractères interdits.
for reg in "${REGISTRIES[@]}"; do
    if [[ "$reg" == *'"'* || "$reg" == *','* || "$reg" == *'['* || "$reg" == *']'* || "$reg" == *'{'* || "$reg" == *'}'* || "$reg" =~ [[:space:]] ]]; then
        echo "ERREUR: adresse invalide dans $CONF_FILE : $reg"
        exit 1
    fi
done

if [ -f "$DAEMON_JSON" ]; then
    cp -a "$DAEMON_JSON" "$DAEMON_JSON.bak.$(date +%Y%m%d_%H%M%S)"
fi

{
    echo '{'
    echo '  "insecure-registries": ['
    for i in "${!REGISTRIES[@]}"; do
        sep=','
        if [ "$i" -eq "$((${#REGISTRIES[@]} - 1))" ]; then
            sep=''
        fi
        printf '    "%s"%s\n' "${REGISTRIES[$i]}" "$sep"
    done
    echo '  ]'
    echo '}'
} > "$DAEMON_JSON"

echo "OK: Docker insecure registries configurés depuis : $CONF_FILE"
cat "$DAEMON_JSON"

echo
echo "Redémarrage Docker..."
systemctl restart docker

echo
echo "Vérification :"
docker info 2>/dev/null | sed -n '/Insecure Registries:/,/Live Restore Enabled:/p'
