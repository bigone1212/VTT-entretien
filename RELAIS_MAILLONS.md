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

REPRISE PAR CHATGPT — 28/10/2026 : test direct de l'API de sauvegarde multi-vélos.
- Objectif : vérifier que `GET /api/sauvegarde` renvoie le kilométrage courant de `velos`, même lorsque l'ancienne table `velo` contient une valeur différente.
- Branche de test actuelle : `reprise/2026-10-27-correction-sauvegarde`, basée sur `reprise/2026-10-26-multivelos-completion`. Ne pas fusionner ni déployer sans accord explicite de Math.
- Correction applicative enregistrée : `main.py`, commit `b90143b8c8308f713a5bbf533c183d47b417ae86`. `/api/sauvegarde` lit désormais `velos.km_total` pour le CUBE (id 1), avec repli vers `velo.km_total` si la nouvelle valeur est absente.
- Test HTTP enregistré dans `.github/workflows/maillons-check.yml` : commit initial `0f8a8bb262a1321ea759a1d60587c374d05b0d37`, puis correction `54f21fa1c27c18a600c3ef73d368c5e8ea68f1a2`. Le test initialise le vrai schéma sur une base SQLite temporaire, puis fixe `velos.km_total = 2831` et `velo.km_total = 2700`, appelle réellement `GET /api/sauvegarde` via TestClient et vérifie le code HTTP 200, le champ JSON `km_total` et le kilométrage de la liste `velos`.
- Résultat vérifié : GitHub Actions run `40647046003`, terminé avec succès. Journal : `OK: l'API /api/sauvegarde exporte 2831 km depuis velos, pas les 2700 km de l'ancienne table velo.` L'ensemble des autres étapes du workflow a également réussi. Lien : https://github.com/bigone1212/VTT-entretien/actions/runs/40647046003
- Le premier essai du test, run `40646891336`, avait échoué parce qu'il créait un schéma incomplet avant l'import de `main`. Le test a été corrigé pour laisser `init_db()` créer le schéma réel ; l'échec concernait le montage du test, pas la route de sauvegarde.
- Enregistré sur GitHub : correction `main.py`, test du workflow et présent relais, tous uniquement sur la branche de test. Local, non enregistré : rien.
- Limites : test réalisé sur SQLite temporaire et sur l'API locale via TestClient ; aucun test sur Neon, Render, téléphone ou base de production. Aucune donnée de production n'a été modifiée.
- Prochaine action : présenter le bilan à Math. Ne pas fusionner cette branche dans la PR n° 7 ni dans `main` sans confirmation explicite portant sur la fusion. Aucun déploiement n'a été déclenché.
- Note : le reste de ce fichier conserve l'historique des étapes multi-vélos antérieures ; les commits réellement vérifiés ci-dessus font foi.

REPRISE PAR CHATGPT — 26/10/2026 :
- Objectif : valider la migration C3(a) « Plusieurs vélos » sur des moteurs SQLite et PostgreSQL de test avant toute fusion.
- État GitHub : branche de code `reprise/2026-10-26-plusieurs-velos`. Elle reste divergente de `main` ; le dernier comparatif avait 18 commits en avance et 4 en retard. Ne pas fusionner automatiquement.
- Tests réussis : workflow GitHub Actions `40352096393`, commit de branche `42a16b17f86d36ce67367bb022939e003715c959`, job `121674601397`. Vérifiés dans les journaux : syntaxe de `main.py`, installation des dépendances, véritable démarrage de l'application sur SQLite temporaire et PostgreSQL 16 éphémère, conservation des données historiques (2700 km, dates, composant, intervention, sortie, réglage), rattachement des anciens composants/sorties au CUBE id 1, puis deuxième démarrage sans doublons.
- Incident de test corrigé : le premier essai PostgreSQL `40351998345` a échoué à cause de mauvais indices dans les assertions du test (les valeurs migrées étaient bien présentes). Les indices ont été corrigés dans le workflow ; l'exécution suivante a réussi. Aucun échec applicatif identifié par cet incident.
- Enregistré sur GitHub : workflow de test `.github/workflows/maillons-check.yml` sur la branche de reprise, commit `42a16b17f86d36ce67367bb022939e003715c959`. Mise à jour du relais de la branche de reprise à enregistrer après ce bilan.
- Local, non enregistré : rien.
- Fichiers modifiés pour cette étape : `.github/workflows/maillons-check.yml` sur la branche de test uniquement. Aucun changement à `main.py`, `db.py` ou `requirements.txt`.
- Limites : PostgreSQL testé dans un conteneur temporaire GitHub Actions, pas sur Neon ; tests navigateur, téléphone et Render non faits. Aucun accès ni changement à la base de production, aucune fusion de code ni déploiement de code.
- Prochaine action : passer à la prochaine sous-étape C3(b/c) seulement après avoir revu précisément le périmètre et les dépendances de l'interface multi-vélos ; conserver la migration validée séparément. Fusion dans `main` uniquement avec l'accord explicite de Math.


REVUE PAR CLAUDE — 26/10/2026 (soir), à la demande de Math, de la branche `reprise/2026-10-26-plusieurs-velos` (tête 5b63ff3, lecture seule d'abord) : code globalement sain (table `velos`, `velo_id` sur composants et sorties, sauvegarde complète ; l'ancienne table `velo` reste la référence ; aucun backslash). 3 points : (1) le CUBE doit avoir EXPLICITEMENT l'id 1 (aujourd'hui il l'a seulement parce qu'il est le premier créé ; sur PostgreSQL, une première initialisation échouée puis rejouée pourrait lui donner l'id 2 et détacher pièces et sorties) ; (2) le type du CUBE est « electrique », qui ne correspond pas aux 8 types décidés avec Math : à régler à l'étape « ajout d'un vélo » ; (3) le fichier `.github/workflows/maillons-check.yml` (tests automatiques GitHub, aucun secret) est un fichier hors code de l'appli : Math doit décider s'il va dans `main` ou reste sur la branche. À noter aussi : `velos` n'est pas encore dans /api/sauvegarde.
  Math a dit « vas y » pour corriger le point (1) sur cette branche de ChatGPT. FAIT par Claude le 26/10/2026 : commit `c79f61e81fd9a2d59f6833a12b6bf77ebd4cd43e` sur `reprise/2026-10-26-plusieurs-velos` (enregistré, vérifié sur GitHub ; avance de `5b63ff3` sans conflit ; `main.py` seulement, 16 ajouts / 3 suppressions, aucun backslash ajouté) : le CUBE est inséré avec `id = 1` explicite (`ON CONFLICT DO NOTHING`) et, sur PostgreSQL seulement, la séquence est avancée par `setval(pg_get_serial_sequence('velos','id'), GREATEST(MAX(id),1))`. Tests RÉELLEMENT faits : (a) syntaxe OK ; (b) SQLite, `init_db()` exécutée pour de vrai (fausses pièces de FastAPI, installation impossible dans l'espace de Claude) : base neuve x2 démarrages = 1 seul vélo id 1, 30 composants et 2 historiques rattachés, pas de doublon ; ancienne base (créée par le code de `main`) avec 3012 km, 0,4 km_frac et 1 sortie = km, date d'achat, historique et sortie conservés et copiés dans `velos`, `velo_id` = 1 partout, 2 démarrages sans doublon ; ajout d'un 2e vélo sans id = id 2 ; (c) GitHub Actions, exécutions `40383985867` et `40383988168` sur `c79f61e` : toutes les étapes en succès, dont « Démarrer Maillons sur PostgreSQL temporaire et vérifier la migration réelle » ; le texte des journaux n'a pas pu être lu par Claude, seul le statut des étapes l'a été. NON testé : l'ajout d'un 2e vélo sur PostgreSQL (le `setval` s'exécute au démarrage sans erreur, mais l'id suivant n'a pas été vérifié), la base Neon réelle, navigateur, téléphone, Render. Rien fusionné dans `main`. Restent ouverts : points (2) et (3) ci-dessus et ajout de `velos` à /api/sauvegarde.

REVUE PAR CLAUDE — 26/10/2026 (soir) de la branche `reprise/2026-10-26-multivelos-completion` (tête `7be6118`, PR n° 7 en brouillon ; elle contient `multivelos-base-main`, ne contient PAS `plusieurs-velos` ni le correctif d'id `c79f61e`). ⚠ NE PAS FUSIONNER TELLE QUELLE : BUG DE PERTE DE KILOMÈTRES, reproduit.
  - Cause (lecture du code + reproduction réelle sur SQLite, `init_db()` exécutée avec de fausses pièces de FastAPI) : à CHAQUE démarrage, `init_db` recopie sans condition `velo` (ancienne table) vers `velos` (km_total, km_frac, km_maj_le, date_achat du CUBE, environ l. 519-526), alors que sur cette branche les kilomètres vivants ne sont écrits que dans `velos` (`ajuster_km_total`, `/api/sync`). Reproduit : 2700 km -> sortie de +100 km (2800) -> redémarrage -> retour à 2700 et km_maj_le remis à l'ancienne valeur. Conséquence probable en ligne : à chaque redémarrage de Render, le compteur du CUBE retourne à la valeur du jour de la migration (usure faussée, sorties déjà importées mal comptées). Non vu par les tests GitHub : ils démarrent deux fois sans ajouter de kilomètres entre-temps.
  - Correction proposée (faite, voir ligne suivante) : ne copier `velo` vers `velos` QU'UNE SEULE FOIS (marqueur dans `reglages`, comme `migration_composants_v2`, ou seulement si la ligne CUBE vient d'être créée) ; ajouter au test GitHub « ajouter des kilomètres puis redémarrer = kilomètres conservés » ; garder `velo` en lecture seule de secours. En plus : reporter le correctif d'id 1 (`c79f61e`) et l'ajout de `velos` à /api/sauvegarde si absent.
  - CORRIGÉ le 26/10/2026 par Claude (Math a dit oui) : commit `90ae37c49f4369600500f07979df2f5e89adb27c` sur `reprise/2026-10-26-multivelos-completion` (enregistré, vérifié sur GitHub ; avance de `7be6118` sans conflit ; `main.py` + `.github/workflows/maillons-check.yml`, aucun backslash ajouté dans `main.py`). Changements : (1) copie `velo` -> `velos` UNE SEULE FOIS grâce au marqueur `migration_velos_copie_v1` (table `reglages`) ; (2) CUBE inséré avec `id = 1` explicite + `setval` de la séquence sur PostgreSQL seulement (même correctif que `c79f61e` de la branche `plusieurs-velos`) ; (3) deux tests ajoutés : SQLite (« kilomètres conservés après redémarrage », CUBE id 1, vélo suivant id 2) et prolongement de l'étape PostgreSQL (2800 km conservés après un 3e démarrage, vélo suivant id 2). Tests RÉELLEMENT faits : SQLite local avec `init_db()` exécutée pour de vrai (fausses pièces de FastAPI) : base neuve x2 = 1 vélo id 1 ; ancienne base (créée par le code de `main`, 3012 km, 1 sortie, 2 historiques) = données conservées, copiées une fois, marqueur posé ; +100 km puis 2 redémarrages = 3112 km, 0,9 de fraction et date de synchronisation conservés, 1 seul vélo, 30 composants, vélo suivant id 2. GitHub Actions, run `40384900885` (commit `90ae37c`) : toutes les étapes en succès, dont PostgreSQL 16 temporaire et le nouveau test de redémarrage (statut des étapes lu ; texte des journaux non lu). NON testé : base Neon réelle, navigateur, téléphone, Render. Rien fusionné dans `main` ; la PR n° 7 reste en brouillon. Avant fusion : la branche a son propre `RELAIS_MAILLONS.md` à réconcilier avec celui de `main`, et Math doit décider du sort de `.github/workflows/maillons-check.yml` (fichier de tests, hors appli).
  - ⚠ DÉFAUT BLOQUANT TROUVÉ le 26/10/2026 (relecture du sélecteur par Claude, test dans un VRAI navigateur Chromium) sur `multivelos-completion` au commit `90ae37c` : la page entière plante au chargement avec « RangeError: Maximum call stack size exceeded ». Cause : `function fetch(url, options)` (main.py, environ l. 4056, dans le script de la page) est une DÉCLARATION de fonction, donc remontée au sommet du script : elle remplace `window.fetch` AVANT la ligne `const fetchOriginal = window.fetch.bind(window)` (l. 4054), qui copie alors la nouvelle fonction elle-même ; chaque appel se rappelle sans fin. Effets observés : 0 appel au serveur, l'écran du code d'accès reste affiché, liste des vélos vide. Contrôle : la page de `main` (sans cet ajout) chargée avec le même banc de test fonctionne (appels faits, écran de code disparu). Les tests GitHub ne le voient pas (aucun navigateur : ils ne vérifient que la syntaxe JavaScript et le contenu de la page).
  - Correction testée SEULEMENT sur une copie locale de la page (pas dans le dépôt) : remplacer la déclaration par une affectation `window.fetch = function (url, options) { ... };` (le reste inchangé). Résultat dans Chromium avec de fausses réponses du serveur : sélecteur rempli (« Mon CUBE · VTT électrique », « Gravel test · Gravel »), au chargement `/api/etat?velo_id=1` puis, après changement du sélecteur, `/api/etat?velo_id=2`, `/api/mes-reglages?velo_id=2`, `/api/historique?velo_id=2`, `/api/sorties?velo_id=2` ; pastilles du vélo 1 (« Chaîne du CUBE ») puis du vélo 2 (« Pièce du gravel ») ; entête « Mon CUBE » puis « Gravel test » ; choix gardé dans `sessionStorage` ; 0 erreur JavaScript. Les réponses du serveur étaient SIMULÉES : pas testé avec le vrai serveur ni Neon, ni sur téléphone, ni Render. APPLIQUÉE ensuite avec l'accord de Math (« oui », 26/10/2026) : commit `93bbbf36aa0a325d29dae052777408e932971cb1` sur `reprise/2026-10-26-multivelos-completion` (enregistré, vérifié sur GitHub ; avance de `90ae37c` sans conflit ; `main.py` : 4 lignes de code changées + 3 de commentaire, aucun backslash ajouté ; `.github/workflows/maillons-check.yml` : +99 lignes). NOUVEAU TEST ajouté aux tests GitHub : « Tester la page dans un vrai navigateur (chargement et sélecteur de vélo) » (Chromium via Playwright, vrai FastAPI, réponses du serveur SIMULÉES) : la page doit se charger sans erreur JavaScript, le sélecteur doit lister les vélos, et le changement de vélo doit recharger état, historique, sorties et réglages du bon vélo. Vérifié que ce test ÉCHOUE sur l'ancien code (« Maximum call stack size exceeded ») et RÉUSSIT sur le nouveau, en local avant l'envoi. GitHub Actions, run `40391186508` (commit `93bbbf3`) : toutes les étapes en succès (syntaxe, migration SQLite, PostgreSQL 16 temporaire, isolation des vélos, kilomètres conservés après redémarrage, installation du navigateur, test navigateur, script de migration) ; statut des étapes lu, texte des journaux non lu. NON testé : vrai serveur avec la vraie base (Neon), téléphone, Render, et le reste de l'interface (autres onglets, formulaire d'ajout de vélo, changement de vélo d'une sortie) dans un navigateur. Rien fusionné dans `main` ; PR n° 7 toujours en brouillon.
  - Relu aussi (lecture du code, sans défaut bloquant vu) : création d'un vélo (8 types contrôlés côté serveur, pièces de départ selon le type, moteur et batterie seulement si électrique ; réserve mineure : l'id du nouveau vélo est retrouvé par son NOM, donc deux vélos de même nom restent possibles) ; fiche modifiable (`PUT /api/velos/{id}`, le CUBE n'est pas modifiable depuis cette fiche) ; changement de vélo d'une sortie (retire les km de l'ancien vélo si comptés, les ajoute au nouveau si la sortie est postérieure à sa dernière synchronisation, le tout dans une seule transaction) ; l'interface demande le vélo de destination par une fenêtre où l'on tape un numéro (peu pratique sur téléphone mais fonctionnelle). Sorties importées sans `velo_id` (script automatique du PC de Math) : rangées sur le vélo 1 (CUBE) ; la règle « dernier vélo utilisé » + pastille « à vérifier » reste à faire.
  - TÂCHE EN COURS le 26/10/2026 (Claude, Math a dit « oui ») sur `multivelos-completion` : règle « dernier vélo utilisé » + pastille rouge « à vérifier » pour les sorties reçues SANS `velo_id` (script automatique du PC de Math). Plan : colonne `sorties.a_verifier` (0 par défaut) ; `POST /api/sorties/import` : `velo_id` devient facultatif ; absent = import automatique -> vélo de la sortie la plus récente (par `debut`, repli sur le vélo 1) + `a_verifier = 1` ; présent (importation faite dans l'appli, vélo choisi en haut) = `a_verifier = 0` ; route pour confirmer le vélo d'une sortie ; changer le vélo d'une sortie la confirme aussi ; pastille rouge « À vérifier » sur la sortie + action « Confirmer le vélo » ; nombre de sorties à vérifier indiqué par vélo dans le sélecteur. Étapes : [ ] code ; [ ] tests SQLite réels + test navigateur ; [ ] tests GitHub ; [ ] relais mis à jour avec les résultats réels. Rien fusionné.
  - FAIT le 27/10/2026 par Claude : règle « dernier vélo utilisé » + pastille « À vérifier » sur `reprise/2026-10-26-multivelos-completion`, commits `a0f2c0af3858e398d341607da6390be58ae7ee63` (code + tests) et `70f1b76dfe0ccc457af96ddbea589f73cdd5d71a` (contrôle de texte adapté), enregistrés et vérifiés sur GitHub (avance de `93bbbf3` sans conflit). `main.py` : colonne `sorties.a_verifier` (0 par défaut, donc aucune sortie existante n'est « à vérifier ») ; `POST /api/sorties/import` : `velo_id` facultatif ; ABSENT = envoi automatique -> vélo de la sortie la plus récente par `debut` (repli sur le vélo 1) + `a_verifier = 1` ; PRÉSENT (import fait dans l'appli, le vélo choisi en haut est toujours envoyé par l'interface) = `a_verifier = 0` ; doublons d'import contrôlés tous vélos confondus (l'heure de départ est unique dans toute la table) ; `POST /api/sorties/{id}/verifier` (confirmer) ; changer le vélo d'une sortie la confirme ; `/api/velos` renvoie `sorties_a_verifier` par vélo ; interface : pastille rouge « À vérifier » sur la sortie, action « Confirmer le vélo » dans le menu trois points, nombre « N à vérifier » à côté du nom du vélo dans le sélecteur. Aucun backslash ajouté dans `main.py`. Une sortie reçue automatiquement compte provisoirement dans l'usure du vélo choisi (décision de Math).
  - Tests RÉELLEMENT faits : (a) local, SQLite, routes appelées pour de vrai avec de fausses pièces de FastAPI : premier import automatique sans sortie = vélo 1 + à vérifier ; import de l'appli sur le gravel = pas de pastille ; import automatique suivant = gravel (dernier vélo utilisé) + à vérifier ; idem côté CUBE ; compteurs par vélo ; liste des sorties ; kilomètres de chaque vélo = somme de ses sorties ; doublons (même heure exacte, même sortie à 4 minutes) refusés (409) tous vélos confondus ; confirmer (bon vélo ok, mauvais vélo 404, mauvais code 401) ; changer le vélo d'une sortie à vérifier = confirmée et kilomètres déplacés ; ancienne base migrée : sorties existantes à 0, kilomètres conservés. (b) local, Chromium : pastille « À vérifier » visible, « Confirmer le vélo » envoie `POST /api/sorties/7/verifier?velo_id=1`, pastille et compteur du sélecteur disparaissent, puis changement de vélo toujours correct (test vérifié ÉCHOUER sur la version d'avant). (c) GitHub Actions, run `40470840363` (commit `70f1b76`) : toutes les étapes en succès, dont le nouveau test serveur sur SQLite PUIS sur PostgreSQL 16 temporaire (base dédiée) et le test navigateur ; statut des étapes lu, texte des journaux non lu. Un premier run `40469677249` (commit `a0f2c0a`) avait échoué UNIQUEMENT sur l'étape « Vérifier le script de migration SQLite sur une base temporaire » : contrôle de TEXTE de ChatGPT exigeant deux anciennes phrases SQL de détection de doublons par vélo, que j'ai changées volontairement ; les deux phrases du contrôle ont été adaptées (`70f1b76`), tous les tests réels étaient déjà passés.
  - DÉCISION sur `.github/workflows/maillons-check.yml` (Math, 27/10/2026 : « c'est toi qui vois » ; décidé par Claude) : le fichier est GARDÉ et voyage avec le code multi-vélos dans la PR n° 7, donc arrive dans `main` à la fusion de cette PR (pas de PR séparée : ses tests échoueraient sur l'ancien code de `main`). Raisons : il ne change rien à l'appli (tourne seulement sur GitHub, aucun déploiement, `permissions: contents: read`, aucun secret, mot de passe PostgreSQL du conteneur jetable seulement) ; il a déjà trouvé/confirmé des défauts graves (kilomètres remis à l'ancienne valeur au redémarrage, page plantée par le sélecteur). Changement fait par Claude : commit `46117804a7948e0fe1a842b4175e74c9b6e4ca12` sur `reprise/2026-10-26-multivelos-completion` (enregistré, vérifié sur GitHub ; fichier seul) : nom « Vérifications Maillons », déclenché sur TOUTE branche `reprise/**` (et non plus deux branches nommées) et sur toute PR vers `main`, quand `main.py`, `db.py`, `gpx_import.py`, `requirements.txt` ou le workflow changent. Run `40471401154` (commit `4611780`) en succès (statut lu, journaux non lus). Conséquence à connaître : après fusion, chaque nouvelle branche `reprise/…` lancera ces tests ; si une évolution voulue casse un contrôle de TEXTE du dernier pas (liste `required`), adapter ce contrôle plutôt que le contourner (déjà arrivé une fois, voir ci-dessus).
  - NON testé / à savoir : le script automatique du PC de Math n'est pas dans le dépôt ; on suppose qu'il appelle `POST /api/sorties/import` SANS `velo_id` (si un jour il en envoie un, ses sorties ne seront plus « à vérifier ») : à VÉRIFIER avec un premier envoi réel après déploiement. Pas testé : base Neon réelle, vrai serveur, téléphone, Render, réponse du serveur réelle dans le navigateur (simulée). La pastille n'existe que dans la liste des sorties du vélo affiché ; le sélecteur indique les sorties à vérifier des AUTRES vélos. Pas de pastille sur l'onglet Sorties lui-même. Rien fusionné dans `main` ; PR n° 7 toujours en brouillon.
  - Limites encore ouvertes sur cette branche : `/api/sauvegarde` renvoie `km_total` lu dans l'ancienne table `velo` (figée) ; les vrais kilomètres sont dans la liste `velos` de la même réponse ; sélecteur, création de vélo et réaffectation d'une sortie non relus en détail ni testés dans un navigateur ; « dernier vélo utilisé » et pastille « à vérifier » des sorties automatiques non faits ; photo des vélos non faite.
  - Aussi constaté : `main.py` de cette branche = syntaxe OK, un seul backslash (l. 68, Python, inchangé) ; dernier contrôle GitHub sur du code : run `40379653916` (commit `6330866`) en succès ; l'ancien run `40379448708` (commit `e45b99b`) avait échoué avant correction ; PR n° 7 non relue par Claude (GraphQL refusé). Non lu en détail par Claude : sélecteur et pages (JavaScript), création de vélo, réaffectation d'une sortie. Non testé : navigateur, téléphone, Neon, Render.

NOUVELLE DEMANDE de Math — 26/10/2026, par Claude (3 demandes, traitées une par une ; les quatre premières demandes du 25/10 sont terminées et fusionnées) :
  [x] A. Survol 3D : le message « Appuie sur Lecture pour lancer le survol… » était au centre et cachait le circuit. Branche `reprise/2026-10-26-message-3d` (CSS de `.v3d-message` seulement, 2 règles ; fonctionnement inchangé) : le message est en haut, à droite de la légende des pentes, plus compact ; en paysage, même principe. Test sur page HTML générée avec fausse carte (la vraie carte 3D n'est pas testable chez Claude) : 3 tailles d'écran (390x800, 360x640, 844x390 paysage) x légende pentes et terrain : le message ne chevauche plus ni la légende ni la zone du circuit (avant : il chevauchait dans les 6 cas) ; 0 erreur JS. Non testé : vraie carte 3D, téléphone, Render. Fusionné dans `main` le 26/10/2026 avec l'accord explicite de Math (commit `0f8d0a8`, fusion sans conflit). À vérifier par Math après redéploiement Render : message en haut dans le vrai survol 3D. Déploiement Render non vérifié par Claude.
  [x] B. Réglages : « Apparence » et « Unités » sont repliables (fermées au départ), comme « Mes réglages vélo » et « Réglages de l'usure ». Branche `reprise/2026-10-26-cartes-repliables`, `main.py` seulement (même mécanisme `mr-repliable` que les cartes existantes, contenu inchangé). Tests sur page HTML générée : 4 cartes fermées au départ ; ouverture/fermeture OK ; choix de thème, de couleur et d'unité fonctionnent toujours et sont mémorisés ; 0 erreur JS ; aucun backslash ajouté. Non testé : téléphone, Render. Fusionné dans `main` le 26/10/2026 avec l'accord explicite de Math (commit `65efb80`, fusion sans conflit). À vérifier par Math après redéploiement Render : cartes Apparence et Unités repliables.
  [~] C. PLUSIEURS VÉLOS — discussion avec Math le 26/10/2026 (Claude) : les 5 points à décider sont TRANCHÉS. Aucun code écrit ; rien d'enregistré hors ce fichier. Décisions :
      1. Vélos : Math en a plusieurs et veut TOUS les cas prévus dès le départ. 8 types : VTT électrique (le CUBE actuel), VTT sans moteur, enduro, descente, gravel, gravel électrique, route, route électrique.
      2. Onglet Entretien : un sélecteur « Mon vélo » en haut de l'appli ; Entretien, Historique et composants suivent le vélo choisi (pas un onglet par vélo). Math a dit oui.
      3. Historique existant : rattaché au CUBE, sans rien supprimer.
      4. Sorties : un champ « vélo » par sortie. Chez Math, Strava met toujours le même vélo par défaut : l'équipement Strava n'est PAS fiable, il sert seulement d'indice. Import manuel (GPX ou archive) : Math choisit le vélo pour tout le lot. Envoi automatique de 19h (script sur le PC de Math, hors dépôt, on ignore s'il transmet le vélo) : la sortie est rangée d'office sur le DERNIER VÉLO UTILISÉ avec une pastille rouge « à vérifier » (choix de Math, Claude avait conseillé « Vélo à choisir ») ; elle compte provisoirement dans l'usure de ce vélo ; si Math change le vélo, l'usure des deux vélos est recalculée. Constaté dans le code : l'import de l'archive Strava lit déjà l'équipement Strava (main.py, environ lignes 3683, 10573, 10655) mais seulement pour filtrer ; il n'est pas gardé dans la sortie.
      5. Ajout d'un vélo : Math donne un nom et choisit un type parmi les 8 ; Maillons propose la liste de pièces habituelle pour ce type (batterie et moteur seulement si électrique), modifiable (ajouter, retirer) ; la fiche technique devient modifiable par vélo (marque, modèle, année, taille…). Le CUBE garde sa fiche actuelle (aujourd'hui écrite en dur pour le CUBE 2025). Math a dit oui.
      Étapes prévues (à valider par Math, une à la fois ; AUCUN code avant) :
      [x] C1. Code actuel lu le 26/10/2026 (lecture seule, commit b2541fd) : constats et plan ci-dessous. Rien n'a été exécuté ni modifié dans le code.
      [~] C2. EN COURS le 26/10/2026 : sur `reprise/2026-10-26-plusieurs-velos`, `/api/sauvegarde` exporte désormais la ligne complète `velo` et les lignes de `mes_reglages`, tout en conservant les clés déjà présentes. Commit de code : `7dd6a695446e675b5a3bbfaafa7d85651c925066`. Diff relu sur GitHub : seul `main.py` modifié (7 ajouts, 2 suppressions). Tests d'exécution et syntaxe non effectués ; ne pas fusionner. Math devra télécharger une sauvegarde avant toute migration ; aucune suppression ni migration sans son accord. Enregistré sur GitHub : code sur la branche de reprise ; point de relais `main` mis à jour. Local, non enregistré : rien. Fusion dans `main` seulement avec l'accord explicite de Math.
      [ ] C3. Branche `reprise/AAAA-MM-JJ-plusieurs-velos`, petites étapes testées : (a) table des vélos + CUBE créé avec l'existant rattaché ; (b) sélecteur « Mon vélo » ; (c) fiche technique par vélo ; (d) ajout d'un vélo par type ; (e) vélo sur chaque sortie + pastille « à vérifier » ; (f) recalcul de l'usure.
      [ ] C4. Fusion dans `main` seulement avec l'accord explicite de Math ; Render se met à jour à partir de `main`.
      Rappel : changement de fonctionnement et de base de données, donc prudence maximale ; ne pas toucher à db.py (initialisation) sans nécessité (voir section 7).
      CONSTATS C1 (numéros de ligne de main.py au commit b2541fd, approximatifs) :
      - Table `velo` (l.327) : UNE seule ligne (id = 1, contrainte CHECK id = 1) avec km_total, date_achat, km_maj_le, km_frac. Le code lit `WHERE id = 1` à une quinzaine d'endroits : /api/etat, /api/sync, remplacement de pièce, ajout/modification d'historique, import et suppression de sortie, sauvegarde.
      - Table `composants` (l.340) : pas de colonne vélo ; chaque pièce a son km_depart. `historique` est rattaché par composant_id : il suivra donc le vélo dès que les composants auront un vélo.
      - Table `sorties` (l.378) : pas de colonne vélo ; `debut` est UNIQUE. Une sortie postérieure à km_maj_le a km_compte = 1 et s'ajoute au km_total (ajuster_km_total, l.11671). Le calcul d'usure (/api/etat, l.11063) prend TOUTES les sorties : il faudra filtrer par vélo.
      - Fiche technique : table clé/valeur `mes_reglages`, globale, clés fixes MES_REGLAGES_CLES (l.549 : taille_cadre, pneus, pressions et réglages fourche/amortisseur, notes) ; texte « modèle CUBE 2025 » écrit en dur dans le HTML (l.3435).
      - Schéma du vélo : photo du CUBE tirée d'un site de boutique (l.3196) avec pastilles numérotées placées en pourcentage (pos_x, pos_y) ; ces positions n'ont de sens que pour CETTE photo.
      - Pièces du CUBE écrites en dur (COMPOSANTS_V2/V3, l.213-241 : Fox, Bosch, PowerTube…) : il n'existe aucun modèle de pièces par type de vélo.
      - `famille_de(nom)` (l.100) déduit la famille d'usure (freins, suspension, pneu, transmission) d'un MOT du nom de la pièce : les pièces des nouveaux vélos devront garder ces mots, sinon l'usure liée au terrain ne s'applique pas.
      - `reglages` (coefficients de terrain, poids du dénivelé) : globaux. Proposition : les garder communs à tous les vélos (à confirmer par Math).
      - /api/sauvegarde (l.12573) est INCOMPLÈTE : elle n'exporte ni `mes_reglages`, ni date_achat / km_frac / km_maj_le. À compléter AVANT toute migration.
      - init_db (l.315) : migrations par `ajouter_colonne` + marqueurs dans `reglages`, verrou PostgreSQL ; à ne pas casser. 28 appels fetch('/api/…') côté page.
      PLAN PROPOSÉ (petites étapes réversibles, sur la branche de reprise ; rien n'est supprimé) :
      a. Compléter /api/sauvegarde (velo, mes_reglages) ; Math télécharge une sauvegarde AVANT la migration.
      b. Nouvelle table `velos` (id, nom, type parmi les 8, électrique oui/non, km_total, km_frac, km_maj_le, date_achat…). Le CUBE y est créé avec id = 1 en copiant la ligne actuelle ; l'ancienne table `velo` reste en place, intacte.
      c. Colonne `velo_id` (défaut 1 = CUBE) ajoutée à composants et sorties ; fiche technique par vélo (nouvelle table ou colonne). Rien n'est supprimé ni déplacé.
      d. Toutes les lectures filtrent par velo_id ; sans précision, c'est le vélo 1 : l'appli reste utilisable pendant la transition.
      e. Ensuite seulement : sélecteur « Mon vélo » (vélo choisi gardé sur l'appareil, clé maillons_velo_actif), ajout d'un vélo par type avec pièces modèles, fiche technique par vélo, vélo sur chaque sortie + pastille « à vérifier », recalcul de l'usure.
      QUESTIONS Q2 et Q3 RÉSOLUES le 26/10/2026. Q2 : « dernier vélo utilisé » = vélo de la sortie la plus récente (validé par Math). Q3 : Math REFUSE de figer des coefficients de terrain communs à tous les vélos : il veut améliorer ce point plus tard. Conséquence : aujourd'hui les coefficients (table `reglages`) restent tels quels, sans aucun changement de comportement, mais le code doit les lire par une SEULE fonction (lire_reglages, l.87) pour qu'on puisse ajouter plus tard des coefficients propres à chaque vélo (avec repli sur les valeurs communes) sans tout réécrire. Ne pas décider à sa place de la forme future de ces coefficients.
      DÉCISION Q1 (Math, 26/10/2026) : photo des autres vélos = DERNIÈRE ÉTAPE du chantier, après sélecteur, fiche technique et sorties. À l'ajout d'un vélo, Math colle le lien de sa fiche (Décathlon, Opticycles ou autre) ; l'APPLI (sur Render, pas l'espace de Claude qui n'a pas accès à ces sites) lit la page, repère la photo principale et en garde une COPIE dans la base. Repli si la photo n'est pas trouvée ou si le site refuse : dessin simple fourni par l'appli selon le type, ou photo choisie par Math depuis son téléphone. Le CUBE garde sa photo actuelle (l.3196).
      Pastilles (demande de Math) : placer au mieux AUTOMATIQUEMENT les pastilles sur la photo, en partant de positions par défaut (en %) pour chaque type de vélo, car les photos Opticycles sont prises avec le même angle de profil et le même cadrage. Approximation, pas une garantie : Math ajuste ensuite avec le mode de placement existant (placementActif, route /api/composants/{id}/position). À tester sur de vraies photos avec Math ; non testable dans l'espace de Claude.

SUITE du temps 4 — 26/10/2026, par Claude : Math a vérifié sur l'appli en ligne. Verdict : profil correct dans les fenêtres qui s'ouvrent, mais encore trop écrasé dans la liste de l'onglet Sorties. Cause mesurée sur une fausse sortie de 80 km : dans la liste, le profil est dessiné en 600 x 235 puis réduit à environ 134 px de haut sur téléphone (la fenêtre agrandie fait 250 px), et l'échelle verticale minimale `total / 40` laisse les sommets à environ 30 % de la hauteur. Correction prévue sur la branche `reprise/2026-10-26-profil-liste` : échelle `total / 50` et hauteur de dessin 235 -> 270 en portrait (paysage inchangé), dans `dessinerProfil` / `construireProfil` seulement. Fusion dans `main` seulement avec l'accord de Math.

Fait sur la branche `reprise/2026-10-26-profil-liste` (2 lignes de `main.py`) : échelle `total / 50` et hauteur de dessin 270 en portrait. Tests sur une fausse sortie (page HTML générée, appli complète non démarrable chez Claude) : hauteur des sommets dans la liste 37 -> 53 px (80 km) et 70 -> 101 px (40 km), contre 55 et 102 px dans la fenêtre agrandie ; 0 erreur JavaScript ; syntaxe OK ; aucun backslash ajouté. Non testé : téléphone, vraie sortie, Render. Fusionné dans `main` le 26/10/2026 avec l'accord explicite de Math (commit `45ebacf`, fusion sans conflit). À vérifier par Math après redéploiement Render : sommets assez hauts sur une longue sortie dans la liste Sorties. Déploiement Render non vérifié par Claude.

Statut : EN COURS — fusion effectuée, validation visuelle utilisateur en attente
Objectif : temps 4 de la demande en quatre temps de Math : améliorer légèrement la hauteur du profil d'altitude dans l'onglet Sorties pour que les pics restent lisibles sur les longues distances, sans modifier les données GPX ni les autres fonctions.
Démarrée : 25/10/2026 — par : ChatGPT
Étapes prévues :
  [x] 1. Lire ce fichier de relais et vérifier les derniers commits de `main` : le code de l'application est à jour après le commit `1b1d4847808ac0bd2028cb5ce612b40eeb7132a5`.
  [x] 2. Examiner le code actuel de `main.py` : le profil est construit par `construireProfil(s, profil, largeur, hauteur, grand)`, autour de la ligne 6477 ; le profil dans la liste Sorties passe par `dessinerProfil(s)`.
  [x] 3. Ajustement ciblé enregistré sur la branche de reprise : seule l'échelle verticale du profil dans la liste Sorties est assouplie pour les longues distances. Les autres usages de `construireProfil` conservent leur échelle.
  [~] 4. Vérifier le diff et la syntaxe ; vérifications statiques faites, tests d'exécution et navigateur/téléphone non disponibles dans cette session.
  [x] 5. PR #6 fusionnée avec l'accord explicite de Math le 25/10/2026 ; commit de fusion `9a0ad76e6ca01487a06dfb7cf743b34ef67d5b3f`.
Enregistré sur GitHub :
- `main` : point de reprise `1d29868089a6b39675f4014e704402fe94e65467`.
- Branche `reprise/2026-10-25-profil-altitude-v2` : modification `main.py`, commit `286fcfa0b0d9e940117f4f835fa1a3987680e3dc`, fusionnée dans `main` par le commit `9a0ad76e6ca01487a06dfb7cf743b34ef67d5b3f`.
Local, non enregistré : rien
Fichiers modifiés pour cette étape : `main.py` uniquement pour le code de l'application ; ce fichier de relais pour le suivi.
Tests : diff GitHub relu ; seul `main.py` a été modifié (3 insertions, 3 suppressions). Tests navigateur/téléphone non faits. PR #6 fusionnée : https://github.com/bigone1212/VTT-entretien/pull/6. Déploiement Render déclenché automatiquement et en cours de construction au dernier contrôle ; attendre le déploiement, puis demander à Math de vérifier une longue sortie. L'ancienne PR #5 a été fermée comme remplacée.
Problèmes rencontrés : aucun à ce stade.
Prochaine action : attendre la fin du déploiement Render, puis demander à Math de vérifier le rendu sur une longue sortie, sur ordinateur et téléphone si possible.
Dernier commit de `main` avant la mise à jour de ce relais : `1d29868089a6b39675f4014e704402fe94e65467`.

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

### Historique ChatGPT du chantier multi-vélos — repris tel quel de la branche `reprise/2026-10-26-multivelos-completion` lors de la préparation de la fusion (27/10/2026, Claude). Ces lignes datent du 26/10/2026 ; elles sont antérieures aux corrections de Claude ci-dessus (kilomètres au redémarrage, plantage du sélecteur, règle « dernier vélo utilisé »), qui font foi quand elles diffèrent.

REPRISE PAR CHATGPT — 26/10/2026 : préparation de la fonctionnalité multi-vélos avant validation utilisateur.
- Branche dédiée : `reprise/2026-10-26-multivelos-completion`. Aucune modification de `main`, aucune fusion et aucun déploiement effectués.
- Objectif : créer et sélectionner plusieurs vélos, isoler les données et réglages personnels, préserver l'historique du CUBE, et permettre de réaffecter une sortie sans fausser les compteurs.
- Modifications enregistrées dans `main.py` :
  - Sélecteur « Mon vélo » et création de vélos parmi les huit types.
  - Composants de départ adaptés au type, avec moteur/batterie seulement pour les vélos électriques.
  - Champs moteur/batterie ajoutés au registre ; fiche technique des nouveaux vélos affichée et modifiable.
  - Réglages personnels stockés dans `mes_reglages_velo`; anciens réglages copiés vers le CUBE sans supprimer la table historique.
  - Appels API liés au vélo sélectionné ; kilomètres, composants, interventions, sorties et coefficients de pièces filtrés par vélo.
  - Pastilles de nouveauté séparées par vélo, sauvegarde étendue au registre et aux réglages personnels.
  - Réaffectation d'une sortie vers un autre vélo avec correction des kilomètres comptabilisés sur l'ancien et le nouveau vélo.
  - Un nouveau vélo commence sans date de synchronisation kilométrique ; ses sorties importées peuvent donc alimenter son compteur. Les coefficients de terrain restent globaux et inchangés.
- Modifications de `.github/workflows/maillons-check.yml` : déclenchement sur la branche de reprise et sur PR ; tests migration SQLite/PostgreSQL et test d'intégration SQLite pour création, composants selon type, réglages séparés, modification de fiche et transfert d'une sortie.
- **Tests GitHub Actions vérifiés : run `40373004664`, commit `3c265ee64c93e9eef82627612243fd35bb586ca2`, conclusion `success`.** Étapes réussies : syntaxe Python, migration SQLite avec données historiques, démarrage/migration PostgreSQL 16 temporaire, relance idempotente, test d'intégration multi-vélos et vérification statique du script. Un premier run `40372847935` a échoué car PostgreSQL refuse `WHERE 1`; correction enregistrée en `WHERE TRUE`, puis le run `40373004664` a réussi.
- Limites restantes : test navigateur/téléphone non réalisé ; photo personnalisée des nouveaux vélos à faire à l'étape prévue ; affectation des futures sorties automatiques « à vérifier » non intégrée, car le mécanisme d'import automatique correspondant n'est pas présent dans les routes actuelles. Ne pas considérer la fonctionnalité entièrement validée.
- Pull request de brouillon : [PR #7](https://github.com/bigone1212/VTT-entretien/pull/7). Elle n'est pas fusionnée. La comparaison avec `main` est divergente ; le relais a été réconcilié avec la version récente de `main` et la PR est actuellement signalée comme fusionnable. Recontrôler son état avant toute fusion.
- Local, non enregistré : rien.
- Prochaine étape : relire la PR et vérifier le statut des contrôles les plus récents ; corriger si nécessaire, puis demander à Math s'il souhaite fusionner. Ne pas fusionner sans son accord explicite.

### Historique précédent — état de main au démarrage



REPRISE PAR CHATGPT — 26/10/2026 :
- Objectif : valider la migration C3(a) « Plusieurs vélos » sur des moteurs SQLite et PostgreSQL de test avant toute fusion.
- État GitHub : branche de code `reprise/2026-10-26-plusieurs-velos`. Elle reste divergente de `main` ; le dernier comparatif avait 18 commits en avance et 4 en retard. Ne pas fusionner automatiquement.
- Tests réussis : workflow GitHub Actions `40352096393`, commit de branche `42a16b17f86d36ce67367bb022939e003715c959`, job `121674601397`. Vérifiés dans les journaux : syntaxe de `main.py`, installation des dépendances, véritable démarrage de l'application sur SQLite temporaire et PostgreSQL 16 éphémère, conservation des données historiques (2700 km, dates, composant, intervention, sortie, réglage), rattachement des anciens composants/sorties au CUBE id 1, puis deuxième démarrage sans doublons.
- Incident de test corrigé : le premier essai PostgreSQL `40351998345` a échoué à cause de mauvais indices dans les assertions du test (les valeurs migrées étaient bien présentes). Les indices ont été corrigés dans le workflow ; l'exécution suivante a réussi. Aucun échec applicatif identifié par cet incident.
- Enregistré sur GitHub : workflow de test `.github/workflows/maillons-check.yml` sur la branche de reprise, commit `42a16b17f86d36ce67367bb022939e003715c959`. Mise à jour du relais de la branche de reprise à enregistrer après ce bilan.
- Local, non enregistré : rien.
- Fichiers modifiés pour cette étape : `.github/workflows/maillons-check.yml` sur la branche de test uniquement. Aucun changement à `main.py`, `db.py` ou `requirements.txt`.
- Limites : PostgreSQL testé dans un conteneur temporaire GitHub Actions, pas sur Neon ; tests navigateur, téléphone et Render non faits. Aucun accès ni changement à la base de production, aucune fusion de code ni déploiement de code.
- Prochaine action : passer à la prochaine sous-étape C3(b/c) seulement après avoir revu précisément le périmètre et les dépendances de l'interface multi-vélos ; conserver la migration validée séparément. Fusion dans `main` uniquement avec l'accord explicite de Math.



## Reprise ChatGPT — 26/10/2026, suite après accord de poursuivre sans fusion

- Consigne confirmée par Math : **ne pas fusionner la PR #7**, continuer uniquement sur `reprise/2026-10-26-multivelos-completion`, ne pas modifier `main`, ne pas déployer.
- État vérifié avant cette étape : PR #7 ouverte, brouillon, non fusionnée ; `main.py` de la branche contient déjà les routes de lecture/écriture par vélo, dont ajout/modification/suppression des interventions, réglages personnels, composants, import et gestion des sorties.
- Modification enregistrée sur la branche de test : `.github/workflows/maillons-check.yml`, commit `69b8d72499e9ad0ba8bdfc4b100479eb0edc091c`. Ajout de contrôles d'intégration qui vérifient qu'un vélo ne peut pas modifier les seuils d'un composant d'un autre vélo, ni modifier/supprimer une intervention qui lui appartient pas ; le test vérifie aussi que l'intervention reste intacte et peut ensuite être supprimée par le bon vélo.
- Workflow GitHub Actions `40374860008` sur le commit `69b8d72499e9ad0ba8bdfc4b100479eb0edc091c` : **terminé avec succès**. La syntaxe, les migrations SQLite/PostgreSQL et les tests d'isolation des interventions/seuils sont passés : https://github.com/bigone1212/VTT-entretien/actions/runs/40374860008
- Étape suivante également enregistrée : commit `1c9dd627bb6c3e16ae3d5861806f2b3c28b24544`, fichier `.github/workflows/maillons-check.yml`, ajoute un test d'import d'activité simulée afin de vérifier que la sortie est affectée au vélo demandé, que ses kilomètres sont incrémentés uniquement sur ce vélo et que la suppression par un autre vélo est refusée.
- Aucun fichier de l'application (`main.py`) modifié dans cette étape ; aucune opération sur `main`, aucune fusion, aucun déploiement, aucune connexion à la base de production.
- Limites toujours ouvertes : test visuel sur navigateur/téléphone non réalisé ; photos personnalisées des vélos non encore ajoutées ; affectation automatique des futures sorties importées avec statut « à vérifier » non intégrée car aucun mécanisme d'import automatique n'a été trouvé dans les routes actuelles. Les coefficients de terrain restent globaux conformément à la décision de Math.
- Le run GitHub Actions `40375125354` sur le commit `1c9dd627bb6c3e16ae3d5861806f2b3c28b24544` a **réussi** : syntaxe, migrations SQLite/PostgreSQL et test d'import (analyse GPX simulée), kilométrage isolé et refus de suppression d'une sortie depuis un autre vélo : https://github.com/bigone1212/VTT-entretien/actions/runs/40375125354
- Le run GitHub Actions `40375622265` sur le commit `aa47a3edc9468c1d6812e0899ef433ede614b24e` a **réussi** : les lectures de l'état d'entretien, du carnet et des sorties sont maintenant vérifiées pour s'assurer qu'elles restent propres au vélo sélectionné : https://github.com/bigone1212/VTT-entretien/actions/runs/40375622265
- Les tests d'import simulent la sortie déjà analysée par `gpx_import`; ils ne valident pas le parseur GPX lui-même. Aucun test visuel navigateur/téléphone ni test Render/Neon n'a été effectué.
- Suite proposée : une vérification visuelle sur navigateur/téléphone reste nécessaire avant fusion. Les photos personnalisées sont réservées à une étape ultérieure. L'affectation automatique des sorties « à vérifier » ne peut pas être intégrée sans définir/ajouter le mécanisme d'import automatique; ne pas modifier les zones 3D ni les filtres.


## Mise à jour de reprise — 26/10/2026, tests de lecture complétés

- Commit de tests : `aa47a3edc9468c1d6812e0899ef433ede614b24e`, fichier `.github/workflows/maillons-check.yml`. Le test vérifie aussi que `GET /api/etat` ne renvoie pas les composants d'un autre vélo, en plus des vérifications du carnet et des sorties.
- GitHub Actions `40375622265` : **succès**. Syntaxe Python, migrations SQLite et PostgreSQL temporaires, création/fiche/réglages/composants par vélo, transfert de sortie, import simulé, calcul et suppression de kilomètres, refus d'accès croisé entre vélos et lectures filtrées : https://github.com/bigone1212/VTT-entretien/actions/runs/40375622265
- Le test d'import simule le résultat du parseur `gpx_import`; il ne remplace pas un test du parseur sur un véritable fichier GPX. Aucun test visuel navigateur/téléphone ni test sur Neon/Render.
- État de publication vérifié : `main` reste au SHA `69142d7f095b350c4393965e093d7c9c7a37140e`; PR #7 toujours ouverte, brouillon et non fusionnée. GitHub signale actuellement `mergeable_state: dirty`; la branche de test et `main` ont divergé, avec un commit supplémentaire sur `main` qui ne modifie que `RELAIS_MAILLONS.md`. Réconcilier le relais avant toute fusion future. Ne pas résoudre cela en modifiant `main` maintenant.
- Prochain travail utile : vérification visuelle de la sélection et du changement de vélo sur navigateur et téléphone. Photos personnalisées et import automatique restent des étapes séparées ; l'import automatique nécessite de définir l'intégration/source avant de pouvoir attribuer les futures sorties avec le statut « à vérifier ».


## Vérification de l’interface — 26/10/2026

- Inspection du JavaScript de main.py : un défaut a été repéré dans la reconnexion automatique quand le code d'accès était déjà conservé en session. Le démarrage appelait /api/etat mais n'appelait pas chargerVelos(), donc le sélecteur « Mon vélo » pouvait rester vide jusqu'à une nouvelle connexion manuelle.
- Correction ciblée enregistrée sur la branche de test dans main.py, commit 86c3ebf043a06008c378a18acda77ea6739c8e86 : au démarrage/reconnexion automatique, charger d'abord la liste des vélos, puis charger l'état et les données associées.
- Contrôle ajouté dans .github/workflows/maillons-check.yml : vérifier que le sélecteur est chargé avant l'état et vérifier la syntaxe de chaque script JavaScript intégré avec Node.
- Workflow 40379653916, commit 6330866a9b09120f31f59730367be3d35ef0b440 : **succès**. Syntaxe Python, contrôle du démarrage et syntaxe JavaScript, migrations SQLite/PostgreSQL temporaires et tests d'intégration multi-vélos passés : https://github.com/bigone1212/VTT-entretien/actions/runs/40379653916
- Le premier essai du nouveau contrôle JavaScript (40379448708) a échoué à cause d'une expression de recherche trop échappée dans le test, avant d'atteindre les tests d'application. Le contrôle a été corrigé et le run final 40379653916 passe entièrement.
- Vérification visuelle réelle dans un navigateur sur ordinateur/téléphone : **non réalisée** faute d'outil navigateur disponible dans cette session. La validation actuelle reste statique et automatique; elle ne prouve pas le rendu visuel ni les interactions en situation réelle.
- Aucun déploiement déclenché. Ne pas toucher à main. La PR #7 reste brouillon et non fusionnée; GitHub signalait encore un état de fusion dirty car main a une mise à jour distincte du fichier de relais. Réconcilier ce fichier avant toute fusion future, avec l'accord explicite de Math.


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

**Contrôle — 26/10/2026 (Claude) : vérification de `main` au commit `e62c807` après les ajouts de ChatGPT (unités, profil d'altitude)**
- Comparaison avec `8a6ed0a` (couleurs fusionnées) : aucune fonction JS ni Python, aucun élément (id), aucune route supprimés ; ajouts seulement (unités : `lireUnites`, `fmtDistance`, `fmtAltitude`, `fmtVitesse`, `fmtTemperature`, `formaterHeure`, `appliquerUnites`, 5 listes dans Réglages + 1 pour le filtre de distance). Palettes de couleurs intactes.
- Précision : l'intervention 6 ci-dessous est intitulée « en cours / non fusionnée » mais elle est bien dans `main` (PR #6, fusion `9a0ad76`), de même que les unités (PR #3 et #4).
- Tests faits sur la page HTML générée (l'appli complète ne peut pas être démarrée dans l'espace de travail de Claude) : syntaxe OK, 0 erreur JavaScript, aucun backslash ajouté (3 caractères, inchangés, hors HTML/CSS/JS), listes d'unités et mémorisation (`maillon_unites`) OK, conversions justes (10 km = 6,21 mi ; 20 °C = 68 °F), couleurs et unités fonctionnent ensemble. Non testé : téléphone, Render, rendu du profil d'altitude sur une vraie longue sortie.

**Intervention 6 — 25/10/2026 : profil d'altitude un peu plus haut sur les longues sorties (en cours)**
- Code préparé sur la branche `reprise/2026-10-25-profil-altitude-v2`, commit `286fcfa0b0d9e940117f4f835fa1a3987680e3dc`, fichier `main.py) uniquement.
- Modification ciblée : le profil compact de la liste Sorties utilise désormais `total / 40` au lieu de `total / 30` comme borne de l'échelle verticale liée à la distance, seulement hors mode paysage et uniquement dans la liste. Cela devrait rendre les pics un peu plus hauts sur les longues sorties. La fenêtre agrandie et les autres profils ne changent pas.
- Vérification GitHub : comparaison de branche effectuée ; 1 commit sur la branche, 0 en retard au moment de la vérification, uniquement `main.py` modifié (3 insertions, 3 suppressions).
- Pas de test navigateur/téléphone ni de test visuel réel effectué. La modification n'est pas fusionnée et n'est pas déployée.
- Prochaine action : ouvrir et vérifier la PR, puis attendre l'accord explicite de Math avant la fusion.

**Intervention 5 — 25/10/2026 : reprise des unités d'affichage sur branche de test**
- Branche : `reprise/2026-10-25-unites`. Code de l'application modifié uniquement dans `main.py`, dernier commit de code `7c9ff2086de8c82ff25b306e736f0b77a20c57b2`.
- Ajout des réglages d'unités distance (km/mi), altitude/dénivelé (m/ft), vitesse (km/h/mph), température (°C/°F) et heure (24 h/12 h), conservés dans `localStorage` (`maillon_unites`). Conversions d'affichage ; données d'entretien conservées dans les unités internes existantes.
- Vérifications statiques et calculs de conversion simples effectués. Tests Python/JavaScript en exécution, navigateur, téléphone et rendu visuel non effectués ; le code ne doit pas être considéré comme validé.
- Math a accepté la fusion et le déploiement automatique Render le 25/10/2026. PR #2 créée ; fusion en attente de résolution du conflit de relais.
- Temps 4 (profil d'altitude plus haut pour les longues distances) non commencé, à traiter après validation des unités.
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

- **En cours** : temps 4, profil d'altitude un peu plus haut pour les longues sorties ; code préparé sur `reprise/2026-10-25-profil-altitude-v2`, commit `286fcfa0b0d9e940117f4f835fa1a3987680e3dc`, non fusionné.
- **Terminé** : temps 3, choix des unités d'affichage fusionné dans `main` avec les corrections du carnet d'entretien et du compteur total ; Math a confirmé que l'affichage fonctionne.
- **Prochaine étape** : ouvrir et vérifier la PR du temps 4. Fusion dans `main` seulement après accord explicite de Math.
- **À faire (sans urgence, non commencé)** : examiner la branche `feature/fiche-technique-velo` par rapport à `main` (lecture seule) et consigner ici ce qu'elle contient, avant toute décision.
- **Prochaine étape recommandée** : à la prochaine modification, démarrer l'appli (ou la tester sur Render) et consigner en section 4 ce qui a été réellement vérifié.

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

- **Dernier commit de `main` vérifié avant la présente mise à jour : `00c7371dd72ae910882552750b5a18f0089c1c41`** (26/10/2026).
- Commits de relais : `6ac4728` (création du fichier) et `b8ef78e` (protocole de reprise), tous deux vérifiés sur GitHub le 25/10/2026. Le commit de l'intervention 3 (règle de publication) vient **après** : son numéro n'est pas connu au moment de l'écriture, consulter l'historique de `main` sur GitHub.
- Dernier commit de **l'appli** (code) : `67e6a92`. Les commits de relais ne modifient que ce fichier.
- Branches vérifiées lors de la reprise : `main`, `feature/fiche-technique-velo` (non examinée), `reprise/2026-10-25-couleurs` et `reprise/2026-10-25-unites`. La branche unités n'est pas fusionnée dans `main`.
- Chaque envoi sur `main` peut déclencher une mise à jour de Render si la mise à jour automatique est active (non vérifié) : un commit qui ne modifie que ce fichier ne change pas l'appli, mais peut provoquer un redéploiement identique.
- **Render : vérifié via la connexion Render le 25/10/2026**. Service `VTT-entretien`, branche `main`, auto-déploiement activé à chaque commit. URL : `https://vtt-entretien.onrender.com`. La fusion déclenchera donc un déploiement automatique ; le résultat du déploiement après fusion reste à vérifier.

## Point de reprise ChatGPT — 26/10/2026, mise à jour après l'étape de lecture multi-vélos

- Branche de code de test active : `reprise/2026-10-26-multivelos-base-main`, basée sur le commit `a3926efcd33e44090faba18c174b1f2deef46e4a`. Ne pas fusionner dans `main` ni déployer sans accord explicite de Math.
- Contrôle antérieur confirmé : workflow `40363163830`, commit `8943ba1ca670e3e91c8fa42db47963bc2bcc4b60`, terminé avec succès. Il vérifie les filtres de lecture de `/api/historique` et `/api/sorties`.
- [~] Nouvelle étape : `POST /api/historique` accepte maintenant `velo_id` (défaut 1 pour préserver le fonctionnement actuel), vérifie que le vélo est actif et que le composant appartient à ce vélo, utilise le kilométrage du registre `velos`, et enregistre `velo_id` dans `historique`. La mise à jour de `km_depart` est également limitée au composant de ce vélo.
- Enregistré sur GitHub, branche de test : `main.py`, commit `815b759345b45ad9c1800e566f86ed3f85c650bf`.
- Contrôle GitHub Actions déclenché pour ce commit : run `40363637688`, état `in_progress` au dernier contrôle. Lien : https://github.com/bigone1212/VTT-entretien/actions/runs/40363637688
- Tests réellement effectués : inspection du diff et vérification de la cohérence des requêtes SQL par lecture. Syntaxe complète et workflow en attente au moment de cette note ; aucun test d'API réel, navigateur, téléphone, Neon ou Render effectué.
- Local, non enregistré : rien.
- Fichiers de l'application modifiés : `main.py` uniquement. Le présent fichier de relais est mis à jour sur `main` pour conserver le point de reprise ; aucun changement de code n'est fait sur `main`.
- Risque restant : les opérations de suppression et de modification de l'historique, ainsi que les écritures de sorties et les réglages/kilométrages, ne sont pas encore toutes isolées par vélo. Ne pas afficher le sélecteur multi-vélos avant d'avoir sécurisé l'ensemble des opérations concernées.
- Prochaine action : vérifier le résultat du run `40363637688`. S'il réussit, étendre les contrôles automatiques pour couvrir cette route d'écriture avant de poursuivre avec une seule route à la fois.
