# Yoleonas

Yoleonas est une interface libre de gestion de NAS Linux. Elle rassemble dans
une interface web les fonctions courantes d'administration du serveur et
fournit des applications clientes pour surveiller et piloter le NAS.

## Fonctions principales

- supervision du système, des disques et des services Linux ;
- gestion du stockage, de mergerfs, RAID et SnapRAID ;
- gestion des conteneurs, images, réseaux et stacks Docker ;
- partages Samba, NFS, SFTP, FTP et services multimédias ;
- gestion des utilisateurs, tâches planifiées et sauvegardes ;
- machines virtuelles libvirt et accès terminal ;
- API sécurisée utilisée par les applications clientes.

## Organisation du dépôt

- `system/` : serveur web Flask et interface d'administration du NAS ;
- `scripts/` : scripts principaux d'archive, cache, Docker, registre et stacks ;
- `bin/` : outils binaires complémentaires lorsqu'ils sont nécessaires ;
- `appli/android/` : application Android de supervision et de contrôle ;
- `appli/windows/` : agent Windows Yoleo.

## Installation simple avec l'image ISO

Pour un nouvel utilisateur, il n'est pas nécessaire de télécharger les dossiers
du projet un par un ni de compiler le code.

### 1. Télécharger Yoleonas NAS

**[Télécharger l'image ISO Yoleonas NAS Gen1](https://github.com/sftpmalin/Yoleonas/releases/latest/download/debian-13.4.0-amd64-nas-gen1.iso)**

[Télécharger le fichier de contrôle SHA-256](https://github.com/sftpmalin/Yoleonas/releases/latest/download/debian-13.4.0-amd64-nas-gen1.iso.sha256)

### 2. Créer la clé USB

Copiez l'image ISO sur une clé USB avec un logiciel de création de clé
amorçable, puis démarrez le futur NAS sur cette clé.

### 3. Installer le système

Suivez l'installation affichée à l'écran. Une fois l'installation terminée,
retirez la clé USB et redémarrez la machine.

> **Projet en cours de développement :** l'installation du système de base est
> simplifiée, mais certaines fonctions avancées, notamment le réseau des
> machines virtuelles et le réseau LAN des conteneurs Docker, demandent encore
> une configuration manuelle expliquée ci-dessous.

[Voir toutes les versions et tous les fichiers publiés](https://github.com/sftpmalin/Yoleonas/releases)

## Ce qui fonctionne avec le réseau Linux par défaut

Après l'installation, le NAS peut utiliser directement la connexion réseau
Linux standard pour l'administration et les fonctions de base. Il n'est pas
obligatoire de modifier immédiatement le réseau pour découvrir l'interface et
commencer à utiliser le serveur.

En revanche, dans l'état actuel du projet, la préparation des réseaux avancés
n'est pas encore automatique :

- les machines virtuelles ne peuvent pas utiliser correctement le pont LAN tant
  que le bridge Linux `br0` n'a pas été installé ;
- les conteneurs qui doivent apparaître directement sur le réseau local ne
  peuvent pas utiliser le réseau Docker externe `br0` tant que celui-ci n'a pas
  été créé ;
- les conteneurs Docker utilisant seulement les réseaux Docker classiques
  (`bridge`, `host`, etc.) ne sont pas concernés de la même manière.

Cette étape manuelle est une limite connue de la version actuelle, et non une
panne de Docker ou des machines virtuelles.

## Activer le réseau des machines virtuelles

Le script `scripts/system/lan_bro.sh` transforme la carte réseau physique en
port du bridge Linux `br0`. L'adresse IP principale du NAS est alors portée par
`br0`, ce qui permet aux machines virtuelles de rejoindre le réseau local.

### Vérifier avant de modifier

```bash
cd /chemin/vers/Yoleonas
sudo bash scripts/system/lan_bro.sh -show
```

La commande ci-dessus affiche la carte, l'adresse, la passerelle et la
configuration détectées sans rien modifier.

### Installer le bridge `br0`

> **Attention :** cette commande modifie la configuration réseau persistante et
> redémarre le NAS. Une session SSH ou l'interface web peuvent être interrompues.
> Exécutez-la de préférence avec un accès local à l'écran et au clavier du NAS,
> et notez auparavant son adresse IP, sa passerelle et ses serveurs DNS.

```bash
sudo bash scripts/system/lan_bro.sh -install
```

Après le redémarrage, contrôlez le résultat :

```bash
sudo bash scripts/system/lan_bro.sh -statut
```

Pour revenir à la configuration réseau classique sauvegardée par le script :

```bash
sudo bash scripts/system/lan_bro.sh -remove
```

Cette commande redémarre également le NAS.

## Activer le réseau LAN des conteneurs Docker

Le script `scripts/system/lan_docker.sh` détecte automatiquement le réseau du
NAS et crée un réseau Docker externe nommé `br0`, utilisant le pilote `ipvlan`
en mode L2.

Vérifiez d'abord l'état actuel :

```bash
cd /chemin/vers/Yoleonas
sudo bash scripts/system/lan_docker.sh status
```

Créez ensuite le réseau Docker :

```bash
sudo bash scripts/system/lan_docker.sh install
```

Si la détection automatique choisit une mauvaise interface, indiquez la carte
réseau manuellement, par exemple :

```bash
sudo bash scripts/system/lan_docker.sh install enp1s0
```

Les fichiers Compose qui utilisent ce réseau doivent le déclarer comme réseau
externe :

```yaml
networks:
  br0:
    external: true
```

Contrôlez enfin la configuration :

```bash
docker network ls
docker network inspect br0
```

Le bridge Linux des VM et le réseau Docker portent tous les deux le nom `br0`,
mais ce sont deux configurations distinctes. Selon l'usage du NAS, il peut être
nécessaire d'installer l'une, l'autre ou les deux.

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
