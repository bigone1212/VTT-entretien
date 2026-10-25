# RELAIS_MAILLONS.md — passage de témoin Claude ⇄ ChatGPT

> Ce fichier est la mémoire commune de l'appli **Maillons**. Quiconque (Claude, ChatGPT ou autre) reprend le travail doit le **lire en premier** et le **mettre à jour en dernier**.
> Dépôt : `bigone1212/VTT-entretien` — branche de travail : `main`.
> **Aucun secret ici** (ni clé, ni mot de passe, ni jeton, ni adresse de base de données). Seuls les *noms* de variables d'environnement sont cités.

## 0. Mode d'emploi du relais

**Au début d'une intervention**
1. Lire ce fichier en entier, puis vérifier que le dernier commit de `main` correspond bien à la section 8. S'il y a des commits plus récents, lire leurs messages : quelqu'un a travaillé entre-temps.
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

**Cette intervention — 25/10/2026**
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

- **En cours** : rien d'enregistré dans le dépôt.
- **À faire** : aucune tâche en attente n'est consignée dans le dépôt. La prochaine demande vient de Math.
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

- **Dernier commit connu de `main` lors de la rédaction : `67e6a926af0e6be6f12703f5a97c3067e6aa7aa0`** (25/10/2026, 19:44 +0100, « Pastille rouge des onglets : un peu plus petite »). Vérifié le 25/10/2026 : le dépôt distant était bien à ce commit au moment de la lecture.
- Le commit qui ajoute ce fichier vient **après** celui-ci : pour connaître le tout dernier état, consulter l'historique de `main` sur GitHub.
- **Render : correspondance NON vérifiée.** Cette session n'a pas accès au tableau de bord Render et l'adresse de l'appli n'est pas dans le dépôt. D'après Math, Render se met à jour à partir de GitHub ; on ne sait pas ici si la mise à jour automatique est active, ni quel commit est actuellement en ligne.
- **Comment vérifier la prochaine fois** : dans Render, ouvrir le service puis l'onglet des déploiements ; comparer le commit affiché avec le dernier commit de `main` sur GitHub. Noter ici le résultat avec la date.
