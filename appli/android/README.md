# Yoleo NAS Android

Application Android native de Yoleo. La version 0.6.7 contient :

- une configuration HTTPS/mTLS avec sélection d'un P12 ;
- un test facultatif en quatre étapes : P12, HTTPS, identifiants, jeton ;
- le stockage privé du P12 dans le bac à sable Android ;
- le chiffrement du mot de passe P12 et du jeton avec Android Keystore ;
- aucun enregistrement du mot de passe du compte serveur ;
- un tableau de bord natif alimenté par `GET /api/v1/capabilities` puis
  `GET /api/v1/monitoring/snapshot` ;
- une navigation native horizontale avec les icônes du menu Yoleo et un menu
  compact Actualiser, Authentification et Réglages ;
- des vues Accueil, Docker, Stockage, Tâches, Fichiers, Machines virtuelles et Backup ;
- le téléchargement HTTPS puis la mise en cache privée des icônes Docker ;
- les commandes Docker démarrer, arrêter et redémarrer, avec les actions
  impossibles grisées ;
- les commandes Tâches démarrer et arrêter ;
- les commandes Backup démarrer et arrêter, regroupées comme la page Yoleo en
  Backup, Miroir, Archive et Cache, sans création ni modification de script ;
- les commandes VM démarrer, arrêt propre et arrêt forcé,
  sans aucune fonction de création ou de suppression ;
- un rafraîchissement à chaque retour visible dans l'application ;
- l'affichage du dernier cliché en heure française ;
- des réglages natifs en cinq rubriques pour sélectionner exactement les
  montages affichés et surveillés, régler les seuils et choisir les types
  d'alertes ;
- une surveillance différée par Android JobScheduler, sans service permanent :
  serveur hors ligne/retour en ligne, CPU, RAM, stockage, montage perdu, Docker,
  Samba, tâche en échec, build en attente et rappel de registre ;
- une mémoire locale des alertes avec un délai de 24 heures pour empêcher une
  même notification de revenir à chaque contrôle.
- un enregistrement synchrone et vérifié des réglages, indépendant d'une
  éventuelle erreur du planificateur Android/Samsung.
- une actualisation toutes les trois secondes après une commande Tâche ou
  Backup, limitée à la page visible et arrêtée dès que l'exécution est finie.
- une surveillance continue de la page Tâches ou Backup tant qu'au moins un
  élément est encore en cours, même s'il a été lancé avant l'ouverture ;
- les filtres Backups, Archives et Cache, comme la page Yoleo ;
- le regroupement visuel des conteneurs Docker par stack ;
- un onglet de réglages « Onglets » pour enregistrer librement l'ordre du menu
  inférieur, avec Fichiers et Backup avant VM par défaut ;
- un explorateur NAS natif confiné aux racines annoncées par l'API : dernier
  dossier mémorisé, navigation, nouveau dossier, renommer, supprimer,
  copier/couper/coller, envoi depuis le téléphone et téléchargement Android ;
- une actualisation CPU/RAM toutes les trois secondes uniquement lorsque
  l'accueil est visible ;
- l'évaluation immédiate des alertes au lancement, lors d'une actualisation
  manuelle et après l'enregistrement des réglages, en plus du JobScheduler.
- une barre d'outils fixe et compacte pour l'explorateur de fichiers, sans
  répétition du chemin ni des titres ;
- la suppression du double titre et de l'ancien bandeau de chargement dans le
  gestionnaire de fichiers après réception de la liste.
- un onglet Réglages > Accueil pour choisir quinze groupes d'informations :
  CPU, RAM, stockage, températures, ventilateurs, GPU, hôte, réseau, uptime,
  services systemd, Docker, Samba, tâches, VM et builds.
- un accueil matériel compact : jusqu'à trois ventilateurs par ligne, puis
  uniquement la température générale du CPU et celle de la carte mère ;
- aucune synchronisation de dossiers Android : l'application reste centrée sur
  la surveillance et la télécommande du NAS.
- le téléchargement ponctuel d'un fichier ou d'un dossier depuis son menu :
  Android crée automatiquement `Download/Yoleo`, et un dossier NAS est reçu
  sous forme d'archive ZIP sans réintroduire de synchronisation.
- une surveillance en arrière-plan renforcée : le JobScheduler reste présent,
  complété par une alarme Android ponctuelle et non exacte autorisée pendant
  Doze, sans service permanent ;
- une demande unique d'exclusion de l'optimisation batterie et une notification
  unique confirmant la réussite du premier véritable contrôle en arrière-plan.

Le projet n'embarque aucune URL, aucun utilisateur, aucun mot de passe, aucun
jeton et aucun certificat. Les champs sont vides à la première installation.

## Compilation

Le SDK Android doit être indiqué dans `local.properties` ou par
`ANDROID_HOME`. Puis :

```powershell
.\gradlew.bat assembleDebug
```

APK produit : `app\build\outputs\apk\debug\app-debug.apk`.

Copie prête à installer pour les essais :
`dist\YoleoNAS-0.6.7.apk`.
