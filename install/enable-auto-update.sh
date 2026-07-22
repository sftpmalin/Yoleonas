#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_ROOT="${YOLEONAS_DIR:-/opt/yoleonas}"
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }
[ -x "$INSTALL_ROOT/install/update.sh" ] || { echo "update.sh introuvable dans $INSTALL_ROOT/install." >&2; exit 1; }

cat > /etc/systemd/system/yoleonas-update.service <<EOF
[Unit]
Description=Mise à jour automatique de Yoleonas
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=YOLEONAS_DIR=$INSTALL_ROOT
ExecStart=/usr/bin/bash $INSTALL_ROOT/install/update.sh
EOF

cat > /etc/systemd/system/yoleonas-update.timer <<'EOF'
[Unit]
Description=Recherche quotidienne des mises à jour Yoleonas

[Timer]
OnBootSec=10min
OnUnitActiveSec=24h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now yoleonas-update.timer
echo "Mise à jour automatique quotidienne activée."
