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

Le dossier `init` est volontairement généré localement et n'est pas stocké sur
GitHub, afin de ne pas gonfler inutilement le dépôt. Le dossier `offline` est
également facultatif : s'il n'existe pas, `system.sh` télécharge les dépendances
Python nécessaires depuis Internet.
