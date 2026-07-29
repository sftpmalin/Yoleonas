#!/usr/bin/env bash
set -u

# Chemins relatifs : si ce script est dans .../scripts/menu.sh,
# le menu par défaut est .../conf/menu.conf.
SCRIPT_FILE="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR_DEFAULT="$(cd -- "$(dirname -- "$SCRIPT_FILE")" >/dev/null 2>&1 && pwd -P)"
BASE_DIR_DEFAULT="$(cd -- "$SCRIPT_DIR_DEFAULT/.." >/dev/null 2>&1 && pwd -P)"

SCRIPT_DIR="${SCRIPT_DIR:-$SCRIPT_DIR_DEFAULT}"
CONF_DIR="${CONF_DIR:-$BASE_DIR_DEFAULT/conf}"
MENU_CONF="${MENU_CONF:-$CONF_DIR/menu.conf}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

TITLES=()
COMMANDS=()
TYPES=()
PARENTS=()
LAST_ID=-1

declare -A ACTIVE_SUBMENU

trim() {
  local s="$*"
  s="${s#${s%%[![:space:]]*}}"
  s="${s%${s##*[![:space:]]}}"
  printf '%s' "$s"
}

clean_title() {
  local s
  s="$(trim "$*")"
  # Permet : --- Titre ---, ---- Titre ----, [Titre]
  s="$(printf '%s' "$s" | sed -E 's/^-+[[:space:]]*//; s/[[:space:]]*-+$//')"
  if [[ "$s" =~ ^\[(.*)\]$ ]]; then
    s="${BASH_REMATCH[1]}"
  fi
  trim "$s"
}

add_item() {
  local type="$1"
  local title="$2"
  local cmd="$3"
  local parent="$4"
  TITLES+=("$title")
  COMMANDS+=("$cmd")
  TYPES+=("$type")
  PARENTS+=("$parent")
  LAST_ID=$((${#TITLES[@]} - 1))
}

clear_submenus_from() {
  local from="$1"
  local k
  for k in "${!ACTIVE_SUBMENU[@]}"; do
    if (( k >= from )); then
      unset 'ACTIVE_SUBMENU[$k]'
    fi
  done
}

reset_all_submenus() {
  ACTIVE_SUBMENU=()
}

load_menu() {
  TITLES=()
  COMMANDS=()
  TYPES=()
  PARENTS=()
  LAST_ID=-1
  reset_all_submenus

  [[ -f "$MENU_CONF" ]] || return 0

  local raw line body title cmd parent level

  while IFS= read -r raw || [[ -n "$raw" ]]; do
    raw="${raw//$'\r'/}"
    line="$(trim "$raw")"

    [[ -z "$line" ]] && continue
    [[ "$line" == \#* ]] && continue
    [[ "$line" == \;* ]] && continue

    level=0
    while [[ "$line" == ,* ]]; do
      level=$((level + 1))
      line="${line#,}"
    done
    body="$(trim "$line")"
    [[ -z "$body" ]] && continue

    # Trait visuel : -
    if [[ "$body" == "-" ]]; then
      if (( level == 0 )); then
        add_item "blank" "" "" "-1"
        reset_all_submenus
      else
        parent="${ACTIVE_SUBMENU[$level]:-}"
        [[ -n "$parent" ]] && add_item "blank" "" "" "$parent"
      fi
      continue
    fi

    # Ligne commande : Titre = commande
    if [[ "$body" == *"="* ]]; then
      title="$(trim "${body%%=*}")"
      cmd="$(trim "${body#*=}")"
      [[ -z "$title" || -z "$cmd" ]] && continue

      if (( level == 0 )); then
        add_item "entry" "$title" "$cmd" "-1"
        reset_all_submenus
      else
        parent="${ACTIVE_SUBMENU[$level]:-}"
        # Une commande avec virgule doit appartenir au dernier sous-menu du même niveau.
        [[ -n "$parent" ]] && add_item "entry" "$title" "$cmd" "$parent"
      fi
      continue
    fi

    # Ligne sans =
    title="$(clean_title "$body")"
    [[ -z "$title" ]] && continue

    if (( level == 0 )); then
      # Sans virgule : simple titre visuel, non cliquable.
      add_item "section" "$title" "" "-1"
      reset_all_submenus
    else
      # Avec virgule : vrai sous-menu cliquable.
      if (( level == 1 )); then
        parent="-1"
      else
        parent="${ACTIVE_SUBMENU[$((level - 1))]:-}"
      fi

      # Si on demande niveau 2 sans niveau 1 actif, on ignore pour éviter un menu cassé.
      [[ -z "$parent" ]] && continue

      add_item "submenu" "$title" "" "$parent"
      ACTIVE_SUBMENU[$level]="$LAST_ID"
      clear_submenus_from $((level + 1))
    fi
  done < "$MENU_CONF"
}

pause_menu() {
  echo
  read -rp "Entrée pour revenir au menu..." _
}

run_config_command() {
  local cmdline="$1"
  local -a parts
  local first full

  echo
  echo ">>> $cmdline"
  echo

  eval "parts=( $cmdline )"

  if [[ ${#parts[@]} -eq 0 ]]; then
    echo "Commande vide."
    return 1
  fi

  first="${parts[0]}"

  if [[ "$first" == *.py ]]; then
    if [[ "$first" = /* ]]; then
      full="$first"
    else
      full="$SCRIPT_DIR/$first"
    fi
    "$PYTHON_BIN" "$full" "${parts[@]:1}"
    return $?
  fi

  if [[ "$first" == *.sh ]]; then
    if [[ "$first" = /* ]]; then
      full="$first"
    else
      full="$SCRIPT_DIR/$first"
    fi
    bash "$full" "${parts[@]:1}"
    return $?
  fi

  bash -lc "$cmdline"
}

print_menu() {
  local menu_id="$1"
  local title="$2"
  local i n=0 type item_title

  MAP=()
  clear 2>/dev/null || true

  if [[ "$menu_id" != "-1" ]]; then
    echo "$title"
    echo
  fi

  if [[ ${#TITLES[@]} -eq 0 ]]; then
    echo "Menu vide."
    echo
    echo "0) Quitter"
    return
  fi

  for i in "${!TITLES[@]}"; do
    [[ "${PARENTS[$i]}" == "$menu_id" ]] || continue

    type="${TYPES[$i]}"
    item_title="${TITLES[$i]}"

    case "$type" in
      blank)
        echo
        ;;
      section)
        echo "$item_title"
        ;;
      entry|submenu)
        n=$((n + 1))
        MAP[$n]="$i"
        echo "$n) $item_title"
        ;;
    esac
  done

  echo
  if [[ "$menu_id" == "-1" ]]; then
    echo "0) Quitter"
  else
    echo "0) Retour"
  fi
}

main() {
  local current_id="-1"
  local current_title=""
  local choice idx rc type

  while true; do
    load_menu
    print_menu "$current_id" "$current_title"
    echo
    read -rp "Choix : " choice

    if [[ "$choice" == "0" ]]; then
      if [[ "$current_id" == "-1" ]]; then
        exit 0
      else
        current_id="${PARENTS[$current_id]}"
        if [[ "$current_id" == "-1" ]]; then
          current_title=""
        else
          current_title="${TITLES[$current_id]}"
        fi
        continue
      fi
    fi

    if [[ "$choice" =~ ^[0-9]+$ ]] && [[ -n "${MAP[$choice]:-}" ]]; then
      idx="${MAP[$choice]}"
      type="${TYPES[$idx]}"

      if [[ "$type" == "submenu" ]]; then
        current_id="$idx"
        current_title="${TITLES[$idx]}"
        continue
      fi

      if [[ "$type" == "entry" ]]; then
        run_config_command "${COMMANDS[$idx]}"
        rc=$?
        echo
        if [[ $rc -eq 0 ]]; then
          echo "OK"
        else
          echo "Erreur : $rc"
        fi
        pause_menu
        continue
      fi
    fi

    echo "Choix invalide."
    sleep 1
  done
}

main
