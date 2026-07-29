#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
user_linux_only_v2.py
Gestion simple des utilisateurs Linux uniquement.

AUCUN Samba.
AUCUN miniDLNA.
AUCUN Docker.
AUCUN Flask.
AUCUN effacement de home.
"""

import os
import sys
import pwd
import grp
import shutil
import subprocess
from dataclasses import dataclass, field


MIN_NORMAL_ID = 1000
MAX_NORMAL_ID = 60000
DEFAULT_SHELL = "/bin/bash"


def print_help(exit_code=0):
    print(r"""
user_linux_only_v2.py — gestion utilisateurs Linux uniquement

USAGE
  ./user_linux_only_v2.py
      Affiche cette aide.

  ./user_linux_only_v2.py -list
      Liste les utilisateurs Linux utiles.

  ./user_linux_only_v2.py -info=yoan
      Affiche le détail d'un utilisateur.

  ./user_linux_only_v2.py -user=yoan
      Crée l'utilisateur yoan si absent.
      UID/GID choisis automatiquement.
      Aucun mot de passe n'est défini.

  ./user_linux_only_v2.py -user=yoan -pass=0000
      Crée ou met à jour l'utilisateur yoan et définit son mot de passe Linux.

  ./user_linux_only_v2.py -user=yoan -uid=1000 -gid=1000
      Crée ou met à jour l'utilisateur avec UID/GID demandés.
      Si le user existe déjà, change seulement la fiche Linux.
      Ne corrige pas les propriétaires des fichiers existants.

  ./user_linux_only_v2.py -setpass=yoan -pass=0000
      Change uniquement le mot de passe Linux de yoan.

  ./user_linux_only_v2.py -lock=yoan
      Verrouille le mot de passe Linux de yoan.

  ./user_linux_only_v2.py -unlock=yoan
      Déverrouille le mot de passe Linux de yoan.

  ./user_linux_only_v2.py -remove=yoan
      Supprime l'utilisateur Linux.
      Ne supprime jamais le dossier home.

OPTIONS
  -user=nom              utilisateur à créer ou mettre à jour
  -pass=motdepasse       mot de passe Linux à définir
  -uid=1000              UID demandé
  -gid=1000              GID demandé
  -shell=/bin/bash       shell de login, défaut /bin/bash
  -home=/home/yoan       chemin home inscrit dans /etc/passwd
  -no-home               à la création, ne crée pas le dossier home
  -list                  liste les utilisateurs
  -info=nom              détail utilisateur
  -setpass=nom           change uniquement le mot de passe
  -lock=nom              verrouille le mot de passe
  -unlock=nom            déverrouille le mot de passe
  -remove=nom            supprime uniquement l'entrée utilisateur Linux
  -dry-run               affiche les commandes sans modifier

RACCOURCIS TOLÉRÉS
  -useryoan              équivaut à -user=yoan
  -pass0000              équivaut à -pass=0000
  -uid1000               équivaut à -uid=1000
  -gid1000               équivaut à -gid=1000
  -hiud1000              toléré comme alias de -uid1000

IMPORTANT
  Ce script ne touche qu'aux comptes Linux : /etc/passwd, /etc/group, /etc/shadow.
  Il ne lance aucune commande Samba, aucun smbpasswd, aucun pdbedit.
  Il ne supprime jamais le dossier home.
""".strip())
    sys.exit(exit_code)


def ok(msg):
    print(f"OK : {msg}")


def warn(msg):
    print(f"ATTENTION : {msg}")


@dataclass
class Args:
    action: str | None = None
    user: str | None = None
    password: str | None = None
    uid: int | None = None
    gid: int | None = None
    shell: str = DEFAULT_SHELL
    home: str | None = None
    create_home: bool = True
    dry_run: bool = False
    unknown: list[str] = field(default_factory=list)


def normalize_token(token):
    return (
        token.replace("–", "-")
             .replace("—", "-")
             .replace("−", "-")
             .strip()
    )


def read_value(argv, idx):
    if idx + 1 < len(argv):
        return argv[idx + 1], idx + 1
    return None, idx


def parse_int(value, name):
    try:
        n = int(str(value).strip())
        if n < 0:
            raise ValueError
        return n
    except Exception:
        raise SystemExit(f"ERREUR : {name} invalide : {value}")


def clean_username(name):
    if name is None:
        raise SystemExit("ERREUR : nom utilisateur manquant")

    name = str(name).strip()
    if not name:
        raise SystemExit("ERREUR : nom utilisateur vide")

    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if any(c not in allowed for c in name):
        raise SystemExit(f"ERREUR : nom utilisateur invalide : {name}")

    if name.startswith("-") or name in (".", ".."):
        raise SystemExit(f"ERREUR : nom utilisateur invalide : {name}")

    return name


def set_action(args, action, value=None):
    if args.action and args.action != action:
        raise SystemExit(f"ERREUR : actions incompatibles : {args.action} et {action}")
    args.action = action
    if value is not None:
        args.user = value


def parse_args(argv):
    if not argv:
        print_help(0)

    args = Args()
    argv = [normalize_token(a) for a in argv]

    i = 0
    while i < len(argv):
        t = argv[i]

        if t in ("-h", "--help", "-help", "/?"):
            print_help(0)

        elif t in ("-dry-run", "--dry-run"):
            args.dry_run = True

        elif t in ("-no-home", "--no-home", "-nohome"):
            args.create_home = False

        elif t in ("-list", "--list", "-ls"):
            set_action(args, "list")

        elif t in ("-delete-home", "--delete-home", "-remove-home", "--remove-home"):
            raise SystemExit("ERREUR : option interdite ici. Ce script ne supprime jamais les dossiers home.")

        elif t in ("-user", "--user"):
            val, i = read_value(argv, i)
            if not val:
                raise SystemExit("ERREUR : valeur manquante après -user")
            set_action(args, "upsert", val)

        elif t.startswith("-user=") or t.startswith("--user="):
            set_action(args, "upsert", t.split("=", 1)[1])

        elif t.startswith("-user") and len(t) > len("-user"):
            set_action(args, "upsert", t[len("-user"):].lstrip("=:"))

        elif t in ("-pass", "--pass", "-password", "--password"):
            val, i = read_value(argv, i)
            if val is None:
                raise SystemExit("ERREUR : valeur manquante après -pass")
            args.password = val

        elif t.startswith("-pass=") or t.startswith("--pass="):
            args.password = t.split("=", 1)[1]

        elif t.startswith("-password=") or t.startswith("--password="):
            args.password = t.split("=", 1)[1]

        elif t.startswith("-pass") and len(t) > len("-pass"):
            args.password = t[len("-pass"):].lstrip("=:")

        elif t in ("-uid", "--uid", "-hiud", "--hiud"):
            val, i = read_value(argv, i)
            if val is None:
                raise SystemExit("ERREUR : valeur manquante après -uid")
            args.uid = parse_int(val, "UID")

        elif t.startswith("-uid=") or t.startswith("--uid="):
            args.uid = parse_int(t.split("=", 1)[1], "UID")

        elif t.startswith("-hiud=") or t.startswith("--hiud="):
            args.uid = parse_int(t.split("=", 1)[1], "UID")

        elif t.startswith("-uid") and len(t) > len("-uid"):
            args.uid = parse_int(t[len("-uid"):].lstrip("=:"), "UID")

        elif t.startswith("-hiud") and len(t) > len("-hiud"):
            args.uid = parse_int(t[len("-hiud"):].lstrip("=:"), "UID")

        elif t in ("-gid", "--gid", "-guid", "--guid"):
            val, i = read_value(argv, i)
            if val is None:
                raise SystemExit("ERREUR : valeur manquante après -gid")
            args.gid = parse_int(val, "GID")

        elif t.startswith("-gid=") or t.startswith("--gid="):
            args.gid = parse_int(t.split("=", 1)[1], "GID")

        elif t.startswith("-guid=") or t.startswith("--guid="):
            args.gid = parse_int(t.split("=", 1)[1], "GID")

        elif t.startswith("-gid") and len(t) > len("-gid"):
            args.gid = parse_int(t[len("-gid"):].lstrip("=:"), "GID")

        elif t.startswith("-guid") and len(t) > len("-guid"):
            args.gid = parse_int(t[len("-guid"):].lstrip("=:"), "GID")

        elif t in ("-shell", "--shell"):
            val, i = read_value(argv, i)
            if not val:
                raise SystemExit("ERREUR : valeur manquante après -shell")
            args.shell = val

        elif t.startswith("-shell=") or t.startswith("--shell="):
            args.shell = t.split("=", 1)[1]

        elif t in ("-home", "--home"):
            val, i = read_value(argv, i)
            if not val:
                raise SystemExit("ERREUR : valeur manquante après -home")
            args.home = val

        elif t.startswith("-home=") or t.startswith("--home="):
            args.home = t.split("=", 1)[1]

        elif t in ("-remove", "--remove", "-del", "--del"):
            val, i = read_value(argv, i)
            if not val:
                raise SystemExit("ERREUR : valeur manquante après -remove")
            set_action(args, "remove", val)

        elif t.startswith("-remove=") or t.startswith("--remove="):
            set_action(args, "remove", t.split("=", 1)[1])

        elif t.startswith("-remove") and len(t) > len("-remove"):
            set_action(args, "remove", t[len("-remove"):].lstrip("=:"))

        elif t.startswith("-del=") or t.startswith("--del="):
            set_action(args, "remove", t.split("=", 1)[1])

        elif t in ("-info", "--info", "-show", "--show"):
            val, i = read_value(argv, i)
            if not val:
                raise SystemExit("ERREUR : valeur manquante après -info")
            set_action(args, "info", val)

        elif t.startswith("-info=") or t.startswith("--info="):
            set_action(args, "info", t.split("=", 1)[1])

        elif t.startswith("-show=") or t.startswith("--show="):
            set_action(args, "info", t.split("=", 1)[1])

        elif t in ("-setpass", "--setpass", "-addpass", "--addpass"):
            val, i = read_value(argv, i)
            if not val:
                raise SystemExit("ERREUR : valeur manquante après -setpass")
            set_action(args, "setpass", val)

        elif t.startswith("-setpass=") or t.startswith("--setpass="):
            set_action(args, "setpass", t.split("=", 1)[1])

        elif t.startswith("-addpass=") or t.startswith("--addpass="):
            set_action(args, "setpass", t.split("=", 1)[1])

        elif t in ("-lock", "--lock"):
            val, i = read_value(argv, i)
            if not val:
                raise SystemExit("ERREUR : valeur manquante après -lock")
            set_action(args, "lock", val)

        elif t.startswith("-lock=") or t.startswith("--lock="):
            set_action(args, "lock", t.split("=", 1)[1])

        elif t in ("-unlock", "--unlock"):
            val, i = read_value(argv, i)
            if not val:
                raise SystemExit("ERREUR : valeur manquante après -unlock")
            set_action(args, "unlock", val)

        elif t.startswith("-unlock=") or t.startswith("--unlock="):
            set_action(args, "unlock", t.split("=", 1)[1])

        else:
            args.unknown.append(t)

        i += 1

    if args.unknown:
        raise SystemExit("ERREUR : option inconnue : " + ", ".join(args.unknown))

    if not args.action:
        print_help(0)

    if args.action in ("upsert", "remove", "info", "setpass", "lock", "unlock"):
        args.user = clean_username(args.user)

    if args.password is not None and args.action == "list":
        raise SystemExit("ERREUR : -pass ne sert à rien avec -list")

    if args.action == "setpass" and args.password is None:
        raise SystemExit("ERREUR : utilisez -setpass=nom -pass=motdepasse")

    return args


def require_root():
    if os.geteuid() != 0:
        raise SystemExit("ERREUR : cette action doit être lancée en root.")


def command_exists(cmd):
    return shutil.which(cmd) is not None


def run(cmd, input_text=None, dry_run=False):
    printable = " ".join(cmd)
    if dry_run:
        print(f"DRY-RUN : {printable}")
        return ""

    print(f"$ {printable}")
    p = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    if p.stdout.strip():
        print(p.stdout.rstrip())
    if p.stderr.strip():
        print(p.stderr.rstrip(), file=sys.stderr)

    if p.returncode != 0:
        raise SystemExit(f"ERREUR : commande échouée ({p.returncode}) : {printable}")

    return p.stdout


def user_exists(name):
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def group_by_name(name):
    try:
        return grp.getgrnam(name)
    except KeyError:
        return None


def group_by_gid(gid):
    try:
        return grp.getgrgid(gid)
    except KeyError:
        return None


def used_uids():
    return {p.pw_uid for p in pwd.getpwall()}


def used_gids():
    return {g.gr_gid for g in grp.getgrall()}


def next_free_id(prefer_same=True):
    uids = used_uids()
    gids = used_gids()

    if prefer_same:
        for n in range(MIN_NORMAL_ID, MAX_NORMAL_ID):
            if n not in uids and n not in gids:
                return n, n

    uid = next(n for n in range(MIN_NORMAL_ID, MAX_NORMAL_ID) if n not in uids)
    gid = next(n for n in range(MIN_NORMAL_ID, MAX_NORMAL_ID) if n not in gids)
    return uid, gid


def password_state(name):
    if not command_exists("passwd"):
        return "?"

    p = subprocess.run(
        ["passwd", "-S", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if p.returncode != 0:
        return "?"

    parts = p.stdout.split()
    if len(parts) < 2:
        return "?"

    code = parts[1]
    if code == "P":
        return "mot de passe actif"
    if code == "L":
        return "verrouillé"
    if code == "NP":
        return "sans mot de passe"
    return code


def print_user(p):
    try:
        gname = grp.getgrgid(p.pw_gid).gr_name
    except Exception:
        gname = str(p.pw_gid)

    print(f"Utilisateur : {p.pw_name}")
    print(f"UID         : {p.pw_uid}")
    print(f"GID         : {p.pw_gid} ({gname})")
    print(f"Home        : {p.pw_dir}")
    print(f"Shell       : {p.pw_shell}")
    print(f"Password    : {password_state(p.pw_name)}")


def list_users():
    rows = []
    for p in pwd.getpwall():
        if p.pw_uid == 0 or MIN_NORMAL_ID <= p.pw_uid < MAX_NORMAL_ID:
            try:
                gname = grp.getgrgid(p.pw_gid).gr_name
            except Exception:
                gname = str(p.pw_gid)
            rows.append((p.pw_uid, p.pw_name, p.pw_gid, gname, p.pw_dir, p.pw_shell, password_state(p.pw_name)))

    rows.sort(key=lambda x: (x[0], x[1]))

    print(f"{'UID':>6}  {'USER':<22} {'GID':>6}  {'GROUP':<22} {'PASSWORD':<20} HOME")
    print("-" * 105)
    for uid, name, gid, gname, home, shell, state in rows:
        print(f"{uid:>6}  {name:<22} {gid:>6}  {gname:<22} {state:<20} {home}")


def choose_ids(args, existing_user):
    uid = args.uid
    gid = args.gid

    if existing_user:
        if uid is None:
            uid = existing_user.pw_uid
        if gid is None:
            gid = existing_user.pw_gid
        return uid, gid

    if uid is None and gid is None:
        uid, gid = next_free_id(prefer_same=True)
    elif uid is None:
        uid, _ = next_free_id(prefer_same=False)
    elif gid is None:
        if uid not in used_gids():
            gid = uid
        else:
            _, gid = next_free_id(prefer_same=False)

    return uid, gid


def ensure_primary_group(username, gid, dry_run=False):
    by_gid = group_by_gid(gid)
    if by_gid:
        return by_gid.gr_name

    by_name = group_by_name(username)
    if by_name:
        warn(f"groupe {username} existe déjà avec GID {by_name.gr_gid}; utilisation de ce groupe")
        return by_name.gr_name

    run(["groupadd", "-g", str(gid), username], dry_run=dry_run)
    ok(f"groupe créé : {username} ({gid})")
    return username


def set_password(username, password, dry_run=False):
    if password is None:
        return

    if not command_exists("chpasswd"):
        raise SystemExit("ERREUR : commande chpasswd introuvable")

    run(["chpasswd"], input_text=f"{username}:{password}\n", dry_run=dry_run)
    ok(f"mot de passe Linux défini pour {username}")


def upsert_user(args):
    require_root()

    existing = user_exists(args.user)
    uid, gid = choose_ids(args, existing)
    group_name = ensure_primary_group(args.user, gid, dry_run=args.dry_run)
    home = args.home or (existing.pw_dir if existing else f"/home/{args.user}")
    shell = args.shell or DEFAULT_SHELL

    if existing:
        cmd = ["usermod"]

        if args.uid is not None and args.uid != existing.pw_uid:
            cmd += ["-u", str(uid)]

        if args.gid is not None and gid != existing.pw_gid:
            cmd += ["-g", group_name]

        if args.shell and args.shell != existing.pw_shell:
            cmd += ["-s", shell]

        if args.home and args.home != existing.pw_dir:
            cmd += ["-d", home]

        if len(cmd) > 1:
            cmd.append(args.user)
            run(cmd, dry_run=args.dry_run)
            ok(f"utilisateur mis à jour : {args.user}")
        else:
            ok(f"utilisateur déjà présent : {args.user}")

    else:
        cmd = ["useradd"]

        if args.create_home:
            cmd.append("-m")
        else:
            cmd.append("-M")

        cmd += ["-u", str(uid), "-g", group_name, "-d", home, "-s", shell, args.user]
        run(cmd, dry_run=args.dry_run)
        ok(f"utilisateur créé : {args.user} UID={uid} GID={gid}")

    if args.password is not None:
        set_password(args.user, args.password, dry_run=args.dry_run)
    else:
        ok("aucun mot de passe défini/modifié")

    if not args.dry_run:
        p = user_exists(args.user)
        if p:
            print()
            print_user(p)


def remove_user(args):
    require_root()

    if not user_exists(args.user):
        ok(f"utilisateur absent : {args.user}")
        return

    run(["userdel", args.user], dry_run=args.dry_run)
    ok(f"utilisateur Linux supprimé : {args.user}")
    ok("home conservé")


def info_user(args):
    p = user_exists(args.user)
    if not p:
        raise SystemExit(f"ERREUR : utilisateur introuvable : {args.user}")
    print_user(p)


def setpass_user(args):
    require_root()
    if not user_exists(args.user):
        raise SystemExit(f"ERREUR : utilisateur introuvable : {args.user}")
    set_password(args.user, args.password, dry_run=args.dry_run)


def lock_user(args):
    require_root()
    if not user_exists(args.user):
        raise SystemExit(f"ERREUR : utilisateur introuvable : {args.user}")
    run(["passwd", "-l", args.user], dry_run=args.dry_run)
    ok(f"mot de passe verrouillé : {args.user}")


def unlock_user(args):
    require_root()
    if not user_exists(args.user):
        raise SystemExit(f"ERREUR : utilisateur introuvable : {args.user}")
    run(["passwd", "-u", args.user], dry_run=args.dry_run)
    ok(f"mot de passe déverrouillé : {args.user}")


def main():
    args = parse_args(sys.argv[1:])

    if args.action == "list":
        list_users()
    elif args.action == "info":
        info_user(args)
    elif args.action == "upsert":
        upsert_user(args)
    elif args.action == "remove":
        remove_user(args)
    elif args.action == "setpass":
        setpass_user(args)
    elif args.action == "lock":
        lock_user(args)
    elif args.action == "unlock":
        unlock_user(args)
    else:
        print_help(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrompu.")
        sys.exit(130)
