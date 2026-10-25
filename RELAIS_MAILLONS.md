# RELAIS_MAILLONS.md — passage de témoin Claude ⇄ ChatGPT

> Ce fichier est la mémoire commune de l'appli **Maillons**. Quiconque (Claude, ChatGPT ou autre) reprend le travail doit le **lire en premier** et le **mettre à jour en dernier**.
> Dépôt : `bigone1212/VTT-entretien` — branche de travail : `main`.
> **Aucun secret ici** (ni clé, ni mot de passe, ni jeton, ni adresse de base de données). Seuls les *noms* de variables d'environnement sont cités.

## 0. Mode d'emploi du relais

**Au début d'une intervention**
1. Lire ce fichier en entier, **en commençant par la section « Tâche en cours »** (juste après celle-ci), puis vérifier que le dernier commit de `main` correspond bien à la section 8 et à la section « Tâche en cours ». S'il y a des commits plus récents, lire leurs messages : quelqu'un a travaillé entre-temps, parfois sans avoir pu mettre ce fichier à jour.
2. Avant de réécrire un gros morceau de `main.py`, comparer l'ancienne et la nouvelle version : le 24/10/2026, la fiche de sortie a **disparu lors d'un assemblage** de versions (voir section 3).

**À la fin d'une intervention significative**
1. Mettre à jour les sections 2 à 8 avec ce qui est **réellement connu**.
2. Séparer clairement : **terminé**, **en cours**, **à faire**.
3. Ne jamais écrire qu'un test a réussi s'il n'a pas été fait. Ne jamais écrire qu'une modification est enregistrée ou déployée sans l'avoir vérifié (voir comment en section 8).
4. Conserver les décisions et problèmes encore utiles des interventions précédentes ; ne supprimer que ce qui est résolu ou périmé (et le dire).
5. Enregistrer ce fichier dans le dépôt, avec le reste du travail ou dans un commit séparé.

**À propos de Math (le propriétaire)** : il se dit non technicien. Réponses en français, simples, **une seule étape à la fois**. Ses règles :
1. Explications simples, en français, une étape à la fois.
2. **Pas de backslash** dans le HTML, le CSS ou le JavaScript de `main.py`.
3. Tester avant de livrer, puis lui dire ce qu'il a à faire (attendre Render, rouvrir l'appli…).
4. Pour une demande de graphisme, **ne pas changer le fonctionnement** de l'appli.
5. Ne pas toucher aux autres fichiers que `main.py` sans le lui dire.

## 0 bis. Protocole de reprise après interruption

Ce fichier ne doit **pas** être mis à jour seulement à la fin : une session peut s'arrêter à tout moment (limite d'utilisation atteinte, coupure). Pour chaque tâche significative :

1. **Avant de commencer** : écrire dans la section « Tâche en cours » l'objectif et les étapes prévues, et enregistrer sur GitHub (commit de ce seul fichier). Pas de modification de code avant.
2. **Pendant le travail** : mettre à jour la section après chaque étape importante, et en particulier après chaque modification de code **vérifiée et enregistrée**. Le plus simple : une même étape = un commit contenant le code vérifié **et** la mise à jour de ce fichier.
3. **Contenu de chaque mise à jour** : fichiers modifiés, changements réalisés, tests effectués et leur résultat, problèmes rencontrés, prochaine action.
4. **Deux listes séparées, toujours** : « Enregistré sur GitHub » (avec le numéro de commit, vérifié) et « Local, non enregistré » (modifications qui n'existent que dans l'espace de travail de la session et qui seraient perdues).
5. **Limite d'utilisation proche** : l'enregistrement d'un point de reprise fiable passe avant tout le reste. Ne rien abandonner : tout changement non enregistré est soit enregistré, soit décrit précisément dans « Local, non enregistré ». Ne jamais écraser le travail existant (relire l'état de GitHub avant d'envoyer). Du code **non testé** ne va **pas** sur `main` (Render peut le publier) : il est envoyé sur une branche séparée nommée `reprise/AAAA-MM-JJ`, dont le nom est noté dans la section « Tâche en cours » sur `main`. Elle n'est fusionnée dans `main` qu'après test.
6. **Ne jamais marquer une tâche ou une étape comme terminée** si elle ne l'est pas. États autorisés : `[ ]` à faire, `[~]` commencée ou non vérifiée, `[x]` terminée **et** vérifiée.
7. **Si une session s'est arrêtée sans mettre à jour ce fichier**, ce qui est écrit ici est le dernier état *connu*, pas forcément l'état réel. Celui qui reprend (Claude ou ChatGPT) doit **vérifier le code réel avant de poursuivre** : comparer `git log` de `main` avec le dernier commit noté ci-dessous (tout commit plus récent est un travail non consigné : lire son contenu), chercher d'éventuelles branches `reprise/…`, relire les fichiers concernés, puis corriger ce fichier avant de continuer. Les modifications « locales » d'une session interrompue sont à considérer comme perdues, sauf si elles figurent sur une branche `reprise/…`.

**Règle de publication — décidée par Math le 25/10/2026, valable pour Claude et ChatGPT**
- **Toute modification du code de l'appli** (`main.py`, `db.py`, `gpx_import.py`, `requirements.txt`) est d'abord enregistrée et testée sur une **branche séparée** `reprise/AAAA-MM-JJ` (on peut ajouter un mot sur le sujet, ex. `reprise/2026-10-26-filtre-zone`). Cette branche sert aussi de point de reprise.
- **Fusion dans `main` uniquement avec l'accord explicite de Math**, après test. Render se met à jour à partir de `main` (mise à jour automatique non vérifiée) : fusionner dans `main` revient peut-être à **déployer**. Aucun déploiement sans l'accord de Math.
- Les mises à jour de **ce fichier seul** (journal, état, décisions) vont directement sur `main`, car elles ne changent pas l'appli. Elles peuvent provoquer un redéploiement identique (voir section 8).
- Avant de fusionner : relire l'état de `main` (quelqu'un d'autre a pu y enregistrer du travail) et ne rien écraser.
- Ne pas modifier d'autres fichiers que ceux prévus par la tâche sans en informer Math.

*Limite connue : le numéro d'un commit n'existe qu'une fois ce commit créé ; il ne peut donc pas figurer dans le fichier qui l'accompagne. Il est ajouté à la mise à jour suivante. Entre-temps, `git log` fait foi.*

## Tâche en cours

Statut : EN COURS
Objectif : demande de Math en 4 temps, traités un par un. Temps 1 (pastille) et temps 2 (couleurs) terminés et dans `main`. Temps 3 (maintenant) : choix des unités dans Réglages (distance km/mi, altitude m/ft, vitesse km/h/mph, température °C/°F, heure 24 h/12 h). Temps 4 : profil d'altitude de l'onglet Sorties un peu plus haut pour les longues distances.
Démarrée : 25/10/2026, vers 20h20 — par : Claude
Décision de conception (temps 3) : changement d'AFFICHAGE seulement. Les données restent en km et en mètres dans la base. En métrique (défaut), les textes affichés doivent rester identiques à avant. Les valeurs que Math saisit lui-même (kilométrage total du vélo, seuils d'usure, kilométrage d'une intervention) restent en km, volontairement : convertir en miles puis reconvertir ferait glisser son vrai kilométrage. À proposer plus tard si besoin.
Étapes prévues (temps 3) :
  [~] 1. Repérer les affichages dans le `main.py` actuel : première lecture effectuée, plusieurs affichages métriques repérés ; inventaire détaillé en cours
  [ ] 2. Ajouter la carte « Unités » dans Réglages (clé `maillon_unites`) et les fonctions de conversion
  [ ] 3. Brancher les affichages, tester (métrique identique à avant ; impérial correct), enregistrer sur `reprise/2026-10-25-unites`
  [ ] 4. Fusion dans `main` seulement avec l'accord explicite de Math
Enregistré sur GitHub : démarrage de tâche à consigner par ce commit de relais
Local, non enregistré : rien
Fichiers modifiés : `RELAIS_MAILLONS.md` uniquement pour enregistrer le démarrage ; aucun code de l'application modifié
Tests : non faits pour cette tâche
Problèmes rencontrés : aucun constaté pour l'instant
Prochaine action : terminer le repérage précis des affichages et points d'intégration dans `main.py`.
Dernier commit de main connu : `e60656b` avant ce commit de relais    Branche de reprise : `reprise/2026-10-25-unites` (à créer après l'enregistrement de ce journal)

À remplir dès qu'une tâche significative démarre, puis à effacer (en reportant l'essentiel en sections 2 à 8) quand elle est **terminée et vérifiée**. Modèle :

```
Statut : EN COURS / BLOQUÉE / TERMINÉE
Objectif : …
Démarrée : date et heure — par : Claude ou ChatGPT
Étapes prévues :
  [ ] 1. …
  [~] 2. … (commencée, non vérifiée)
  [x] 3. … (terminée et vérifiée)
Enregistré sur GitHub : étape → commit (vérifié le … )
Local, non enregistré : fichiers et nature des changements (ou « rien »)
Fichiers modifiés : …
Tests : fait / résultat — non fait
Problèmes rencontrés : …
Prochaine action : …
Dernier commit de main connu : …    Branche de reprise éventuelle : …
```

## 1. État du projet

**Maillons** : appli personnelle de suivi d'entretien du VTTAE de Math (CUBE Stereo Hybrid ONE44).

*Vérifié dans le code le 25/10/2026 :*
- Technique : FastAPI. Fichiers : `main.py` (≈ 12 400 lignes, ≈ 530 Ko, contient aussi tout le HTML/CSS/JS), `db.py` (107 lignes), `gpx_import.py` (482 lignes), `requirements.txt` (`fastapi`, `uvicorn`, `pydantic`, `psycopg[binary]`, **sans numéros de version**).
- Base de données (`db.py`) : PostgreSQL si la variable `DATABASE_URL` commence par `postgres://` ou `postgresql://` (Neon en ligne), sinon SQLite (fichier défini par `SQLITE_PATH`, par défaut `vtt_entretien_v3.db`).
- Accès protégé par un code : variable d'environnement `CODE_ACCES`, réglée côté Render. Si elle n'est pas définie, le serveur répond « Code d'accès non réglé » (erreur 503).
- 4 onglets présents dans le code : **Entretien**, **Historique**, **Réglages**, **Sorties**.
- Routes `/api/…` présentes : état, historique (ajout, modification, suppression, suppression multiple), synchronisation, remplacement/seuils/coefficients/position/suppression de composants, « mes réglages » (fiche technique), réglages, sorties (import, liste, suppression, terrain, modification, nom, surfaces, vidéos, photos), sauvegarde.
- Services externes appelés par l'appli : fonds de carte OpenStreetMap et Esri (satellite), Open-Meteo (météo), Overpass (plusieurs serveurs, noms de lieux 3D), Nominatim (nom de commune des zones).
- Stockage sur le téléphone (localStorage), clés : `maillons_favoris`, `maillons_vus`, `maillons_zones_noms`, `maillon_theme`, `maillon_mode`.

*Fonctions décrites par Math et visibles dans l'historique des commits (non re-testées lors de la création de ce fichier) :* Historique (filtre, calendrier), Réglages (fiche technique du vélo, apparence), Sorties (import GPX et Strava via le menu trois points ; filtres période / distance / favoris / zone géographique), pastilles rouges de nouveautés, fiche de sortie (carte satellite, profil, météo), vue 3D avec noms de lieux et panneaux de pente, favoris, partage d'une image de la sortie, export GPX.

## 2. Dernière intervention

**Intervention 5 — 25/10/2026 : reprise des unités d'affichage sur branche de test**
- Branche : `reprise/2026-10-25-unites`. Code de l'application modifié uniquement dans `main.py`, en 5 commits ; dernier commit `7c9ff2086de8c82ff25b306e736f0b77a20c57b2`.
- Ajout des réglages d'unités distance (km/mi), altitude/dénivelé (m/ft), vitesse (km/h/mph), température (°C/°F) et heure (24 h/12 h), conservés dans `localStorage` (`maillon_unites`). Conversions d'affichage ; valeurs enregistrées et saisies pour l'entretien restent dans les unités de stockage existantes.
- Vérifications statiques faites : valeurs métriques par défaut, options autorisées, lecture/écriture localStorage, conversion du filtre miles vers km, formules de conversion et maintien du kilométrage d'entretien en km. Calculs numériques de conversion contrôlés séparément.
- **Non vérifié** : syntaxe complète Python/JavaScript dans un environnement d'exécution, démarrage de l'application, navigation navigateur/mobile, affichage réel, conservation visuelle exacte du rendu métrique. Aucun déploiement déclenché.
- État Git : branche de reprise en avance de 5 commits et en retard de 2 commits sur `main`. Le diff de code consulté concerne `main.py` ; les deux commits de retard doivent être comparés avant toute PR/fusion. Le code n'est pas fusionné dans `main`.
- Temps 4 (profil d'altitude plus haut pour les longues distances) **non commencé**, à traiter après validation du temps 3.
- Temps 1 : pastille rouge des onglets plus petite (`67e6a92`, dans `main`). Temps 2 : choix des couleurs de l'appli (`41f2129`), fusionné dans `main` avec l'accord de Math.

**Intervention 3 — 25/10/2026 : règle de publication**
- Math a validé : modifications de code sur branche `reprise/…` d'abord, fusion dans `main` seulement avec son accord (détail en section 0 bis). ChatGPT a confirmé le même jour avoir accès au dépôt et avoir lu ce fichier (déclaration de Math, non vérifiée par Claude).
- Fichier modifié : `RELAIS_MAILLONS.md` uniquement. Aucun code de l'appli modifié. Aucun déploiement déclenché volontairement.
- État des branches constaté sur GitHub : `main` et `feature/fiche-technique-velo` (**non examinée** ; contenu et utilité inconnus). Aucune branche `reprise/…`.

**Intervention 2 — 25/10/2026 (après la création du fichier) : protocole de reprise après interruption**
- Objectif : permettre la reprise du travail même si une session s'arrête en pleine tâche (limite d'utilisation).
- Fichier modifié : `RELAIS_MAILLONS.md` uniquement (ajout des sections « 0 bis » et « Tâche en cours », mise à jour des sections 0, 2, 6, 8). **Aucun autre fichier. Aucun déploiement déclenché volontairement.**
- Vérifié avant modification : dépôt local et GitHub identiques au commit `6ac4728`, aucune modification locale en attente.
- Décision à signaler : le code non testé, en cas de limite proche, va sur une branche `reprise/AAAA-MM-JJ` et non sur `main` (voir 0 bis, point 5). Choix fait pour ne pas risquer de publier du code non testé ; Math peut le modifier.
- Aucune branche `reprise/…` n'existe à ce jour.

**Intervention 1 — 25/10/2026 : création du fichier de relais (commit `6ac4728`)**
- Objectif : mettre en place ce fichier de relais.
- Fichier créé : `RELAIS_MAILLONS.md` (racine). **Aucun autre fichier modifié. Aucun déploiement déclenché.**
- Méthode : lecture du dépôt (clone, historique des 40 derniers commits, code), vérifications de la section 4.
- Limite : ce fichier est reconstruit **à partir du dépôt** (code et messages de commit), pas à partir des conversations passées avec Claude ou ChatGPT, auxquelles cette session n'a pas eu accès.

**Dernières interventions sur l'appli (d'après l'historique git ; seul `main.py` est modifié dans ces 40 commits, 39 d'entre eux le touchant)**

| Date | Auteur du commit | Résumé |
|---|---|---|
| 25/10, 19:44 | Claude | Pastille rouge des onglets un peu plus petite (`67e6a92`) |
| 25/10, 13:42 | Claude | Menus trois points : ouverture vers le haut en bas d'écran ; sélecteur d'apparence : texte blanc sur l'élément actif (`1dfff95`) |
| 25/10, 08:46 → 09:23 | Claude | Favoris (cœur, filtre) ; partage en image façon Komoot ; export GPX corrigé (altitude et heure) ; partage par lien secret **ajouté puis retiré** (`4264ffc` → `4c973ac`) |
| 24/10, 21:08 → 21:53 | Claude | Filtre distance ; imports GPX/Strava dans le menu trois points ; filtre par zone (carrés de 0,25°, puis nom de commune via Nominatim) ; cercle d'attente en 3D ; serveurs de secours pour les noms de lieux ; **restauration de la fiche de sortie** (`1fccd8c`) |
| 24/10, 18:07 → 18:59 | bigone1212 (compte du propriétaire du dépôt ; très probablement les modifications faites avec ChatGPT) | Nombreux ajustements des noms de lieux et du survol 3D, terminés par `08af933` : retour exact à la version `7bd9fe4` de la recherche de lieux |
| 24/10, 13:54 → 15:07 | bigone1212 | Fiche technique du vélo (composants CUBE 2025, icônes), repères de pente et noms de lieux en 3D, fusion fiche vélo + repères 3D |

*Le lien auteur ↔ outil est une déduction : les commits signés « Claude » que j'ai ouverts portent la mention Co-Authored-By Claude ; le commit « bigone1212 » que j'ai ouvert (`08af933`) n'en porte pas. Les messages de commit disent ce qui a été voulu, pas que cela a été testé.*

## 3. Décisions techniques

À ne pas remettre en cause sans raison.

1. **Architecture en un seul fichier** (`main.py` contient HTML, CSS et JavaScript). Choix de Math ; ne pas découper sans demande. Conséquence : tout changement visuel touche `main.py`.
2. **Pas de backslash dans le HTML/CSS/JS de `main.py`.** Vérifié le 25/10/2026 : une seule occurrence dans tout le fichier, ligne 68, dans du code Python (`b"\xff\xd8\xff"`, reconnaissance d'une image JPEG). À conserver tel quel.
3. **Base à deux modes** (SQLite en local, PostgreSQL en ligne selon `DATABASE_URL`) via `db.py`. Un commit antérieur (`b99b5f1`) évite des « deadlocks » à l'initialisation PostgreSQL : ne pas modifier l'initialisation sans nécessité.
4. **Partage = image uniquement.** Un partage par lien secret (page publique) a été développé puis **volontairement retiré** (`4264ffc` puis `4c973ac`, message : « le partage reste une image »). La raison exacte n'est pas écrite dans le dépôt. Ne pas le réintroduire sans demande explicite de Math.
5. **Noms de lieux en 3D : zone fragile.** Plusieurs essais successifs ont eu lieu le 24/10 (commits `7bd9fe4` à `08af933`), terminés par un retour exact à `7bd9fe4`, puis ajout de serveurs de secours (`30537f4`). La version actuelle fonctionne d'après les derniers commits, mais **je n'ai pas pu le vérifier**. Ne pas retoucher sans raison.
6. **Zones géographiques** : recherche par carrés de 0,25° ; nom de la commune via Nominatim, avec cadence limitée (le code rappelle que Nominatim demande au plus une requête par seconde) ; noms gardés sur l'appareil (`maillons_zones_noms`). Choisi parce que plus rapide qu'Overpass (message de `fce5735`).
7. **Favoris gardés sur le téléphone** (`maillons_favoris`), pas dans la base. Conséquence : non synchronisés entre appareils.
8. **Leçon de l'assemblage perdu** : la fiche de sortie (carte, profil, statistiques, météo) a disparu lors d'un assemblage de versions et a dû être remise (`1fccd8c`, +544 lignes). Avant et après tout assemblage, vérifier qu'aucune fonction n'a disparu.

## 4. Tests

**Réellement effectués le 25/10/2026, sur le commit `67e6a92`**
- Syntaxe Python de `main.py`, `db.py`, `gpx_import.py` : **OK** (analyse de la syntaxe sans exécution).
- Recherche de backslash dans `main.py` : 1 occurrence, hors HTML/CSS/JS (voir section 3).
- Recherche simple de secrets écrits en dur dans les fichiers `.py` (motifs mot de passe / jeton / clé / secret suivis d'une valeur) : **aucun trouvé**. Limites : ne couvre ni l'historique git ni tous les motifs possibles.
- Fichiers modifiés dans les 40 derniers commits : `main.py` uniquement.

**Non effectués (et donc à ne pas supposer réussis)**
- **Démarrage de l'appli** : impossible dans l'environnement de cette session (installation de FastAPI impossible, pas d'accès au réseau pour les paquets).
- Tests dans un navigateur, sur téléphone, et sur Render.
- Test des fonctions décrites en section 1 (import GPX/Strava, 3D, partage d'image, filtres, favoris…).
- Les tests faits lors des interventions précédentes **ne sont pas consignés** dans le dépôt : on ne peut ni les confirmer ni les infirmer.
- Il n'existe **aucun test automatique** dans le dépôt.

## 5. Problèmes connus

| Sujet | État | Statut de la cause |
|---|---|---|
| Noms de lieux 3D (Overpass) | Nombreuses retouches le 24/10 ; état actuel non vérifié ici | Cause suspectée, **non confirmée** : indisponibilité ou lenteur des serveurs Overpass publics (c'est ce que disent les messages de commit, ex. « éviter les échecs Overpass ») |
| Cadence Nominatim pour les zones | Liste construite progressivement ; peut être lente avec beaucoup de sorties | Limite du service (1 requête/seconde, indiquée dans le code), **confirmée par le code** |
| `requirements.txt` sans versions | Un déploiement peut installer des versions plus récentes que celles utilisées jusqu'ici | **Constat vérifié** ; aucun incident connu lié à cela |
| Pas de `.gitignore` | Si l'appli est lancée en local dans le dossier du dépôt, le fichier SQLite pourrait être ajouté par erreur à un commit | **Constat vérifié** ; ne pas faire `git add .` sans contrôle |
| Aucun problème en cours signalé dans le dépôt | — | Les problèmes constatés par Math dans les conversations ne sont pas visibles ici : **les ajouter dans ce tableau** |

## 6. Travail restant

- **En cours** : temps 3, unités d'affichage, sur `reprise/2026-10-25-unites`. Vérifications statiques partielles effectuées ; pas de test d'exécution ou de navigateur.
- **Prochaine étape** : exécuter des contrôles Python/JavaScript et tester l'application en navigateur/ordinateur/téléphone si l'environnement le permet. Si ces tests ne sont pas possibles ici, consigner la limite et demander à Math un essai guidé avant fusion. Ne pas fusionner sans accord explicite.
- **Ensuite** : temps 4, remonter légèrement le profil d'altitude de l'onglet Sorties pour les longues distances, seulement après validation des unités.
- **À faire (sans urgence)** : examiner la branche `feature/fiche-technique-velo` par rapport à `main`, en lecture seule, avant toute décision.

## 7. Précautions

**Fonctionnalités à préserver** (d'après la liste de Math) : onglets Entretien, Historique (filtre, calendrier), Réglages (fiche technique, apparence), Sorties (import GPX et Strava, filtres période / distance / favoris / zone), pastilles rouges de nouveautés, fiche de sortie (carte satellite, profil, météo), vue 3D (noms de lieux, panneaux de pente), favoris, partage d'image, export GPX, code d'accès, sauvegarde (`/api/sauvegarde`).

**Zones à ne pas modifier sans nécessité**
- Recherche des noms de lieux en 3D (voir section 3, point 5).
- Initialisation de la base et `db.py` (deadlocks PostgreSQL déjà rencontrés).
- Contrôle du code d'accès (`verifier_auth`).
- Données existantes : ne jamais supprimer ni migrer sans accord de Math (la base en ligne contient son historique d'entretien et ses sorties).

**Règles de sécurité**
- Ne jamais écrire dans le dépôt de clé, mot de passe, jeton, `DATABASE_URL` ni valeur de `CODE_ACCES`.
- Risque de régression principal : fichier unique de plus de 12 000 lignes ; une réécriture complète peut faire disparaître des fonctions sans erreur visible (déjà arrivé).
- Une demande de graphisme ne doit pas changer le fonctionnement.

## 8. État GitHub / Render

- **Dernier commit connu avant la reprise actuelle : `e60656b6cef866116331646d6a29f2c799c2d9c4`** (25/10/2026, message « Relais : tâche en cours (choix des unités) »). Vérifié sur GitHub au début de la reprise.
- Commits de relais : `6ac4728` (création du fichier) et `b8ef78e` (protocole de reprise), tous deux vérifiés sur GitHub le 25/10/2026. Le commit de l'intervention 3 (règle de publication) vient **après** : son numéro n'est pas connu au moment de l'écriture, consulter l'historique de `main` sur GitHub.
- Dernier commit de **l'appli** (code) : `67e6a92`. Les commits de relais ne modifient que ce fichier.
- Branches vérifiées sur GitHub au début de la reprise : `main`, `feature/fiche-technique-velo` (non examinée), `reprise/2026-10-25-couleurs`. Aucune branche `reprise/2026-10-25-unites` n'existait alors.
- Chaque envoi sur `main` peut déclencher une mise à jour de Render si la mise à jour automatique est active (non vérifié) : un commit qui ne modifie que ce fichier ne change pas l'appli, mais peut provoquer un redéploiement identique.
- **Render : correspondance NON vérifiée.** Cette session n'a pas accès au tableau de bord Render et l'adresse de l'appli n'est pas dans le dépôt. D'après Math, Render se met à jour à partir de GitHub ; on ne sait pas ici si la mise à jour automatique est active, ni quel commit est actuellement en ligne.
- **Comment vérifier la prochaine fois** : dans Render, ouvrir le service puis l'onglet des déploiements ; comparer le commit affiché avec le dernier commit de `main` sur GitHub. Noter ici le résultat avec la date.
