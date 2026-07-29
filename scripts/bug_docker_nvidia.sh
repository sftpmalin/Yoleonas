#!/usr/bin/env bash
set -euo pipefail

CONF="/etc/docker/daemon.json"
BACKUP="/etc/docker/daemon.json.bak.bugDockerNvidia.$(date +%Y%m%d_%H%M%S)"

echo "=== Correction bug NVIDIA Docker / NVML ==="
echo
echo "Ce script modifie uniquement la configuration du service Docker."
echo "Il ne supprime aucun conteneur, aucune image, aucun volume."
echo "Attention : le redémarrage du service Docker arrêtera temporairement les conteneurs."
echo

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERREUR] Lance ce script en root."
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERREUR] python3 est nécessaire."
  exit 1
fi

echo "=== État actuel Docker ==="
docker info 2>/dev/null | grep -Ei 'Cgroup Driver|Cgroup Version|Runtimes|Default Runtime' || true
echo

echo "=== Sauvegarde ==="
if [ -f "$CONF" ]; then
  cp -a "$CONF" "$BACKUP"
  echo "[OK] Sauvegarde créée : $BACKUP"
else
  echo "[INFO] Aucun daemon.json existant, création d'un nouveau fichier."
fi
echo

echo "=== Modification de $CONF ==="
python3 - <<'PY'
import json
import os
import tempfile

conf = "/etc/docker/daemon.json"
data = {}

if os.path.exists(conf) and os.path.getsize(conf) > 0:
    with open(conf, "r", encoding="utf-8") as f:
        data = json.load(f)

opts = data.get("exec-opts", [])

if not isinstance(opts, list):
    raise SystemExit("[ERREUR] exec-opts existe mais n'est pas une liste JSON.")

opts = [x for x in opts if not str(x).startswith("native.cgroupdriver=")]
opts.append("native.cgroupdriver=cgroupfs")

data["exec-opts"] = opts

directory = os.path.dirname(conf)
fd, tmp = tempfile.mkstemp(prefix="daemon.json.", suffix=".tmp", dir=directory)

with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
    json.dump(data, f, indent=2)
    f.write("\n")

os.replace(tmp, conf)

with open(conf, "r", encoding="utf-8") as f:
    print(f.read())
PY

echo "=== Vérification JSON ==="
python3 -m json.tool "$CONF" >/dev/null
echo "[OK] daemon.json valide"
echo

echo "=== Redémarrage du service Docker ==="
systemctl restart docker
echo "[OK] Service Docker redémarré"
echo

echo "=== Nouvel état Docker ==="
docker info 2>/dev/null | grep -Ei 'Cgroup Driver|Cgroup Version|Runtimes|Default Runtime' || true
echo

echo "=== Test NVIDIA dans media si le conteneur est actif ==="
if docker ps --format '{{.Names}}' | grep -qx "media"; then
  docker exec media nvidia-smi || true
else
  echo "[INFO] Le conteneur media n'est pas actif."
fi

echo
echo "=== Terminé ==="
echo "Résultat attendu : Cgroup Driver: cgroupfs"
