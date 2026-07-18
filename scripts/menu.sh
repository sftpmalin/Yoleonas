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
MENU_TITLE="${MENU_TITLE:-YoLeo CLI}"

TITLES=()
COMMANDS=()
TYPES=()
PARENTS=()
LAST_ID=-1
KEY=""
SELECTABLE_IDS=()

# Mémorise la ligne sélectionnée dans chaque sous-menu.
declare -A ACTIVE_SUBMENU
declare -A SAVED_SELECTION

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
        # Une commande avec virgule appartient au dernier sous-menu du même niveau.
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

      # Niveau imbriqué demandé sans parent actif : ligne ignorée.
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

clear_screen() {
  printf '\033[2J\033[H'
}

hide_cursor() {
  printf '\033[?25l'
}

show_cursor() {
  printf '\033[?25h'
}

reset_terminal() {
  printf '\033[0m'
  show_cursor
}

cleanup() {
  reset_terminal
}

trap cleanup EXIT
trap 'exit 130' INT TERM HUP

build_selectable_items() {
  local menu_id="$1"
  local i

  SELECTABLE_IDS=()
  for i in "${!TITLES[@]}"; do
    [[ "${PARENTS[$i]}" == "$menu_id" ]] || continue
    case "${TYPES[$i]}" in
      entry|submenu) SELECTABLE_IDS+=("$i") ;;
    esac
  done

  if [[ "$menu_id" == "-1" ]]; then
    SELECTABLE_IDS+=("__quit__")
  else
    SELECTABLE_IDS+=("__back__")
  fi
}

print_selectable_line() {
  local selected="$1"
  local text="$2"
  local suffix="${3:-}"

  if [[ "$selected" == "1" ]]; then
    printf '\033[7m  > %-42s %s  \033[0m\n' "$text" "$suffix"
  else
    printf '    %-42s %s\n' "$text" "$suffix"
  fi
}

render_arrow_menu() {
  local menu_id="$1"
  local selected_pos="$2"
  local i type item_title selectable_pos=0 special_pos

  clear_screen
  hide_cursor

  printf '===============================================\n'
  printf '  %s\n' "$MENU_TITLE"
  printf '===============================================\n'

  if [[ "$menu_id" != "-1" ]]; then
    printf '  %s\n' "${TITLES[$menu_id]}"
    printf '%s\n' '-----------------------------------------------'
  fi
  echo

  if [[ ${#TITLES[@]} -eq 0 ]]; then
    echo "  Menu vide : $MENU_CONF"
    echo
  else
    for i in "${!TITLES[@]}"; do
      [[ "${PARENTS[$i]}" == "$menu_id" ]] || continue

      type="${TYPES[$i]}"
      item_title="${TITLES[$i]}"

      case "$type" in
        blank)
          echo
          ;;
        section)
          printf '\033[1m  %s\033[0m\n' "$item_title"
          ;;
        entry)
          if (( selectable_pos == selected_pos )); then
            print_selectable_line 1 "$item_title" ""
          else
            print_selectable_line 0 "$item_title" ""
          fi
          selectable_pos=$((selectable_pos + 1))
          ;;
        submenu)
          if (( selectable_pos == selected_pos )); then
            print_selectable_line 1 "$item_title" "▶"
          else
            print_selectable_line 0 "$item_title" "▶"
          fi
          selectable_pos=$((selectable_pos + 1))
          ;;
      esac
    done
  fi

  echo
  special_pos=$((${#SELECTABLE_IDS[@]} - 1))
  if [[ "$menu_id" == "-1" ]]; then
    if (( selected_pos == special_pos )); then
      print_selectable_line 1 "Quitter" ""
    else
      print_selectable_line 0 "Quitter" ""
    fi
  else
    if (( selected_pos == special_pos )); then
      print_selectable_line 1 "Retour" "◀"
    else
      print_selectable_line 0 "Retour" "◀"
    fi
  fi

  echo
  printf '  ↑ ↓ Déplacer   Entrée Valider   ← Retour   Q Quitter\n'
}

read_key() {
  local second="" third=""
  KEY=""

  IFS= read -rsn1 KEY || return 1

  # Une flèche arrive en général sous la forme ESC [ A/B/C/D.
  if [[ "$KEY" == $'\e' ]]; then
    if IFS= read -rsn1 -t 0.08 second; then
      KEY+="$second"
      if [[ "$second" == "[" || "$second" == "O" ]]; then
        IFS= read -rsn1 -t 0.08 third || true
        KEY+="$third"
      fi
    fi
  fi
}

return_to_parent() {
  local current_id="$1"

  if [[ "$current_id" == "-1" ]]; then
    printf '%s' "-1"
  else
    printf '%s' "${PARENTS[$current_id]}"
  fi
}

run_selected_entry() {
  local idx="$1"
  local rc

  reset_terminal
  clear_screen
  run_config_command "${COMMANDS[$idx]}"
  rc=$?
  echo
  if [[ $rc -eq 0 ]]; then
    echo "OK"
  else
    echo "Erreur : $rc"
  fi
  pause_menu
}

main_arrow() {
  local current_id="-1"
  local selected_pos=0
  local count target type parent

  while true; do
    load_menu

    # Si menu.conf a changé pendant l'exécution et que le sous-menu courant
    # n'existe plus, retour automatique à la racine.
    if [[ "$current_id" != "-1" ]] && [[ -z "${TYPES[$current_id]:-}" ]]; then
      current_id="-1"
    fi

    build_selectable_items "$current_id"
    count=${#SELECTABLE_IDS[@]}

    selected_pos="${SAVED_SELECTION[$current_id]:-0}"
    if (( selected_pos < 0 || selected_pos >= count )); then
      selected_pos=0
    fi

    while true; do
      render_arrow_menu "$current_id" "$selected_pos"
      read_key || exit 0

      case "$KEY" in
        $'\e[A')
          selected_pos=$((selected_pos - 1))
          (( selected_pos < 0 )) && selected_pos=$((count - 1))
          SAVED_SELECTION[$current_id]="$selected_pos"
          ;;
        $'\e[B')
          selected_pos=$((selected_pos + 1))
          (( selected_pos >= count )) && selected_pos=0
          SAVED_SELECTION[$current_id]="$selected_pos"
          ;;
        $'\e[H'|$'\eOH')
          selected_pos=0
          SAVED_SELECTION[$current_id]="$selected_pos"
          ;;
        $'\e[F'|$'\eOF')
          selected_pos=$((count - 1))
          SAVED_SELECTION[$current_id]="$selected_pos"
          ;;
        $'\e[D'|$'\x7f'|$'\b')
          if [[ "$current_id" != "-1" ]]; then
            current_id="$(return_to_parent "$current_id")"
            break
          fi
          ;;
        $'\e')
          if [[ "$current_id" == "-1" ]]; then
            exit 0
          fi
          current_id="$(return_to_parent "$current_id")"
          break
          ;;
        q|Q)
          exit 0
          ;;
        ""|$'\r'|" ")
          target="${SELECTABLE_IDS[$selected_pos]}"

          case "$target" in
            __quit__)
              exit 0
              ;;
            __back__)
              current_id="$(return_to_parent "$current_id")"
              break
              ;;
          esac

          type="${TYPES[$target]}"
          if [[ "$type" == "submenu" ]]; then
            current_id="$target"
            break
          fi

          if [[ "$type" == "entry" ]]; then
            run_selected_entry "$target"
            break
          fi
          ;;
        $'\e[C')
          target="${SELECTABLE_IDS[$selected_pos]}"
          if [[ "$target" != __* ]] && [[ "${TYPES[$target]}" == "submenu" ]]; then
            current_id="$target"
            break
          fi
          ;;
      esac
    done
  done
}

# Ancien mode numérique conservé comme secours pour un terminal sans ANSI.
print_numeric_menu() {
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
      blank) echo ;;
      section) echo "$item_title" ;;
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

main_numeric() {
  local current_id="-1"
  local current_title=""
  local choice idx rc type

  while true; do
    load_menu
    print_numeric_menu "$current_id" "$current_title"
    echo
    read -rp "Choix : " choice

    if [[ "$choice" == "0" ]]; then
      if [[ "$current_id" == "-1" ]]; then
        exit 0
      fi
      current_id="${PARENTS[$current_id]}"
      if [[ "$current_id" == "-1" ]]; then
        current_title=""
      else
        current_title="${TITLES[$current_id]}"
      fi
      continue
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

main() {
  if [[ -t 0 && -t 1 && "${TERM:-dumb}" != "dumb" ]]; then
    main_arrow
  else
    main_numeric
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
