# Yoleonas

Yoleonas est un projet de NAS personnel regroupant une interface de gestion, des scripts d'administration et des applications clientes.

Le code source et la documentation d'installation seront ajoutés progressivement.

## Première installation

Clonez le dépôt puis entrez dans son dossier :

```bash
git clone https://github.com/sftpmalin/Yoleonas.git
cd Yoleonas
```

Avant la première installation, créez la référence locale d'intégrité. Cette
commande génère automatiquement le dossier `init`, le catalogue
`init/system.sha256` et l'archive `init/system.tar.gz` à partir des fichiers
présents sur la machine :

```bash
sudo bash system/system.sh -add
```

Lancez ensuite l'installation du service :

```bash
sudo bash system/system.sh -install
```

L'ordre est important :

1. `-add` crée le catalogue et l'archive de référence dans `init`.
2. `-install` installe l'application et son service Linux.

Après `-install`, le service systemd est activé pour démarrer automatiquement
avec Linux, puis il est lancé immédiatement.

Le dossier `init` est volontairement généré localement et n'est pas stocké sur
GitHub, afin de ne pas gonfler inutilement le dépôt. Le dossier `offline` est
également facultatif : s'il n'existe pas, `system.sh` télécharge les dépendances
Python nécessaires depuis Internet.

## Commandes de `system.sh`

Le script `system/system.sh` installe, démarre et protège l'interface Yoleonas.
Les commandes d'administration doivent être lancées avec `sudo`.

| Commande | Fonction |
| --- | --- |
| `sudo bash system/system.sh -add` | Crée ou met à jour la référence d'intégrité dans `init` : catalogue SHA-256 et archive locale des dossiers présents parmi `system`, `scripts`, `offline` et `bin`. À exécuter avant la première installation, puis après une modification volontaire des fichiers protégés. |
| `sudo bash system/system.sh -install` | Installe les paquets Debian nécessaires, crée l'environnement Python, installe les dépendances, génère la clé secrète locale, crée le service systemd, l'active au démarrage de Linux et le lance. |
| `sudo bash system/system.sh -start` | Démarre le service Yoleonas déjà installé. |
| `sudo bash system/system.sh -stop` | Arrête le service. |
| `sudo bash system/system.sh -restart` | Vérifie l'intégrité des fichiers puis redémarre le service. |
| `sudo bash system/system.sh -status` | Affiche l'état du service, ses chemins et son port réseau. |
| `sudo bash system/system.sh -logs` | Affiche les derniers journaux systemd et le fichier de log. |
| `sudo bash system/system.sh -routes` | Vérifie le chargement de l'application Flask et affiche ses routes. |
| `sudo bash system/system.sh -integrity` | Contrôle les fichiers protégés et restaure ceux qui ont été modifiés ou supprimés à partir de l'archive `init`. |
| `sudo bash system/system.sh -backup` | Crée dans `init/backups` une sauvegarde datée des dossiers protégés présents. |
| `sudo bash system/system.sh -restaure` | Propose les sauvegardes disponibles, restaure celle choisie, puis reconstruit la référence d'intégrité. |

### Après une mise à jour volontaire

Le contrôle d'intégrité considère toute modification non enregistrée comme
anormale. Après avoir volontairement modifié ou mis à jour les fichiers du
projet, validez la nouvelle version de référence avec :

```bash
sudo bash system/system.sh -add
sudo bash system/system.sh -restart
```
