YOLEO AGENT WINDOWS - VERSION 1.0.0
===================================

Projet : WinForms .NET 8, autonome win-x64, sans package externe.
Rôle   : agent de surveillance dans la zone de notification Windows.

Fonctionnement
--------------
- Premier lancement : fenêtre HTTPS/P12/identifiants. Le bouton Tester est
  facultatif ; OK enregistre toujours les champs et ferme immédiatement la fenêtre.
- Sans test préalable, l'agent tente ensuite de créer son jeton en arrière-plan.
  Une configuration incorrecte reste enregistrée et produit simplement un échec
  de connexion, sans rouvrir ni bloquer la fenêtre.
- Double clic sur l'icône : ouvre l'interface https://.../index dans le navigateur.
- Clic droit : Authentification, Options de notification, Vérifier maintenant,
  Ouvrir Yoleo et Quitter.
- Une requête GET /api/v1/monitoring/snapshot par intervalle configuré.
- Alertes Windows uniquement sur changement d'état ou franchissement de seuil.
- Les options sont réparties en trois onglets : Général, Stockage et baies,
  Services et travaux.
- Les points de montage à surveiller sont choisis dans l'onglet Stockage. Leur
  sélection est indépendante de disk_top.conf et reste dans le registre Windows.
- Un chemin sélectionné doit rester un vrai montage Linux. Le passage à un
  simple dossier local, à un dossier contenant des données ou à un chemin absent
  déclenche une seule alerte, réarmée lorsque le montage revient.
- Un clic sur une alerte ouvre sa page : stockage vers /disk/general, Docker
  vers /docker/containers, Samba vers /partage/samba, tâches vers /system/task,
  build vers /build/main et entretien du registre vers /build/registry.
- Aucun lien avec le système Web Push du navigateur.

Registre Windows
----------------
Tous les réglages et le dernier état sont enregistrés sous :
  HKEY_CURRENT_USER\Software\Sftpmalin\YoleoAgent

Le jeton API et le mot de passe du P12 sont chiffrés avec DPAPI pour l'utilisateur
Windows courant avant leur écriture dans le registre. Le mot de passe du compte
serveur n'est jamais enregistré.

Compatibilité du certificat client Windows
-------------------------------------------
La clé privée du P12 est chargée dans le magasin cryptographique de l'utilisateur
Windows pendant son utilisation. Le mode de clé éphémère ne doit pas être utilisé :
Schannel le refuse pour l'authentification mTLS avec l'erreur
"the platform does not support ephemeral keys".

Compilation de test
-------------------
  dotnet run --project tools\IconGenerator\IconGenerator.csproj -- Assets\Logo.png Assets\App.ico
  dotnet publish YoleoAgent.csproj -c Release -r win-x64 --self-contained true

Installation MSI
----------------
Le fichier dist\YoleoAgent.msi installe l'agent 64 bits dans Program Files,
ajoute un raccourci au menu Démarrer et lance automatiquement l'agent à chaque
ouverture de session Windows. Le MSI ne contient aucun P12, mot de passe, jeton,
URL de serveur ni réglage utilisateur.
