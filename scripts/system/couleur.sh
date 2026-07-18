#!/bin/bash

# --- CONFIG TERMINAL ROOT UNRAID ---
# Réécrit un bashrc propre au démarrage.
# Un seul alias métier : menu

cat <<'EOF' > /root/.bashrc
# Alias menu principal
alias menu='bash /dockers/scripts/menu.sh'

# Alias de base
alias ls='ls --color=auto'
alias ll='ls -lah --color=auto'

# Prompt cyan
PS1='\[\e[1;36m\]\u@\h:\w\$ \[\e[0m\]'

# Couleurs fichiers
export LS_COLORS=$LS_COLORS:'*.sh=01;32:*.yml=01;35:*.yaml=01;35:*.conf=01;33:*.log=00;31:'
EOF

# Forcer la lecture du bashrc au login
echo '[[ -f ~/.bashrc ]] && . ~/.bashrc' > /root/.bash_profile
