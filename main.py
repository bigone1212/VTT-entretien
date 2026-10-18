from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import base64
import db
import gpx_import
import hmac
import json
import math
import os
import time
from typing import Optional, List, Dict
from datetime import date, datetime, timedelta, timezone

app = FastAPI(title="VTTAE Entretien")

# Code d'accès de l'application.
# Il n'est PAS écrit ici : ce fichier est public sur GitHub.
# Il se règle dans Render : Environment > variable CODE_ACCES.
CODE_ACCES = os.environ.get("CODE_ACCES", "").strip()

MAX_PHOTOS_PAR_SORTIE = 8
MAX_CHARS_PHOTO = 700_000
MAX_CHARS_MINIATURE = 60_000
PREFIXE_JPEG = "data:image/jpeg;base64,"

# ============================================================
# BASE DE DONNÉES
# ============================================================

REGLAGES_PAR_DEFAUT = {
    "terrain_route": 1.0,
    "terrain_mixte": 1.15,
    "terrain_chemin": 1.3,
    "terrain_sentier": 1.6,
    "poids_denivele": 1.0,
}


def verifier_auth(x_code_acces=None):
    """Vérifie le code d'accès envoyé par l'en-tête HTTP."""
    if not CODE_ACCES:
        raise HTTPException(
            status_code=503,
            detail="Code d'accès non réglé sur le serveur."
        )

    envoye = (x_code_acces or "").encode("utf-8")

    if not hmac.compare_digest(envoye, CODE_ACCES.encode("utf-8")):
        time.sleep(0.5)
        raise HTTPException(status_code=401, detail="Code d'accès incorrect.")


def verifier_photo(texte, max_chars):
    """Vérifie qu'on reçoit bien une photo JPEG de taille raisonnable."""
    if not texte.startswith(PREFIXE_JPEG) or len(texte) > max_chars:
        raise HTTPException(
            status_code=400,
            detail="Photo refusée (JPEG de taille raisonnable attendu)."
        )

    try:
        octets = base64.b64decode(texte[len(PREFIXE_JPEG):], validate=True)
    except ValueError:
        raise HTTPException(status_code=400, detail="Photo illisible.")

    if not octets.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=400, detail="Photo illisible.")


def miniature_de(photo):
    """Miniature d'une photo stockée (ancien format : texte seul)."""
    if isinstance(photo, dict):
        return photo.get("mini") or ""

    return photo if isinstance(photo, str) else ""


def photo_complete_de(photo):
    if isinstance(photo, dict):
        return photo.get("photo") or ""

    return photo if isinstance(photo, str) else ""


def lire_reglages(cursor):
    """Réglages d'usure : valeurs enregistrées, sinon valeurs par défaut."""
    reglages = dict(REGLAGES_PAR_DEFAUT)

    cursor.execute("SELECT cle, valeur FROM reglages")

    for ligne in cursor.fetchall():
        if ligne[0] in reglages and ligne[1] is not None:
            reglages[ligne[0]] = float(ligne[1])

    return reglages


def famille_de(nom):
    """Famille d'une pièce, déduite de son nom."""
    nom = (nom or "").lower()

    if "plaquette" in nom or "frein" in nom or "disque" in nom:
        return "freins"

    if "fourche" in nom or "amortisseur" in nom or "suspension" in nom:
        return "suspension"

    if "pneu" in nom:
        return "pneu"

    if any(m in nom for m in (
        "chaîne", "chaine", "cassette", "plateau", "galet",
        "dérailleur", "derailleur", "transmission"
    )):
        return "transmission"

    # Entretiens liés au temps ou sans lien direct avec le terrain.
    return "autre"


def facteur_usure(famille, distance_km, d_pos, d_neg, terrain, reglages):
    """
    Combien de km d'usure équivaut 1 km de cette sortie pour cette famille.
    1,0 = usure normale ; 2,0 = deux fois plus vite.
    """
    coef_terrain = reglages.get(f"terrain_{terrain}", 1.0)
    distance = max(float(distance_km or 0), 0.1)
    poids = reglages.get("poids_denivele", 1.0)

    if famille == "transmission":
        montee = (d_pos or 0) / distance
        return coef_terrain * (1 + poids * montee / 100)

    if famille == "freins":
        descente = (d_neg or 0) / distance
        return coef_terrain * (1 + poids * descente / 100)

    if famille == "autre":
        return 1.0

    return coef_terrain


def maintenant_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ajouter_colonne(cursor, table, colonne, definition):
    """Ajoute une colonne à une table existante, si elle n'existe pas encore."""
    if db.USE_PG:
        cursor.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {colonne} {definition}"
        )
        return

    cursor.execute(f"PRAGMA table_info({table})")
    existantes = [row[1] for row in cursor.fetchall()]

    if colonne not in existantes:
        cursor.execute(
            f"ALTER TABLE {table} ADD COLUMN {colonne} {definition}"
        )


DATE_ACHAT_PAR_DEFAUT = "2025-11-18"   # livraison du vélo (facture)

# Entretiens ajoutés pour le Cube Stereo Hybrid ONE44 EXC 800 (2025).
# (nom, seuil en km (0 = aucun), seuil en jours (0 = aucun), catégorie, x %, y %)
COMPOSANTS_V2 = [
    ("Disque de frein avant (203 mm)", 4000, 0, "piece", 79, 58),
    ("Disque de frein arrière (203 mm)", 4000, 0, "piece", 16, 67),
    ("Purge du liquide de frein (avant + arrière)", 0, 365, "maintenance", 57, 15),
    ("Plateau (pédalier ACID 34/36 dents)", 5000, 0, "piece", 43, 66),
    ("Galets de dérailleur arrière", 4000, 0, "piece", 21, 73),
    ("Câble et gaine de dérailleur", 0, 365, "maintenance", 30, 66),
    ("Pédales (graissage et jeu)", 0, 365, "maintenance", 45, 75),
    ("Pneu avant / tubeless", 3500, 0, "piece", 93, 63),
    ("Liquide préventif tubeless (2 pneus)", 0, 120, "maintenance", 87, 82),
    ("Roulements de moyeux (avant + arrière)", 5000, 365, "maintenance", 76, 68),
    ("Rayons et voile des roues (contrôle)", 0, 180, "maintenance", 84, 48),
    ("Amortisseur Fox - Entretien 50h", 800, 0, "maintenance", 44, 44),
    ("Amortisseur Fox - Révision complète 200h", 3200, 0, "maintenance", 48, 50),
    ("Roulements de suspension (biellettes)", 3000, 365, "maintenance", 35, 56),
    ("Jeu de direction (contrôle et graissage)", 5000, 365, "maintenance", 65, 20),
    ("Tige de selle télescopique (nettoyage et graissage)", 1600, 0, "maintenance", 33, 20),
    ("Contrôle des serrages (potence, cintre, roues, pédalier)", 0, 180, "maintenance", 63, 12),
    ("Contrôle annuel Bosch (moteur + logiciel)", 0, 365, "maintenance", 37, 70),
    ("Batterie PowerTube 800 (contacts et état)", 0, 180, "maintenance", 55, 45),
]


# Contrôles réguliers (pression, réglages) à faire souvent.
COMPOSANTS_V3 = [
    ("Pneus - pression et état (contrôle)", 0, 14, "maintenance", 92, 74),
    ("Fourche - pression et SAG (contrôle)", 0, 30, "maintenance", 80, 38),
    ("Amortisseur - pression et SAG (contrôle)", 0, 30, "maintenance", 41, 50),
]


def migrer_composants_v2(cursor):
    ajouter_composants_une_fois(cursor, "migration_composants_v2", COMPOSANTS_V2)
    ajouter_composants_une_fois(cursor, "migration_composants_v3", COMPOSANTS_V3)
    marquer_verifications_une_fois(cursor)


# Contrôles réguliers : affichés en pastilles sous la photo, pas dans la liste.
NOMS_VERIFICATIONS = (
    "Pneus - pression et état (contrôle)",
    "Fourche - pression et SAG (contrôle)",
    "Amortisseur - pression et SAG (contrôle)",
    "Rayons et voile des roues (contrôle)",
    "Contrôle des serrages (potence, cintre, roues, pédalier)",
    "Contrôle annuel Bosch (moteur + logiciel)",
    "Batterie PowerTube 800 (contacts et état)",
)


def marquer_verifications_une_fois(cursor):
    cursor.execute(
        "SELECT valeur FROM reglages WHERE cle = ?",
        ("migration_verifications_v1",)
    )

    if cursor.fetchone() is not None:
        return

    for nom in NOMS_VERIFICATIONS:
        cursor.execute(
            "UPDATE composants SET verification = 1 WHERE nom = ?",
            (nom,)
        )

    cursor.execute("""
        INSERT INTO reglages (cle, valeur)
        VALUES (?, 1)
        ON CONFLICT (cle) DO NOTHING
    """, ("migration_verifications_v1",))


def ajouter_composants_une_fois(cursor, marqueur, liste):
    """Ajoute une seule fois de nouveaux entretiens (sans toucher à l'existant)."""
    cursor.execute(
        "SELECT valeur FROM reglages WHERE cle = ?",
        (marqueur,)
    )

    if cursor.fetchone() is not None:
        return

    cursor.execute("SELECT nom FROM composants")
    existants = {r[0] for r in cursor.fetchall()}

    for nom, km, jours, categorie, x, y in liste:
        if nom in existants:
            continue

        cursor.execute("""
            INSERT INTO composants (
                nom, km_depart, seuil_alerte, seuil_jours, categorie, pos_x, pos_y
            )
            VALUES (?, 0, ?, ?, ?, ?, ?)
        """, (nom, km, jours, categorie, x, y))

    cursor.execute("""
        INSERT INTO reglages (cle, valeur)
        VALUES (?, 1)
        ON CONFLICT (cle) DO NOTHING
    """, (marqueur,))


def init_db():
    conn = db.connect()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS velo (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            km_total INTEGER DEFAULT 2700
        )
    """)

    cursor.execute("""
        INSERT INTO velo (id, km_total)
        VALUES (1, 2700)
        ON CONFLICT (id) DO NOTHING
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS composants (
            id {db.AUTO_ID},
            nom TEXT NOT NULL,
            km_depart INTEGER DEFAULT 0,
            seuil_alerte INTEGER NOT NULL,
            categorie TEXT DEFAULT 'piece',
            pos_x INTEGER DEFAULT 50,
            pos_y INTEGER DEFAULT 50
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS historique (
            id {db.AUTO_ID},
            composant_id INTEGER,
            date_remplacement TEXT,
            cout DOUBLE PRECISION DEFAULT 0,
            notes TEXT,
            km_intervention INTEGER,
            FOREIGN KEY (composant_id) REFERENCES composants (id)
        )
    """)

    if not db.USE_PG:
        cursor.execute("PRAGMA table_info(historique)")
        colonnes = [row[1] for row in cursor.fetchall()]

        if "km_intervention" not in colonnes:
            cursor.execute("""
                ALTER TABLE historique
                ADD COLUMN km_intervention INTEGER
            """)

    # Kilométrage de la pièce juste AVANT l'intervention : permet de
    # remettre la pièce exactement comme avant si on supprime la ligne.
    ajouter_colonne(cursor, "historique", "km_avant", "INTEGER")

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS sorties (
            id {db.AUTO_ID},
            nom TEXT,
            debut TEXT NOT NULL UNIQUE,
            sport TEXT,
            distance_km DOUBLE PRECISION,
            denivele_pos INTEGER,
            denivele_neg INTEGER,
            duree_s INTEGER,
            vitesse_moy DOUBLE PRECISION,
            alt_max INTEGER,
            pente_moy_montee DOUBLE PRECISION,
            terrain TEXT DEFAULT 'mixte',
            notes TEXT,
            km_compte INTEGER DEFAULT 0,
            trace TEXT
        )
    """)

    ajouter_colonne(cursor, "sorties", "photos", "TEXT")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reglages (
            cle TEXT PRIMARY KEY,
            valeur DOUBLE PRECISION NOT NULL
        )
    """)

    for cle, valeur in REGLAGES_PAR_DEFAUT.items():
        cursor.execute("""
            INSERT INTO reglages (cle, valeur)
            VALUES (?, ?)
            ON CONFLICT (cle) DO NOTHING
        """, (cle, valeur))

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mes_reglages (
            cle TEXT PRIMARY KEY,
            valeur TEXT NOT NULL,
            maj_le TEXT
        )
    """)

    ajouter_colonne(cursor, "composants", "seuil_jours", "INTEGER DEFAULT 0")
    ajouter_colonne(cursor, "composants", "verification", "INTEGER DEFAULT 0")
    ajouter_colonne(cursor, "velo", "date_achat", "TEXT")
    cursor.execute(
        "UPDATE velo SET date_achat = ? WHERE id = 1 AND date_achat IS NULL",
        (DATE_ACHAT_PAR_DEFAUT,)
    )

    ajouter_colonne(cursor, "velo", "km_maj_le", "TEXT")
    ajouter_colonne(cursor, "velo", "km_frac", "DOUBLE PRECISION DEFAULT 0")

    cursor.execute(
        "UPDATE velo SET km_maj_le = ? WHERE id = 1 AND km_maj_le IS NULL",
        (maintenant_utc(),)
    )

    cursor.execute("SELECT COUNT(*) FROM composants")

    if cursor.fetchone()[0] == 0:
        composants_initiaux = [
            (
                "Chaîne Shimano XT (Remplacement)",
                1236, 2000, "piece", 27, 76
            ),
            (
                "Nettoyage & Lubrification chaîne",
                2445, 300, "maintenance", 35, 76
            ),
            (
                "Plaquettes de frein avant (N04C)",
                0, 1500, "piece", 70, 62
            ),
            (
                "Plaquettes de frein arrière",
                0, 1500, "piece", 22, 47
            ),
            (
                "Fourche Fox 36 - Entretien 50h",
                0, 800, "maintenance", 75, 44
            ),
            (
                "Fourche Fox 36 - Révision lourde 200h",
                0, 3200, "maintenance", 71, 34
            ),
            (
                "Cassette Shimano XT 12-speed",
                0, 4000, "piece", 20, 63
            ),
            (
                "Pneu arrière / tubeless",
                0, 2500, "piece", 7, 58
            )
        ]

        cursor.executemany("""
            INSERT INTO composants (
                nom,
                km_depart,
                seuil_alerte,
                categorie,
                pos_x,
                pos_y
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, composants_initiaux)

        entretiens_connus = [
            ("Chaîne Shimano XT (Remplacement)",
             "2026-03-05", 1236, "Remplacement chaîne"),
            ("Nettoyage & Lubrification chaîne",
             "2026-10-01", 2445, "Nettoyage et lubrification"),
        ]

        for nom, jour, km, note in entretiens_connus:
            cursor.execute(
                "SELECT id FROM composants WHERE nom = ?", (nom,)
            )
            ligne = cursor.fetchone()

            if ligne is not None:
                cursor.execute("""
                    INSERT INTO historique (
                        composant_id,
                        date_remplacement,
                        cout,
                        notes,
                        km_intervention
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (ligne[0], jour, 0.0, note, km))

    migrer_composants_v2(cursor)

    conn.commit()
    conn.close()


init_db()


# ============================================================
# MODÈLES DES REQUÊTES
# ============================================================

class SynchroRequest(BaseModel):
    km_total: int = Field(ge=0)


class RemplacementRequest(BaseModel):
    notes: Optional[str] = "Intervention standard"
    cout: Optional[float] = Field(default=0.0, ge=0)
    date_remplacement: Optional[str] = None


class ReglagesRequest(BaseModel):
    terrain_route: float = Field(ge=0.5, le=5)
    terrain_mixte: float = Field(ge=0.5, le=5)
    terrain_chemin: float = Field(ge=0.5, le=5)
    terrain_sentier: float = Field(ge=0.5, le=5)
    poids_denivele: float = Field(ge=0, le=5)


MES_REGLAGES_CLES = {
    "pneu_av", "pneu_ar",
    "fourche_psi", "fourche_sag", "fourche_detente", "fourche_bagues",
    "amorto_psi", "amorto_sag", "amorto_detente", "amorto_bagues",
    "notes",
}


class MesReglagesRequest(BaseModel):
    valeurs: Dict[str, str]


class SeuilsRequest(BaseModel):
    seuil_alerte: int = Field(ge=0, le=200000)
    seuil_jours: int = Field(ge=0, le=3650)


class PositionRequest(BaseModel):
    pos_x: int = Field(ge=0, le=100)
    pos_y: int = Field(ge=0, le=100)


class PhotoIn(BaseModel):
    mini: str = Field(max_length=200_000)
    photo: str = Field(max_length=1_500_000)


class TerrainRequest(BaseModel):
    terrain: str = Field(max_length=20)


class AjoutPhotosRequest(BaseModel):
    photos: List[PhotoIn] = Field(max_length=8)


class ImportGpxRequest(BaseModel):
    # Un fichier GPX ou TCX (texte), ou un fichier FIT (binaire en base64).
    gpx: Optional[str] = Field(default=None, min_length=50, max_length=3_000_000)
    fit: Optional[str] = Field(default=None, min_length=20, max_length=8_500_000)
    terrain: Optional[str] = "mixte"
    notes: Optional[str] = None
    nom: Optional[str] = None
    photos: Optional[List[PhotoIn]] = None


class HistoriqueRequest(BaseModel):
    composant_id: int
    date_remplacement: str
    km_intervention: int = Field(ge=0)
    cout: Optional[float] = Field(default=0.0, ge=0)
    notes: Optional[str] = "Ancien entretien"


# ============================================================
# PAGE PRINCIPALE : HTML, CSS ET JAVASCRIPT
# ============================================================

@app.get("/", response_class=HTMLResponse)
def accueil():
    html_content = """
    <!DOCTYPE html>
    <html lang="fr">

    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <title>Maillons - Entretien VTT</title>
        <meta name="theme-color" content="#0b171c">
        <script>
            (function () {
                try {
                    var t = localStorage.getItem('maillon_theme');
                    if (t === 'light' || t === 'dark') {
                        document.documentElement.setAttribute('data-theme', t);
                    }
                } catch (e) {}
            })();
        </script>
        <meta name="apple-mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-title" content="Maillons">
        <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 96 96'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%2335e0c8'/><stop offset='1' stop-color='%230a8f9c'/></linearGradient></defs><rect width='96' height='96' rx='24' fill='url(%23g)'/><g fill='none' stroke-linecap='round'><rect x='14' y='34' width='42' height='22' rx='11' transform='rotate(-32 35 45)' stroke='%2306262b' stroke-width='9'/><rect x='40' y='42' width='42' height='22' rx='11' transform='rotate(-32 61 53)' stroke='%2306262b' stroke-width='9'/><rect x='14' y='34' width='42' height='22' rx='11' transform='rotate(-32 35 45)' stroke='white' stroke-width='5'/><rect x='40' y='42' width='42' height='22' rx='11' transform='rotate(-32 61 53)' stroke='white' stroke-width='5'/></g></svg>">

        <style>
            :root {
                --bg-color: #0b171c;
                --card-bg: #13252c;
                --text-color: #eef6f4;
                --text-muted: #8fa9b0;
                --accent: #35e0c8;
                --accent-hover: #6ff0dc;
                --on-accent: #04231f;
                --nav-h: 68px;
                --success: #35e0c8;
                --danger: #ef4444;
                --line: rgba(255,255,255,0.05);
                --line2: rgba(255,255,255,0.1);
                --line3: rgba(255,255,255,0.07);
                --fill: rgba(255,255,255,0.05);
                --fill2: rgba(255,255,255,0.1);
                --tick: rgba(255,255,255,0.22);
                --sh: 0 4px 12px rgba(0,0,0,0.3);
                --field: #0a1317;
                --field-line: #23393f;
                --inset: rgba(0,0,0,0.6);
                --nav-bg: rgba(14, 29, 35, 0.94);
                --bg-fade: rgba(11,23,28,0);
                --strong: #ffffff;
                color-scheme: dark;
                --warning: #f59e0b;
            }

            * {
                box-sizing: border-box;
            }

            body {
                font-family: ui-rounded, "SF Pro Rounded", "Segoe UI Variable",
                    "Segoe UI", system-ui, Roboto, Helvetica, Arial, sans-serif;
                background-color: var(--bg-color);
                color: var(--text-color);
                margin: 0;
                padding: 0 16px calc(var(--nav-h) + 28px + env(safe-area-inset-bottom));
                -webkit-font-smoothing: antialiased;
            }

            #authScreen {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: var(--bg-color);
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                z-index: 9999;
                padding: 20px;
            }

            .auth-card {
                background: var(--card-bg);
                padding: 24px;
                border-radius: 14px;
                box-shadow: var(--sh);
                border: 1px solid var(--line);
                width: 100%;
                max-width: 400px;
                text-align: center;
            }

            .auth-card h2 {
                margin-top: 0;
                font-size: 1.2rem;
                color: var(--strong);
            }

            .header {
                position: sticky;
                top: 0;
                z-index: 40;
                max-width: 600px;
                margin: 0 auto 14px auto;
                padding: 12px 0;
                display: flex;
                align-items: center;
                gap: 12px;
                background: linear-gradient(var(--bg-color) 75%, var(--bg-fade));
            }

            .logo {
                width: 40px;
                height: 40px;
                flex-shrink: 0;
                border-radius: 11px;
                box-shadow: 0 4px 14px rgba(53,224,200,0.25);
            }

            .header-titres {
                flex: 1;
                min-width: 0;
            }

            .header h1 {
                font-size: 1.3rem;
                font-weight: 800;
                letter-spacing: -0.03em;
                line-height: 1.1;
                margin: 0;
                color: var(--strong);
            }

            .header-velo {
                font-size: 0.78rem;
                color: var(--text-muted);
                margin: 2px 0 0 0;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            .km-pill {
                flex-shrink: 0;
                font-size: 0.85rem;
                font-weight: 700;
                color: var(--accent);
                background: rgba(53,224,200,0.12);
                border: 1px solid rgba(53,224,200,0.3);
                border-radius: 999px;
                padding: 6px 12px;
            }

            .sync-card {
                background: var(--card-bg);
                padding: 14px;
                border-radius: 14px;
                margin: 0 auto 20px auto;
                box-shadow: var(--sh);
                border: 1px solid var(--line);
                max-width: 600px;
            }

            .sync-card label {
                display: block;
                font-size: 0.75rem;
                color: var(--text-muted);
                margin-bottom: 6px;
            }

            .sync-row {
                display: flex;
                gap: 10px;
            }

            input[type="number"],
            input[type="text"],
            input[type="date"],
            input[type="password"],
            select {
                flex: 1;
                min-width: 0;
                background: var(--field);
                border: 1px solid var(--field-line);
                border-radius: 12px;
                padding: 10px;
                color: var(--strong);
                font-size: 1rem;
                outline: none;
                width: 100%;
                margin-bottom: 10px;
            }

            input:focus,
            select:focus {
                border-color: var(--accent);
            }

            button.btn-primary {
                background: var(--accent);
                color: var(--on-accent);
                border: none;
                border-radius: 12px;
                padding: 10px 16px;
                font-weight: 600;
                cursor: pointer;
            }

            button.btn-primary:hover {
                background: var(--accent-hover);
            }

            .bike-container {
                position: relative;
                width: 100%;
                max-width: 600px;
                aspect-ratio: 16 / 9;
                margin: 0 auto 20px auto;
                background: #ffffff;
                border-radius: 14px;
                overflow: hidden;
                border: 1px solid var(--line);
                box-shadow: var(--sh);
            }

            .bike-img {
                width: 100%;
                height: 100%;
                object-fit: contain;
                display: block;
            }

            .mes-reglages {
                max-width: 600px;
                margin: 0 auto 20px auto;
                background: var(--card-bg);
                border: 1px solid var(--line);
                border-radius: 14px;
                padding: 14px;
                box-shadow: var(--sh);
            }

            .mr-titre {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 8px;
                margin-bottom: 10px;
            }

            .mr-titre strong { font-size: 1rem; }

            .mr-titre button, .mr-actions button {
                background: transparent;
                color: var(--text-muted);
                border: 1px solid rgba(148,163,184,0.4);
                border-radius: 8px;
                padding: 6px 10px;
                font-size: 0.8rem;
                cursor: pointer;
            }

            .mr-actions { display: flex; gap: 8px; margin-top: 12px; }
            .mr-actions button { scroll-margin-bottom: 110px; }
            .mr-actions button.mr-ok {
                background: var(--accent);
                color: var(--on-accent);
                border-color: var(--accent);
                font-weight: 700;
            }

            .mr-groupe {
                padding: 10px 0;
                border-top: 1px solid var(--line3);
            }

            .mr-groupe h3 {
                margin: 0 0 6px;
                font-size: 0.9rem;
                color: var(--accent);
            }

            .mr-ligne {
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 10px;
                font-size: 0.88rem;
                padding: 3px 0;
            }

            .mr-ligne span { color: var(--text-muted); }
            .mr-ligne b { color: var(--strong); font-weight: 700; }
            .mr-ligne input[type="text"] { width: 120px; flex: none; margin: 0; padding: 8px 10px; text-align: right; }

            .placement-bar {
                max-width: 600px;
                margin: -8px auto 16px auto;
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                align-items: center;
                justify-content: center;
            }

            .placement-bar button, .card-tools button, .btn-voir-plus {
                background: transparent;
                color: var(--text-muted, #94a3b8);
                border: 1px solid rgba(148,163,184,0.4);
                border-radius: 8px;
                padding: 6px 10px;
                font-size: 0.8rem;
                cursor: pointer;
            }

            .card-tools {
                display: flex;
                gap: 8px;
                margin-top: 8px;
            }

            .btn-voir-plus {
                width: 100%;
                padding: 12px;
                margin: 4px 0 16px;
                font-size: 0.95rem;
            }

            .bike-container.placement { outline: 3px dashed var(--accent); cursor: crosshair; }
            .hotspot.choisi { outline: 3px solid #fff; transform: translate(-50%, -50%) scale(1.35); z-index: 5; }

            .hotspot {
                position: absolute;
                width: 24px;
                height: 24px;
                background: linear-gradient(135deg, #35e0c8, #0a8f9c);
                border: 2px solid #fff;
                border-radius: 50%;
                transform: translate(-50%, -50%);
                cursor: pointer;
                box-shadow: 0 2px 6px rgba(0,0,0,0.4);
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 0.7rem;
                font-weight: 800;
                color: #fff;
                text-shadow: 0 1px 2px rgba(0,0,0,0.45);
                transition: transform 0.2s;
            }

            .hotspot:hover {
                transform: translate(-50%, -50%) scale(1.2);
            }

            .hotspot.warning {
                background: var(--danger);
                animation: pulse-red 1.5s infinite;
            }

            .hotspot.near-warning {
                background: var(--warning);
                animation: pulse-orange 1.5s infinite;
            }

            @keyframes pulse-red {
                0% {
                    box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7);
                }
                70% {
                    box-shadow: 0 0 0 10px rgba(239, 68, 68, 0);
                }
                100% {
                    box-shadow: 0 0 0 0 rgba(239, 68, 68, 0);
                }
            }

            @keyframes pulse-orange {
                0% {
                    box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.7);
                }
                70% {
                    box-shadow: 0 0 0 10px rgba(245, 158, 11, 0);
                }
                100% {
                    box-shadow: 0 0 0 0 rgba(245, 158, 11, 0);
                }
            }

            .grid {
                display: flex;
                flex-direction: column;
                gap: 12px;
                max-width: 600px;
                margin: 0 auto 20px auto;
            }

            .card {
                background: var(--card-bg);
                border-radius: 20px;
                padding: 16px;
                box-shadow: var(--sh);
                border: 1px solid var(--line);
            }

            .card-top {
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                gap: 10px;
                margin-bottom: 8px;
            }

            .component-name {
                font-weight: 600;
                font-size: 1rem;
                color: var(--strong);
            }

            .badge {
                flex-shrink: 0;
                font-size: 0.7rem;
                padding: 3px 8px;
                border-radius: 20px;
                font-weight: 600;
            }

            .badge.ok {
                background: rgba(53, 224, 200, 0.15);
                color: var(--success);
            }

            .badge.near-warning {
                background: rgba(245, 158, 11, 0.15);
                color: var(--warning);
            }

            .badge.warning {
                background: rgba(239, 68, 68, 0.15);
                color: var(--danger);
            }

            .stats-row {
                display: flex;
                justify-content: space-between;
                gap: 10px;
                font-size: 0.8rem;
                color: var(--text-muted);
                margin-bottom: 6px;
            }

            .bar-bg {
                background: var(--field);
                border-radius: 6px;
                height: 8px;
                width: 100%;
                overflow: hidden;
                margin-bottom: 12px;
            }

            .bar-fill {
                height: 100%;
                background: var(--success);
                border-radius: 6px;
                transition: width 0.3s ease;
            }

            .bar-fill.near-warning {
                background: var(--warning);
            }

            .bar-fill.warning {
                background: var(--danger);
            }

            button.btn-action {
                background: var(--fill);
                color: var(--text-color);
                border: 1px solid var(--line2);
                width: 100%;
                padding: 8px;
                border-radius: 12px;
                font-size: 0.85rem;
                cursor: pointer;
            }

            button.btn-action:hover {
                background: var(--fill2);
            }

            .history-section {
                max-width: 600px;
                margin: 0 auto;
                background: var(--card-bg);
                border-radius: 14px;
                padding: 16px;
                border: 1px solid var(--line);
                box-shadow: var(--sh);
            }

            .history-section h2 {
                font-size: 1.1rem;
                margin-top: 0;
                color: var(--strong);
            }

            .history-item {
                font-size: 0.85rem;
                padding: 10px 0;
                border-bottom: 1px solid var(--line);
                display: flex;
                justify-content: space-between;
                gap: 12px;
                align-items: flex-start;
            }

            .history-item:last-child {
                border-bottom: none;
            }

            .history-details {
                overflow-wrap: anywhere;
                flex: 1;
            }

            .history-cost {
                text-align: right;
                white-space: nowrap;
                font-weight: 600;
                color: var(--accent);
            }

            .loading {
                text-align: center;
                color: var(--text-muted);
                padding: 30px;
            }

            .form-help {
                color: var(--text-muted);
                font-size: 0.8rem;
                margin: -4px 0 10px;
            }

            input[type="file"] {
                width: 100%;
                margin-bottom: 10px;
                color: var(--text-muted);
            }

            .sortie-media {
                display: flex;
                flex-direction: column;
                gap: 6px;
                align-items: center;
                flex-shrink: 0;
            }

            .photos-gallery {
                display: flex;
                gap: 4px;
                flex-wrap: wrap;
                max-width: 100px;
                justify-content: flex-end;
            }

            .sortie-img {
                width: 43px;
                height: 43px;
                object-fit: cover;
                border-radius: 4px;
                border: 1px solid var(--line2);
            }

            #lightbox {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.92);
                z-index: 9000;
                align-items: center;
                justify-content: center;
                cursor: pointer;
            }

            #lightboxRetirer {
                position: absolute;
                bottom: 18px;
                left: 50%;
                transform: translateX(-50%);
                background: rgba(180, 30, 30, 0.9);
                color: #fff;
                border: none;
                border-radius: 8px;
                padding: 10px 18px;
                font-size: 0.95rem;
                cursor: pointer;
            }

            #lightbox img {
                max-width: 96%;
                max-height: 96%;
                object-fit: contain;
                border-radius: 6px;
            }

            @media (max-width: 480px) {
                body {
                    padding: 0 12px calc(var(--nav-h) + 28px + env(safe-area-inset-bottom));
                }

                .sync-row {
                    flex-direction: column;
                }

                .card-top {
                    flex-direction: column;
                }

                .stats-row {
                    flex-direction: column;
                    gap: 4px;
                }
            }

            .onglets {
                position: fixed;
                left: 0;
                right: 0;
                bottom: 0;
                z-index: 50;
                display: flex;
                justify-content: center;
                gap: 0;
                height: calc(var(--nav-h) + env(safe-area-inset-bottom));
                padding: 6px 8px env(safe-area-inset-bottom);
                background: var(--nav-bg);
                -webkit-backdrop-filter: blur(14px);
                backdrop-filter: blur(14px);
                border-top: 1px solid var(--line3);
            }

            .onglet {
                flex: 1;
                max-width: 200px;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                gap: 3px;
                padding: 0;
                font: inherit;
                font-size: 0.72rem;
                font-weight: 600;
                color: var(--text-muted);
                background: none;
                border: none;
                cursor: pointer;
            }

            .onglet svg {
                width: 24px;
                height: 24px;
                fill: none;
                stroke: currentColor;
                stroke-width: 1.9;
                stroke-linecap: round;
                stroke-linejoin: round;
                box-sizing: content-box;
                padding: 4px 18px;
                border-radius: 999px;
                transition: background 0.2s;
            }

            .onglet.actif {
                color: var(--accent);
            }

            .onglet.actif svg {
                background: rgba(53,224,200,0.16);
            }

            .onglet:focus-visible {
                outline: 2px solid var(--accent);
                outline-offset: -4px;
                border-radius: 12px;
            }

            .page {
                max-width: 600px;
                margin: 0 auto;
            }

            /* ---- Habillage des cartes, barres et boutons ---- */
            .ico {
                width: 16px;
                height: 16px;
                flex-shrink: 0;
                fill: none;
                stroke: currentColor;
                stroke-width: 2;
                stroke-linecap: round;
                stroke-linejoin: round;
            }

            .card {
                position: relative;
                padding: 16px 16px 14px 20px;
                overflow: hidden;
            }

            .card::before {
                content: "";
                position: absolute;
                left: 0;
                top: 0;
                bottom: 0;
                width: 5px;
                background: linear-gradient(180deg, #35e0c8, #0a8f9c);
            }

            .card.near-warning::before { background: linear-gradient(180deg, #fbbf24, #d97706); }
            .card.warning::before { background: linear-gradient(180deg, #f87171, #dc2626); }

            .component-name { line-height: 1.3; }

            .badge {
                display: inline-flex;
                align-items: center;
                gap: 6px;
                font-size: 0.74rem;
                padding: 4px 10px;
                white-space: nowrap;
            }

            .badge::before {
                content: "";
                width: 7px;
                height: 7px;
                border-radius: 50%;
                background: currentColor;
            }

            .bar-bg {
                position: relative;
                height: 12px;
                border-radius: 999px;
                background: var(--field);
                box-shadow: inset 0 1px 3px var(--inset);
                margin: 8px 0 14px;
            }

            .bar-bg::after {
                content: "";
                position: absolute;
                top: 2px;
                bottom: 2px;
                left: 85%;
                width: 2px;
                border-radius: 2px;
                background: var(--tick);
            }

            .bar-fill {
                border-radius: 999px;
                background: linear-gradient(90deg, #0a8f9c, #35e0c8);
                box-shadow: 0 0 10px rgba(53,224,200,0.45);
            }

            .bar-fill.near-warning {
                background: linear-gradient(90deg, #d97706, #fbbf24);
                box-shadow: 0 0 10px rgba(251,191,36,0.4);
            }

            .bar-fill.warning {
                background: linear-gradient(90deg, #dc2626, #f87171);
                box-shadow: 0 0 10px rgba(248,113,113,0.45);
            }

            button.btn-action {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 8px;
                padding: 11px;
                font: inherit;
                font-size: 0.9rem;
                font-weight: 700;
                color: var(--accent);
                background: rgba(53,224,200,0.1);
                border: 1px solid rgba(53,224,200,0.35);
                border-radius: 12px;
                transition: background 0.15s, transform 0.1s;
            }

            button.btn-action:hover { background: rgba(53,224,200,0.2); }
            button.btn-action:active { transform: scale(0.98); }

            .card.warning button.btn-action,
            .card.near-warning button.btn-action {
                color: #04231f;
                background: linear-gradient(135deg, #35e0c8, #0a8f9c);
                border-color: transparent;
            }

            .card-tools { margin-top: 10px; gap: 8px; }

            .card-tools button,
            .placement-bar button,
            .mr-titre button,
            .mr-actions button,
            .btn-voir-plus {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 6px;
                font: inherit;
                font-size: 0.82rem;
                font-weight: 600;
                color: var(--text-muted);
                background: var(--fill);
                border: 1px solid var(--line2);
                border-radius: 999px;
                padding: 7px 14px;
                cursor: pointer;
                transition: background 0.15s, color 0.15s;
            }

            .card-tools button:hover,
            .placement-bar button:hover,
            .mr-titre button:hover,
            .mr-actions button:hover,
            .btn-voir-plus:hover {
                background: var(--fill2);
                color: var(--strong);
            }

            .apparence {
                max-width: 600px;
                margin: 0 auto 16px auto;
                background: var(--card-bg);
                border: 1px solid var(--line);
                border-radius: 14px;
                padding: 14px;
                box-shadow: var(--sh);
            }

            .apparence strong { display: block; margin-bottom: 10px; }

            .segments {
                display: flex;
                gap: 4px;
                padding: 4px;
                background: var(--fill);
                border-radius: 999px;
            }

            .segments button {
                flex: 1;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 6px;
                font: inherit;
                font-size: 0.85rem;
                font-weight: 600;
                color: var(--text-muted);
                background: transparent;
                border: none;
                border-radius: 999px;
                padding: 9px 6px;
                cursor: pointer;
            }

            .segments button.actif {
                color: #04231f;
                background: linear-gradient(135deg, #35e0c8, #0a8f9c);
            }

            .card-tools button.danger:hover {
                background: rgba(239,68,68,0.15);
                color: #f87171;
                border-color: rgba(239,68,68,0.4);
            }

            .placement-bar button.actif {
                color: #04231f;
                background: linear-gradient(135deg, #35e0c8, #0a8f9c);
                border-color: transparent;
            }

            .mr-actions button.mr-ok {
                color: #04231f;
                background: linear-gradient(135deg, #35e0c8, #0a8f9c);
                border-color: transparent;
            }

            .liste-entete {
                grid-column: 1 / -1;
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 10px;
                padding: 0 2px;
            }

            .liste-entete strong { font-size: 0.95rem; }

            .liste-entete button {
                display: inline-flex;
                align-items: center;
                gap: 6px;
                font: inherit;
                font-size: 0.82rem;
                font-weight: 600;
                color: var(--text-muted);
                background: var(--fill);
                border: 1px solid var(--line2);
                border-radius: 999px;
                padding: 7px 14px;
                cursor: pointer;
            }

            /* Cartes repliables : une ligne + barre, le détail s'ouvre au toucher */
            .card { padding: 12px 14px 12px 18px; }

            .card .card-top {
                align-items: center;
                margin-bottom: 0;
                cursor: pointer;
                -webkit-tap-highlight-color: transparent;
            }

            .card .card-top:focus-visible {
                outline: 2px solid var(--accent);
                outline-offset: 3px;
                border-radius: 8px;
            }

            .component-name {
                flex: 1;
                min-width: 0;
                font-size: 0.95rem;
            }

            .component-name {
                display: flex;
                align-items: center;
                gap: 10px;
            }

            .component-name .nom { min-width: 0; }

            .num {
                flex-shrink: 0;
                min-width: 30px;
                height: 30px;
                padding: 0 6px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border-radius: 10px;
                font-size: 0.82rem;
                font-weight: 800;
                color: #fff;
                text-shadow: 0 1px 2px rgba(0,0,0,0.35);
                background: linear-gradient(135deg, #35e0c8, #0a8f9c);
                box-shadow: 0 2px 6px rgba(10,143,156,0.35);
            }

            .num.near-warning {
                background: linear-gradient(135deg, #fbbf24, #d97706);
                box-shadow: 0 2px 6px rgba(217,119,6,0.35);
            }

            .num.warning {
                background: linear-gradient(135deg, #f87171, #dc2626);
                box-shadow: 0 2px 6px rgba(220,38,38,0.35);
            }

            .chevron { display: inline-flex; color: var(--text-muted); transition: transform 0.2s; }
            .card.ouverte .chevron { transform: rotate(180deg); }

            .card .bar-bg { height: 8px; margin: 10px 0 0; }
            .card.ouverte .bar-bg { margin-bottom: 12px; }

            .card.masquee { display: none; }

            .verifs {
                max-width: 600px;
                margin: 0 auto 16px auto;
                background: var(--card-bg);
                border: 1px solid var(--line);
                border-radius: 18px;
                padding: 12px 14px;
                box-shadow: var(--sh);
            }

            .verifs-titre {
                display: flex;
                align-items: baseline;
                justify-content: space-between;
                gap: 8px;
                margin-bottom: 10px;
            }

            .verifs-titre strong { font-size: 0.95rem; }
            .verifs-titre .form-help { margin: 0; }

            .chips { display: flex; flex-wrap: wrap; gap: 8px; }

            .chip {
                display: inline-flex;
                align-items: center;
                gap: 7px;
                font: inherit;
                font-size: 0.82rem;
                font-weight: 600;
                color: var(--text-color);
                background: var(--fill);
                border: 1px solid var(--line2);
                border-radius: 999px;
                padding: 7px 12px 7px 10px;
                cursor: pointer;
            }

            .chip::before {
                content: "";
                width: 9px;
                height: 9px;
                border-radius: 50%;
                background: linear-gradient(135deg, #35e0c8, #0a8f9c);
            }

            .chip.near-warning::before { background: linear-gradient(135deg, #fbbf24, #d97706); }
            .chip.warning::before { background: linear-gradient(135deg, #f87171, #dc2626); }
            .chip small { font-size: 0.74rem; font-weight: 600; color: var(--text-muted); }
            .chip.warning small { color: var(--danger); }
            .chip.near-warning small { color: var(--warning); }

            .chip.choisie {
                border-color: var(--accent);
                background: rgba(53,224,200,0.12);
            }

            .verif-detail {
                margin-top: 12px;
                padding-top: 10px;
                border-top: 1px solid var(--line3);
            }

            .verif-detail .vd-nom { font-weight: 700; font-size: 0.95rem; margin-bottom: 4px; }
            .verif-detail .vd-info { font-size: 0.82rem; color: var(--text-muted); margin-bottom: 8px; }

            .btn-voir-plus {
                width: 100%;
                padding: 12px;
                font-size: 0.9rem;
                justify-content: center;
            }

            .card-detail { display: none; padding-top: 4px; }
            .card.ouverte .card-detail { display: block; }

            .card .card-top { flex-direction: row; }
            .card .badge { padding: 3px 9px; }
        
            /* ---- Mode jour : suit le réglage du téléphone, ou choix manuel ---- */
            @media (prefers-color-scheme: light) {
            :root:not([data-theme="dark"]):not([data-theme="light"]) {
                --bg-color: #eef4f3;
                --card-bg: #ffffff;
                --text-color: #16282e;
                --text-muted: #5b727a;
                --accent: #0a8f9c;
                --accent-hover: #0a7d8a;
                --on-accent: #ffffff;
                --success: #0a8f9c;
                --danger: #dc2626;
                --warning: #b45309;
                --line: rgba(20,60,70,0.10);
                --line2: rgba(20,60,70,0.16);
                --line3: rgba(20,60,70,0.12);
                --fill: rgba(20,60,70,0.05);
                --fill2: rgba(20,60,70,0.10);
                --tick: rgba(20,60,70,0.28);
                --sh: 0 4px 14px rgba(20,60,70,0.12);
                --field: #e3ecea;
                --field-line: #c3d3d0;
                --inset: rgba(20,60,70,0.18);
                --nav-bg: rgba(255,255,255,0.94);
                --bg-fade: rgba(238,244,243,0);
                --strong: #0b171c;
                color-scheme: light;
            }
            }

            :root[data-theme="light"] {
                --bg-color: #eef4f3;
                --card-bg: #ffffff;
                --text-color: #16282e;
                --text-muted: #5b727a;
                --accent: #0a8f9c;
                --accent-hover: #0a7d8a;
                --on-accent: #ffffff;
                --success: #0a8f9c;
                --danger: #dc2626;
                --warning: #b45309;
                --line: rgba(20,60,70,0.10);
                --line2: rgba(20,60,70,0.16);
                --line3: rgba(20,60,70,0.12);
                --fill: rgba(20,60,70,0.05);
                --fill2: rgba(20,60,70,0.10);
                --tick: rgba(20,60,70,0.28);
                --sh: 0 4px 14px rgba(20,60,70,0.12);
                --field: #e3ecea;
                --field-line: #c3d3d0;
                --inset: rgba(20,60,70,0.18);
                --nav-bg: rgba(255,255,255,0.94);
                --bg-fade: rgba(238,244,243,0);
                --strong: #0b171c;
                color-scheme: light;
            }
</style>
    </head>

    <body>
        <div id="authScreen">
            <div class="auth-card">
                <svg class="logo" style="width:64px;height:64px;border-radius:18px;margin-bottom:12px" role="img" aria-label="Maillons"><use href="#logo-maillon"/></svg>
                <h2>Maillons</h2>
                <p class="form-help">Entrez votre code d'accès</p>
                <input type="password" id="authInput" placeholder="Code secret" style="margin-bottom: 12px;">
                <button class="btn-primary" style="width: 100%;" onclick="tenterConnexion()">Valider</button>
                <p id="authError" style="color: var(--danger); font-size: 0.8rem; margin-top: 10px;"></p>
            </div>
        </div>

        <div id="lightbox" onclick="fermerPhoto()">
            <img id="lightboxImg" alt="Photo de la sortie">
            <button id="lightboxRetirer" type="button">🗑️ Retirer cette photo</button>
        </div>

        <svg width="0" height="0" style="position:absolute" aria-hidden="true">
            <defs>
                <linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0" stop-color="#35e0c8"/>
                    <stop offset="1" stop-color="#0a8f9c"/>
                </linearGradient>
            </defs>
            <symbol id="logo-maillon" viewBox="0 0 96 96">
                <rect width="96" height="96" rx="24" fill="url(#lg)"/>
                <g fill="none" stroke-linecap="round">
                    <rect x="14" y="34" width="42" height="22" rx="11" transform="rotate(-32 35 45)" stroke="#06262b" stroke-width="9"/>
                    <rect x="40" y="42" width="42" height="22" rx="11" transform="rotate(-32 61 53)" stroke="#06262b" stroke-width="9"/>
                    <rect x="14" y="34" width="42" height="22" rx="11" transform="rotate(-32 35 45)" stroke="#fff" stroke-width="5"/>
                    <rect x="40" y="42" width="42" height="22" rx="11" transform="rotate(-32 61 53)" stroke="#fff" stroke-width="5"/>
                </g>
            </symbol>
        </svg>

        <header class="header">
            <svg class="logo" role="img" aria-label="Maillons"><use href="#logo-maillon"/></svg>
            <div class="header-titres">
                <h1>Maillons</h1>
                <p class="header-velo">Cube Stereo Hybrid ONE44</p>
            </div>
            <span class="km-pill" id="veloInfo">… km</span>
        </header>

        <nav class="onglets" id="onglets" aria-label="Pages de l'application">
            <button type="button" class="onglet" id="onglet-entretien" onclick="location.hash='entretien'"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>Entretien</button>
            <button type="button" class="onglet" id="onglet-historique" onclick="location.hash='historique'"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Historique</button>
            <button type="button" class="onglet" id="onglet-reglages" onclick="location.hash='reglages'"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg>Réglages</button>
            <button type="button" class="onglet" id="onglet-sorties" onclick="location.hash='sorties'"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8 18h8a3 3 0 0 0 0-6H8a3 3 0 0 1 0-6h8"/></svg>Sorties</button>
        </nav>

        <div class="page" id="page-entretien">
        <div class="bike-container">
            <img
                src="https://shop.roulezjeunesse.com/cdn/shop/files/CubeStereoHybridONE44EXC8001.jpg?v=1736521382&width=1220"
                alt="Mon Cube Stereo Hybrid"
                class="bike-img"
            >

            <div id="hotspotsContainer"></div>
        </div>

        <div class="placement-bar">
            <button type="button" id="btnPlacer" onclick="basculerPlacement()"></button>
            <span class="form-help" id="placementAide" style="display:none; margin:0;">
                Touchez un numéro, puis touchez son emplacement exact sur la photo.
            </span>
        </div>

        <section class="verifs" id="verifs" style="display:none;" aria-label="Vérifications">
            <div class="verifs-titre"><strong>Vérifications</strong><span class="form-help" id="verifsInfo"></span></div>
            <div class="chips" id="verifsChips"></div>
            <div class="verif-detail" id="verifDetail" style="display:none;"></div>
        </section>

        <div id="composantsList" class="grid">
            <div class="loading">Chargement des composants...</div>
        </div>

        <div class="sync-card">
            <label for="kmInput">Kilométrage total du VTTAE</label>

            <div class="sync-row">
                <input
                    type="number"
                    id="kmInput"
                    min="0"
                    step="1"
                    placeholder="Ex. : 2700"
                >

                <button class="btn-primary" onclick="syncKm()">
                    Mettre à jour
                </button>
            </div>
        </div>

        <div style="max-width: 600px; margin: 0 auto 20px auto; text-align: right;">
            <button
                class="btn-action"
                style="width: auto; padding: 6px 12px;"
                onclick="telechargerSauvegarde()"
            >
                💾 Télécharger ma sauvegarde
            </button>
        </div>

        </div>

        <div class="page" id="page-reglages" style="display:none;">
        <section class="apparence" aria-label="Apparence">
            <strong>Apparence</strong>
            <div class="segments" id="segmentsTheme">
                <button type="button" data-theme-choix="auto"></button>
                <button type="button" data-theme-choix="light"></button>
                <button type="button" data-theme-choix="dark"></button>
            </div>
        </section>
        <section class="mes-reglages" id="mesReglages" aria-label="Mes réglages">
            <div class="mr-titre">
                <strong>🎚️ Mes réglages</strong>
                <button type="button" id="mrModifier" onclick="modifierReglagesVelo()"></button>
            </div>
            <div id="mrContenu"></div>
            <div class="form-help" id="mrDate" style="margin:8px 0 0;"></div>
            <div class="mr-actions" id="mrActions" style="display:none;">
                <button type="button" class="mr-ok" id="mrOk" onclick="enregistrerReglagesVelo()"></button>
                <button type="button" id="mrAnnuler" onclick="annulerReglagesVelo()"></button>
            </div>
        </section>
        </div>

        <div class="page" id="page-historique" style="display:none;">
        <div class="history-section">
            <h2>📜 Ajouter une ancienne intervention</h2>

            <label for="histComposant">Composant concerné</label>
            <select id="histComposant"></select>

            <label for="histDate">Date de l'intervention</label>
            <input type="date" id="histDate" required>

            <label for="histKm">Kilométrage au moment de l'intervention</label>
            <input
                type="number"
                id="histKm"
                min="0"
                step="1"
                placeholder="Ex. : 2445"
                required
            >

            <p class="form-help">
                Ce kilométrage deviendra le point de départ du suivi
                d'entretien du composant.
            </p>

            <label for="histCout">Coût en euros</label>
            <input
                type="number"
                step="0.01"
                min="0"
                id="histCout"
                placeholder="Coût en € (optionnel)"
            >

            <label for="histNotes">Notes</label>
            <input
                type="text"
                id="histNotes"
                placeholder="Ex. : entretien réalisé chez le bouclard"
            >

            <button
                class="btn-primary"
                style="width:100%;"
                onclick="ajouterHistorique()"
            >
                Enregistrer l'intervention
            </button>

            <h2 style="margin-top: 24px;">
                📋 Historique des interventions
            </h2>

            <div id="historyList">
                <div class="loading">Chargement de l'historique...</div>
            </div>
        </div>

        </div>

        <div class="page" id="page-sorties" style="display:none;">
        <div class="history-section" style="margin-bottom: 20px;">
            <h2>🚵 Sorties</h2>

            <label for="gpxFile">Importer une ou plusieurs sorties (fichiers GPX de Komoot)</label>
            <input
                type="file"
                id="gpxFile"
                accept=".gpx,application/gpx+xml,text/xml"
                multiple
            >

            <p class="form-help">
                Plusieurs fichiers d'un coup sont possibles (30 maximum).
                Le terrain et la note choisis ci-dessous s'appliquent à toutes
                les sorties ; tu pourras corriger le terrain de chacune ensuite.
                Les photos ne se joignent qu'à une seule sortie à la fois.
            </p>

            <label for="gpxPhotos">Photos souvenirs (plusieurs possibles)</label>
            <input
                type="file"
                id="gpxPhotos"
                accept="image/*"
                multiple
            >

            <label for="gpxTerrain">Terrain</label>
            <select id="gpxTerrain">
                <option value="mixte">Mixte</option>
                <option value="route">Route / bitume</option>
                <option value="chemin">Chemin / piste</option>
                <option value="sentier">Sentier technique</option>
            </select>

            <label for="gpxNotes">Notes (optionnel)</label>
            <input
                type="text"
                id="gpxNotes"
                placeholder="Ex. : sortie boueuse, pluie"
            >

            <button
                class="btn-primary"
                id="gpxBouton"
                style="width:100%;"
                onclick="importerGpx()"
            >
                Importer
            </button>

            <p class="form-help" id="gpxMessage" style="margin-top: 10px;"></p>

            <details id="blocStrava" style="margin-top: 20px;">
                <summary style="cursor: pointer; font-weight: 600;">
                    📥 Importer depuis Strava (archive)
                </summary>

                <p class="form-help" style="margin-top: 10px;">
                    Sur le site strava.com : Réglages → Mon compte →
                    « Télécharger ou supprimer votre compte » →
                    « Demander votre archive » (les libellés peuvent varier).
                    Strava t'envoie un e-mail avec un lien : télécharge le
                    fichier .zip, puis choisis-le ici. Le fichier est lu dans
                    ton navigateur : seules les sorties choisies sont envoyées
                    à l'application. Les photos ne sont pas reprises.
                </p>

                <label for="stravaZip">Archive Strava (.zip)</label>
                <input type="file" id="stravaZip" accept=".zip,application/zip">

                <div id="stravaChoix" style="display: none;">
                    <label for="stravaVelo">Vélo (équipement Strava)</label>
                    <select id="stravaVelo"></select>

                    <label for="stravaType">Type d'activité</label>
                    <select id="stravaType"></select>

                    <label for="stravaTerrain">Terrain</label>
                    <select id="stravaTerrain">
                        <option value="mixte">Mixte</option>
                        <option value="route">Route / bitume</option>
                        <option value="chemin">Chemin / piste</option>
                        <option value="sentier">Sentier technique</option>
                    </select>

                    <p class="form-help" id="stravaCompte"></p>

                    <button
                        class="btn-primary"
                        id="stravaBouton"
                        style="width:100%;"
                        onclick="importerStrava()"
                    >
                        Importer ces sorties
                    </button>
                </div>

                <p class="form-help" id="stravaMessage" style="margin-top: 10px;"></p>
            </details>

            <h2 style="margin-top: 24px;">Mes sorties</h2>

            <div id="sortiesList">
                <div class="loading">Chargement des sorties...</div>
            </div>

            <details style="margin-top: 20px;">
                <summary style="cursor: pointer; font-weight: 600;">
                    ⚙️ Réglages de l'usure
                </summary>

                <p class="form-help" style="margin-top: 10px;">
                    Chaque sortie fait vieillir tes pièces plus ou moins vite
                    selon le terrain. 1 = usure normale, 1,6 = usure 60 % plus
                    rapide. Le dénivelé compte aussi : 0 = on l'ignore,
                    1 = poids normal.
                </p>

                <label for="regRoute">Route / bitume</label>
                <input type="text" inputmode="decimal" id="regRoute">

                <label for="regMixte">Mixte</label>
                <input type="text" inputmode="decimal" id="regMixte">

                <label for="regChemin">Chemin / piste</label>
                <input type="text" inputmode="decimal" id="regChemin">

                <label for="regSentier">Sentier technique</label>
                <input type="text" inputmode="decimal" id="regSentier">

                <label for="regDenivele">Poids du dénivelé</label>
                <input type="text" inputmode="decimal" id="regDenivele">

                <button
                    class="btn-primary"
                    style="width:100%;"
                    onclick="enregistrerReglages()"
                >
                    Enregistrer les réglages
                </button>

                <p class="form-help" id="reglagesMessage" style="margin-top: 10px;"></p>
            </details>
        </div>

        </div>

        <script>
            const PAGES = ['entretien', 'historique', 'reglages', 'sorties'];

            function afficherPage() {
                let nom = (location.hash || '').replace('#', '');

                if (!PAGES.includes(nom)) {
                    nom = 'entretien';
                }

                PAGES.forEach(p => {
                    document.getElementById('page-' + p).style.display =
                        p === nom ? 'block' : 'none';

                    const bouton = document.getElementById('onglet-' + p);
                    bouton.classList.toggle('actif', p === nom);

                    if (p === nom) {
                        bouton.setAttribute('aria-current', 'page');
                    } else {
                        bouton.removeAttribute('aria-current');
                    }
                });

                window.scrollTo(0, 0);
            }

            window.addEventListener('hashchange', afficherPage);
            afficherPage();

            function getHeaders() {
                const code = sessionStorage.getItem('vtt_code') || '';
                return {
                    'Content-Type': 'application/json',
                    'x-code-acces': code
                };
            }

            async function tenterConnexion() {
                const code = document.getElementById('authInput').value;
                sessionStorage.setItem('vtt_code', code);
                
                try {
                    const res = await fetch('/api/etat', { headers: getHeaders() });
                    if (res.ok) {
                        document.getElementById('authScreen').style.display = 'none';
                        chargerEtat();
                    } else {
                        document.getElementById('authError').textContent =
                            res.status === 503
                                ? "Le code d'accès n'est pas encore réglé sur le serveur."
                                : "Code incorrect.";
                    }
                } catch (e) {
                    document.getElementById('authError').textContent = "Erreur de connexion.";
                }
            }

            document.getElementById('authInput').addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    tenterConnexion();
                }
            });

            // Vérifie si un code est déjà en mémoire
            if (sessionStorage.getItem('vtt_code')) {
                fetch('/api/etat', { headers: getHeaders() }).then(res => {
                    if (res.ok) {
                        document.getElementById('authScreen').style.display = 'none';
                        chargerEtat();
                    }
                });
            }

            const MR_GROUPES = [
                { titre: '🛞 Pneus', champs: [
                    ['pneu_av', 'Avant (bar)'], ['pneu_ar', 'Arrière (bar)'] ] },
                { titre: '🔱 Fourche', champs: [
                    ['fourche_psi', 'Pression (psi)'], ['fourche_sag', 'SAG (%)'],
                    ['fourche_detente', 'Détente (clics)'], ['fourche_bagues', 'Bagues de volume'] ] },
                { titre: '🌀 Amortisseur', champs: [
                    ['amorto_psi', 'Pression (psi)'], ['amorto_sag', 'SAG (%)'],
                    ['amorto_detente', 'Détente (clics)'], ['amorto_bagues', 'Bagues de volume'] ] },
                { titre: '📝 Remarque', champs: [
                    ['notes', 'Note libre'] ] }
            ];
            const ICONES = {
                cle: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
                echange: '<path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/><path d="M3 21v-5h5"/>',
                crayon: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
                poubelle: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/>',
                repere: '<path d="M12 21s7-6.2 7-11.5A7 7 0 0 0 5 9.5C5 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
                valider: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
                fermer: '<path d="M6 6l12 12M18 6L6 18"/>',
                bas: '<path d="M6 9l6 6 6-6"/>',
                haut: '<path d="M6 15l6-6 6 6"/>'
            };

            function remplirBouton(el, icone, texte) {
                el.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">'
                    + ICONES[icone] + '</svg><span></span>';
                el.lastChild.textContent = texte;
            }

            ICONES.soleil = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
            ICONES.lune = '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>';
            ICONES.auto = '<circle cx="12" cy="12" r="9"/><path d="M12 3v18"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>';

            function themeChoisi() {
                try {
                    const t = localStorage.getItem('maillon_theme');
                    if (t === 'light' || t === 'dark') return t;
                } catch (e) {}
                return 'auto';
            }

            function appliquerTheme(choix) {
                const racine = document.documentElement;
                if (choix === 'light' || choix === 'dark') {
                    racine.setAttribute('data-theme', choix);
                } else {
                    racine.removeAttribute('data-theme');
                }
                try {
                    if (choix === 'auto') {
                        localStorage.removeItem('maillon_theme');
                    } else {
                        localStorage.setItem('maillon_theme', choix);
                    }
                } catch (e) {}

                const clair = choix === 'light' ||
                    (choix === 'auto' && window.matchMedia &&
                     window.matchMedia('(prefers-color-scheme: light)').matches);
                const meta = document.querySelector('meta[name="theme-color"]');
                if (meta) meta.setAttribute('content', clair ? '#eef4f3' : '#0b171c');

                document.querySelectorAll('#segmentsTheme button').forEach(b => {
                    b.classList.toggle('actif', b.dataset.themeChoix === choix);
                });
            }

            document.querySelectorAll('#segmentsTheme button').forEach(b => {
                const choix = b.dataset.themeChoix;
                remplirBouton(b, choix === 'light' ? 'soleil' : choix === 'dark' ? 'lune' : 'auto',
                    choix === 'light' ? 'Jour' : choix === 'dark' ? 'Nuit' : 'Auto');
                b.addEventListener('click', () => appliquerTheme(choix));
            });
            appliquerTheme(themeChoisi());
            if (window.matchMedia) {
                window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
                    if (themeChoisi() === 'auto') appliquerTheme('auto');
                });
            }

            let mrValeurs = {};
            let mrMaj = null;
            let mrEdition = false;

            function afficherReglagesVelo() {
                const zone = document.getElementById('mrContenu');
                zone.innerHTML = '';

                MR_GROUPES.forEach(g => {
                    const bloc = document.createElement('div');
                    bloc.className = 'mr-groupe';
                    const h = document.createElement('h3');
                    h.textContent = g.titre;
                    bloc.appendChild(h);

                    g.champs.forEach(([cle, libelle]) => {
                        const ligne = document.createElement('div');
                        ligne.className = 'mr-ligne';
                        const l = document.createElement('span');
                        l.textContent = libelle;
                        ligne.appendChild(l);

                        if (mrEdition) {
                            const inp = document.createElement('input');
                            inp.type = 'text';
                            inp.inputMode = cle === 'notes' ? 'text' : 'decimal';
                            inp.id = 'mr-' + cle;
                            inp.maxLength = 200;
                            inp.value = mrValeurs[cle] || '';
                            if (cle === 'notes') { inp.style.width = '100%'; inp.style.flex = '1'; inp.style.maxWidth = '200px'; }
                            ligne.appendChild(inp);
                        } else {
                            const v = document.createElement('b');
                            v.textContent = mrValeurs[cle] || '—';
                            ligne.appendChild(v);
                        }
                        bloc.appendChild(ligne);
                    });
                    zone.appendChild(bloc);
                });

                document.getElementById('mrActions').style.display =
                    mrEdition ? 'flex' : 'none';
                document.getElementById('mrModifier').style.display =
                    mrEdition ? 'none' : 'inline-flex';

                let txt = '';
                if (mrMaj) {
                    const p = mrMaj.split('-');
                    txt = `Mis à jour le ${p[2]}/${p[1]}/${p[0]}`;
                } else if (!mrEdition) {
                    txt = 'Aucune valeur notée pour le moment.';
                }
                document.getElementById('mrDate').textContent = txt;
            }

            async function chargerReglagesVelo() {
                try {
                    const res = await fetch('/api/mes-reglages', { headers: getHeaders() });
                    if (!res.ok) throw new Error();
                    const d = await res.json();
                    mrValeurs = d.valeurs || {};
                    mrMaj = d.maj_le || null;
                } catch (e) {
                    mrValeurs = {};
                }
                if (!mrEdition) afficherReglagesVelo();
            }

            function modifierReglagesVelo() {
                mrEdition = true;
                afficherReglagesVelo();
            }

            function annulerReglagesVelo() {
                mrEdition = false;
                afficherReglagesVelo();
            }

            async function enregistrerReglagesVelo() {
                const valeurs = {};
                MR_GROUPES.forEach(g => g.champs.forEach(([cle]) => {
                    valeurs[cle] = document.getElementById('mr-' + cle).value;
                }));

                try {
                    const res = await fetch('/api/mes-reglages', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ valeurs })
                    });
                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }
                    if (!res.ok) throw new Error();
                    mrEdition = false;
                    await chargerReglagesVelo();
                } catch (e) {
                    alert("Impossible d'enregistrer les réglages.");
                }
            }

            remplirBouton(document.getElementById('btnPlacer'), 'repere', 'Placer les repères');
            remplirBouton(document.getElementById('mrModifier'), 'crayon', 'Modifier');
            remplirBouton(document.getElementById('mrOk'), 'valider', 'Enregistrer');
            remplirBouton(document.getElementById('mrAnnuler'), 'fermer', 'Annuler');

            const cartesOuvertes = new Set();
            let vertesVisibles = false;
            let verifChoisie = null;

            function nomCourt(nom) {
                return nom.split(' - ')[0].split(' (')[0];
            }

            function texteDelai(c) {
                if (!(c.seuil_jours > 0) || c.jours_ecoules === null) return '';
                const reste = c.seuil_jours - c.jours_ecoules;
                return reste >= 0 ? `${reste} j` : `+${-reste} j`;
            }

            function afficherVerifications(verifs) {
                const zone = document.getElementById('verifs');
                const chips = document.getElementById('verifsChips');
                const detail = document.getElementById('verifDetail');
                chips.innerHTML = '';
                detail.style.display = 'none';

                if (verifs.length === 0) {
                    zone.style.display = 'none';
                    return;
                }
                zone.style.display = 'block';

                const aFaire = verifs.filter(v => v.pourcentage_usure >= 100).length;
                document.getElementById('verifsInfo').textContent =
                    aFaire > 0 ? `${aFaire} à faire` : 'Tout est à jour';

                verifs.sort((a, b) => b.pourcentage_usure - a.pourcentage_usure || a.id - b.id);

                verifs.forEach(v => {
                    const etat = v.pourcentage_usure >= 100 ? 'warning'
                        : v.pourcentage_usure >= 85 ? 'near-warning' : 'ok';
                    const chip = document.createElement('button');
                    chip.type = 'button';
                    chip.className = `chip ${etat}`;
                    chip.dataset.id = v.id;
                    chip.appendChild(document.createTextNode(nomCourt(v.nom)));
                    const delai = texteDelai(v);
                    if (delai) {
                        const pt = document.createElement('small');
                        pt.textContent = delai;
                        chip.appendChild(pt);
                    }
                    chip.addEventListener('click', () => {
                        verifChoisie = verifChoisie === v.id ? null : v.id;
                        montrerDetailVerification(verifs);
                    });
                    chips.appendChild(chip);
                });

                if (verifChoisie !== null && !verifs.some(v => v.id === verifChoisie)) {
                    verifChoisie = null;
                }
                montrerDetailVerification(verifs);
            }

            function montrerDetailVerification(verifs) {
                const detail = document.getElementById('verifDetail');
                document.querySelectorAll('#verifsChips .chip').forEach(ch => {
                    ch.classList.toggle('choisie', Number(ch.dataset.id) === verifChoisie);
                });

                const v = verifs.find(x => x.id === verifChoisie);
                if (!v) {
                    detail.style.display = 'none';
                    return;
                }

                detail.innerHTML = '';
                const nom = document.createElement('div');
                nom.className = 'vd-nom';
                nom.textContent = v.nom;
                const info = document.createElement('div');
                info.className = 'vd-info';
                info.textContent = v.seuil_jours > 0
                    ? `Fait il y a ${v.jours_ecoules} j · à refaire tous les ${v.seuil_jours} j`
                    : `Seuil : ${v.seuil_alerte} km`;
                detail.appendChild(nom);
                detail.appendChild(info);

                const bFait = document.createElement('button');
                bFait.className = 'btn-action';
                remplirBouton(bFait, 'cle', 'Marquer fait');
                bFait.addEventListener('click', () => remplacer(v.id, v.nom));
                detail.appendChild(bFait);

                const outils = document.createElement('div');
                outils.className = 'card-tools';
                const bSeuils = document.createElement('button');
                bSeuils.type = 'button';
                remplirBouton(bSeuils, 'crayon', 'Seuils');
                bSeuils.addEventListener('click', () => modifierSeuils(v));
                const bRetirer = document.createElement('button');
                bRetirer.type = 'button';
                bRetirer.className = 'danger';
                remplirBouton(bRetirer, 'poubelle', 'Retirer');
                bRetirer.addEventListener('click', () => retirerComposant(v));
                outils.appendChild(bSeuils);
                outils.appendChild(bRetirer);
                detail.appendChild(outils);
                detail.style.display = 'block';
            }
            let placementActif = false;
            let placementId = null;

            function basculerCarte(card, id) {
                const ouverte = !card.classList.contains('ouverte');
                card.classList.toggle('ouverte', ouverte);
                card.querySelector('.card-top').setAttribute('aria-expanded', String(ouverte));
                if (ouverte) cartesOuvertes.add(id); else cartesOuvertes.delete(id);
                majBoutonDeplier();
            }

            function majBoutonDeplier() {
                const b = document.getElementById('btnToutDeplier');
                if (!b) return;
                const cartes = document.querySelectorAll('#composantsList .card:not(.masquee)');
                const total = cartes.length;
                const ouvertes = document.querySelectorAll('#composantsList .card.ouverte:not(.masquee)').length;
                const tout = total > 0 && ouvertes >= total;
                remplirBouton(b, tout ? 'haut' : 'bas', tout ? 'Tout replier' : 'Tout déplier');
            }

            function toutDeplier() {
                const cartes = document.querySelectorAll('#composantsList .card:not(.masquee)');
                const tout = document.querySelectorAll('#composantsList .card.ouverte:not(.masquee)').length >= cartes.length;
                cartes.forEach(card => {
                    const id = Number(card.id.replace('card-', ''));
                    card.classList.toggle('ouverte', !tout);
                    card.querySelector('.card-top').setAttribute('aria-expanded', String(!tout));
                    if (tout) cartesOuvertes.delete(id); else cartesOuvertes.add(id);
                });
                majBoutonDeplier();
            }

            function basculerPlacement() {
                placementActif = !placementActif;
                placementId = null;
                const bp = document.getElementById('btnPlacer');
                remplirBouton(bp, placementActif ? 'valider' : 'repere',
                    placementActif ? 'Terminer le placement' : 'Placer les repères');
                bp.classList.toggle('actif', placementActif);
                document.getElementById('placementAide').style.display =
                    placementActif ? 'inline' : 'none';
                document.querySelector('.bike-container').classList.toggle('placement', placementActif);
                document.querySelectorAll('.hotspot').forEach(h => h.classList.remove('choisi'));
            }

            async function deplacerRepere(event) {
                if (!placementActif || placementId === null) return;
                if (event.target.classList.contains('hotspot')) return;

                const rect = event.currentTarget.getBoundingClientRect();
                const x = Math.round((event.clientX - rect.left) / rect.width * 100);
                const y = Math.round((event.clientY - rect.top) / rect.height * 100);

                try {
                    const res = await fetch(`/api/composants/${placementId}/position`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({
                            pos_x: Math.max(0, Math.min(100, x)),
                            pos_y: Math.max(0, Math.min(100, y))
                        })
                    });
                    if (!res.ok) throw new Error();
                    const garde = placementId;
                    await chargerEtat();
                    placementId = garde;
                    document.querySelectorAll('.hotspot').forEach(h => {
                        h.classList.toggle('choisi', Number(h.dataset.id) === garde);
                    });
                } catch (e) {
                    alert("Impossible de déplacer le repère.");
                }
            }

            async function modifierSeuils(c) {
                const km = prompt(
                    `Seuil en km pour « ${c.nom} » (0 = aucun) :`,
                    c.seuil_alerte || 0
                );
                if (km === null) return;
                const jours = prompt(
                    `Seuil en jours pour « ${c.nom} » (0 = aucun) :`,
                    c.seuil_jours || 0
                );
                if (jours === null) return;

                try {
                    const res = await fetch(`/api/composants/${c.id}/seuils`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({
                            seuil_alerte: parseInt(km, 10) || 0,
                            seuil_jours: parseInt(jours, 10) || 0
                        })
                    });
                    if (!res.ok) {
                        const err = await res.json().catch(() => ({}));
                        alert(err.detail || "Seuils refusés.");
                        return;
                    }
                    await chargerEtat();
                } catch (e) {
                    alert("Impossible de modifier les seuils.");
                }
            }

            async function retirerComposant(c) {
                if (!confirm(`Retirer « ${c.nom} » de la liste ? Son historique sera aussi supprimé.`)) {
                    return;
                }
                try {
                    const res = await fetch(`/api/composants/${c.id}/supprimer`, {
                        method: 'POST',
                        headers: getHeaders()
                    });
                    if (!res.ok) throw new Error();
                    await chargerEtat();
                } catch (e) {
                    alert("Impossible de retirer cette pièce.");
                }
            }

            async function chargerEtat() {
                try {
                    const res = await fetch('/api/etat', { headers: getHeaders() });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error("Erreur lors du chargement des composants.");
                    }

                    const data = await res.json();

                    document.getElementById('veloInfo').innerText =
                        `${Number(data.km_total).toLocaleString('fr-FR')} km`;

                    const container =
                        document.getElementById('composantsList');

                    const hotspotsContainer =
                        document.getElementById('hotspotsContainer');

                    const selectComp =
                        document.getElementById('histComposant');

                    container.innerHTML = '';
                    hotspotsContainer.innerHTML = '';
                    selectComp.innerHTML = '';

                    const boite = document.querySelector('.bike-container');
                    boite.onclick = deplacerRepere;
                    const verifs = [];

                    data.composants.forEach((c, index) => {
                        let statusClass = 'ok';
                        let hotspotClass = '';

                        if (c.pourcentage_usure >= 100) {
                            statusClass = 'warning';
                            hotspotClass = 'warning';
                        } else if (c.pourcentage_usure >= 85) {
                            statusClass = 'near-warning';
                            hotspotClass = 'near-warning';
                        }

                        const pctAffiche =
                            c.pourcentage_usure > 999
                                ? '999%+'
                                : `${c.pourcentage_usure}%`;

                        const badgeText =
                            c.pourcentage_usure >= 100
                                ? `À faire · ${pctAffiche}`
                                : pctAffiche;

                        const estMaintenance = c.categorie === 'maintenance';
                        const btnLabel = estMaintenance ? 'Marquer fait' : 'Remplacer';

                        const option = document.createElement('option');
                        option.value = c.id;
                        option.textContent = c.nom;
                        selectComp.appendChild(option);

                        if (c.verification) {
                            verifs.push(c);
                            return;
                        }

                        const hotspot = document.createElement('div');
                        hotspot.className = `hotspot ${hotspotClass}`;
                        hotspot.style.top = `${c.pos_y}%`;
                        hotspot.style.left = `${c.pos_x}%`;
                        hotspot.title = `${c.nom} (${c.pourcentage_usure}%)`;
                        hotspot.textContent = c.numero || (index + 1);
                        hotspot.dataset.id = c.id;

                        hotspot.addEventListener('click', () => {
                            if (placementActif) {
                                placementId = c.id;
                                document.querySelectorAll('.hotspot').forEach(h => {
                                    h.classList.toggle('choisi', h === hotspot);
                                });
                                return;
                            }
                            remplacer(c.id, c.nom);
                        });

                        hotspotsContainer.appendChild(hotspot);

                        const card = document.createElement('div');
                        card.className = `card ${statusClass}`;
                        card.id = `card-${c.id}`;

                        const cardTop = document.createElement('div');
                        cardTop.className = 'card-top';

                        const componentName = document.createElement('div');
                        componentName.className = 'component-name';

                        const pastille = document.createElement('span');
                        pastille.className = `num ${statusClass}`;
                        pastille.textContent = c.numero || (index + 1);
                        const libelle = document.createElement('span');
                        libelle.className = 'nom';
                        libelle.textContent = c.nom;
                        componentName.appendChild(pastille);
                        componentName.appendChild(libelle);

                        const badge = document.createElement('div');
                        badge.className = `badge ${statusClass}`;
                        badge.textContent = badgeText;

                        cardTop.appendChild(componentName);
                        cardTop.appendChild(badge);

                        const statsRow = document.createElement('div');
                        statsRow.className = 'stats-row';

                        if (c.seuil_alerte > 0) {
                            const kmText = document.createElement('span');
                            kmText.textContent =
                                `Parcouru depuis l'intervention : ${c.km_actuel} km`;

                            const thresholdText = document.createElement('span');
                            thresholdText.textContent =
                                `Seuil : ${c.seuil_alerte} km`;

                            statsRow.appendChild(kmText);
                            statsRow.appendChild(thresholdText);
                        }

                        if (c.seuil_jours > 0) {
                            const jTxt = document.createElement('span');
                            jTxt.textContent =
                                `Temps écoulé : ${c.jours_ecoules} j`;
                            const jSeuil = document.createElement('span');
                            jSeuil.textContent =
                                `Tous les ${c.seuil_jours} j`;
                            statsRow.appendChild(jTxt);
                            statsRow.appendChild(jSeuil);
                        }

                        const barBg = document.createElement('div');
                        barBg.className = 'bar-bg';

                        const barFill = document.createElement('div');
                        barFill.className = `bar-fill ${statusClass}`;
                        barFill.style.width =
                            `${Math.min(c.pourcentage_usure, 100)}%`;

                        barBg.appendChild(barFill);

                        const button = document.createElement('button');
                        button.className = 'btn-action';
                        remplirBouton(button, estMaintenance ? 'cle' : 'echange', btnLabel);
                        button.addEventListener('click', () => {
                            remplacer(c.id, c.nom);
                        });

                        const chevron = document.createElement('span');
                        chevron.className = 'chevron';
                        chevron.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">'
                            + ICONES.bas + '</svg>';
                        cardTop.appendChild(chevron);
                        cardTop.setAttribute('role', 'button');
                        cardTop.setAttribute('tabindex', '0');
                        cardTop.setAttribute('aria-expanded', 'false');
                        cardTop.addEventListener('click', () => basculerCarte(card, c.id));
                        cardTop.addEventListener('keydown', (ev) => {
                            if (ev.key === 'Enter' || ev.key === ' ') {
                                ev.preventDefault();
                                basculerCarte(card, c.id);
                            }
                        });

                        const detail = document.createElement('div');
                        detail.className = 'card-detail';

                        card.appendChild(cardTop);
                        card.appendChild(barBg);
                        card.appendChild(detail);
                        detail.appendChild(statsRow);

                        if (
                            c.seuil_alerte > 0 &&
                            c.km_usure !== undefined &&
                            Math.abs(c.km_usure - c.km_actuel) >= 1
                        ) {
                            const usureInfo = document.createElement('div');
                            usureInfo.className = 'form-help';
                            usureInfo.style.margin = '0 0 6px';
                            usureInfo.textContent =
                                `Usure estimée selon vos sorties : ${c.km_usure} km`;
                            detail.appendChild(usureInfo);
                        }

                        detail.appendChild(button);

                        const outils = document.createElement('div');
                        outils.className = 'card-tools';

                        const bSeuils = document.createElement('button');
                        bSeuils.type = 'button';
                        remplirBouton(bSeuils, 'crayon', 'Seuils');
                        bSeuils.addEventListener('click', () => modifierSeuils(c));

                        const bRetirer = document.createElement('button');
                        bRetirer.type = 'button';
                        bRetirer.className = 'danger';
                        remplirBouton(bRetirer, 'poubelle', 'Retirer');
                        bRetirer.addEventListener('click', () => retirerComposant(c));

                        outils.appendChild(bSeuils);
                        outils.appendChild(bRetirer);
                        detail.appendChild(outils);

                        if (cartesOuvertes.has(c.id)) {
                            card.classList.add('ouverte');
                            cardTop.setAttribute('aria-expanded', 'true');
                        }

                        if (!vertesVisibles && c.pourcentage_usure < 85) {
                            card.classList.add('masquee');
                        }

                        container.appendChild(card);
                    });

                    afficherVerifications(verifs);

                    const nbPieces = data.composants.filter(c => !c.verification).length;
                    const entete = document.createElement('div');
                    entete.className = 'liste-entete';
                    const titreListe = document.createElement('strong');
                    titreListe.textContent = `${nbPieces} pièces à suivre`;
                    const bTout = document.createElement('button');
                    bTout.type = 'button';
                    bTout.id = 'btnToutDeplier';
                    bTout.addEventListener('click', toutDeplier);
                    entete.appendChild(titreListe);
                    entete.appendChild(bTout);
                    container.insertBefore(entete, container.firstChild);

                    const nbVertes = data.composants.filter(
                        c => !c.verification && c.pourcentage_usure < 85
                    ).length;
                    titreListe.textContent = vertesVisibles || nbVertes === 0
                        ? `${nbPieces} pièces à suivre`
                        : `${nbPieces - nbVertes} à surveiller · ${nbVertes} en bon état`;

                    if (nbVertes > 0) {
                        const bVertes = document.createElement('button');
                        bVertes.type = 'button';
                        bVertes.id = 'btnVertes';
                        bVertes.className = 'btn-voir-plus';
                        remplirBouton(bVertes, vertesVisibles ? 'haut' : 'bas',
                            vertesVisibles
                                ? 'Masquer les pièces en bon état'
                                : `Voir les ${nbVertes} pièces en bon état`);
                        bVertes.addEventListener('click', () => {
                            vertesVisibles = !vertesVisibles;
                            chargerEtat();
                        });
                        container.appendChild(bVertes);
                    }
                    majBoutonDeplier();

                    await chargerReglagesVelo();
                    await chargerHistorique();
                    await chargerSorties();
                    await chargerReglages();

                } catch (e) {
                    console.error(e);
                    document.getElementById('composantsList').innerHTML =
                        '<div class="loading">Impossible de charger les composants.</div>';
                }
            }


            async function chargerHistorique() {
                try {
                    const res = await fetch('/api/historique', { headers: getHeaders() });
                    if (!res.ok) throw new Error();

                    const history = await res.json();
                    const listDiv = document.getElementById('historyList');

                    listDiv.innerHTML = '';

                    if (history.length === 0) {
                        listDiv.innerHTML = `
                            <p style="color: var(--text-muted); font-size: 0.85rem;">
                                Aucune intervention enregistrée.
                            </p>
                        `;
                        return;
                    }

                    history.forEach(h => {
                        const item = document.createElement('div');
                        item.className = 'history-item';

                        const details = document.createElement('div');
                        details.className = 'history-details';

                        const title = document.createElement('strong');
                        title.textContent = h.composant_nom;

                        const detailText = document.createElement('div');
                        detailText.style.color = 'var(--text-muted)';
                        detailText.style.marginTop = '4px';

                        const kmText =
                            h.km_intervention !== null &&
                            h.km_intervention !== undefined
                                ? `🚲 À ${h.km_intervention} km`
                                : '🚲 Kilométrage non renseigné';

                        detailText.textContent =
                            `${h.notes || 'Intervention réalisée'} · ` +
                            `${h.date_remplacement} · ${kmText}`;

                        details.appendChild(title);
                        details.appendChild(detailText);

                        const boutonSuppr = document.createElement('button');
                        boutonSuppr.className = 'btn-action';
                        boutonSuppr.style.marginTop = '8px';
                        boutonSuppr.style.width = 'auto';
                        boutonSuppr.style.padding = '6px 12px';
                        boutonSuppr.textContent = '🗑️ Supprimer';
                        boutonSuppr.addEventListener('click', () => {
                            supprimerIntervention(
                                h.id,
                                h.composant_nom,
                                h.date_remplacement
                            );
                        });
                        details.appendChild(boutonSuppr);

                        const cost = document.createElement('div');
                        cost.className = 'history-cost';
                        cost.textContent =
                            h.cout > 0 ? `${h.cout} €` : '';

                        item.appendChild(details);
                        item.appendChild(cost);

                        listDiv.appendChild(item);
                    });

                } catch (e) {
                    console.error(e);
                }
            }


            async function supprimerIntervention(id, nom, jour) {
                const confirmation = confirm(
                    `Supprimer l'intervention « ${nom} » du ${jour} ? ` +
                    `La pièce reviendra au point de départ de l'intervention ` +
                    `précédente (ou à 0 km s'il n'y en a pas).`
                );

                if (!confirmation) {
                    return;
                }

                try {
                    const res = await fetch(`/api/historique/${id}/supprimer`, {
                        method: 'POST',
                        headers: getHeaders()
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    // Recharge tout : l'usure de la pièce change.
                    await chargerEtat();

                } catch (e) {
                    alert("Suppression impossible.");
                }
            }


            async function syncKm() {
                const input = document.getElementById('kmInput');
                const km = parseInt(input.value, 10);

                if (isNaN(km) || km < 0) {
                    alert("Veuillez saisir un kilométrage valide.");
                    return;
                }

                try {
                    const res = await fetch('/api/sync', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ km_total: km })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail);

                    input.value = '';
                    await chargerEtat();

                } catch (e) {
                    alert(e.message || "Impossible de mettre à jour le kilométrage.");
                }
            }


            async function remplacer(id, nom) {
                if (!confirm(`Confirmer l'intervention pour : ${nom} ?`)) {
                    return;
                }

                const aujourdhui = new Date().toISOString().split('T')[0];

                try {
                    const res = await fetch(`/api/composants/${id}/remplacer`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({
                            notes: "Intervention standard",
                            cout: 0.0,
                            date_remplacement: aujourdhui
                        })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) throw new Error();
                    await chargerEtat();

                } catch (e) {
                    alert("Impossible d'enregistrer l'intervention.");
                }
            }


            async function ajouterHistorique() {
                const composant_id = parseInt(
                    document.getElementById('histComposant').value,
                    10
                );

                const date_remplacement =
                    document.getElementById('histDate').value;

                const km_intervention = parseInt(
                    document.getElementById('histKm').value,
                    10
                );

                const cout =
                    parseFloat(document.getElementById('histCout').value) || 0;

                const notes =
                    document.getElementById('histNotes').value.trim()
                    || "Ancien entretien";

                if (!date_remplacement) {
                    alert("Veuillez sélectionner la date de l'intervention.");
                    return;
                }

                if (isNaN(km_intervention) || km_intervention < 0) {
                    alert("Veuillez saisir un kilométrage valide.");
                    return;
                }

                try {
                    const res = await fetch('/api/historique', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({
                            composant_id,
                            date_remplacement,
                            km_intervention,
                            cout,
                            notes
                        })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail);

                    document.getElementById('histDate').value = '';
                    document.getElementById('histKm').value = '';
                    document.getElementById('histCout').value = '';
                    document.getElementById('histNotes').value = '';

                    await chargerEtat();

                } catch (e) {
                    alert(e.message || "Une erreur est survenue.");
                }
            }


            const LIBELLES_TERRAIN = {
                mixte: 'Mixte',
                route: 'Route',
                chemin: 'Chemin',
                sentier: 'Sentier technique'
            };


            function formaterDuree(secondes) {
                const minutesTotales = Math.round(secondes / 60);
                const h = Math.floor(minutesTotales / 60);
                const m = minutesTotales % 60;

                return h > 0
                    ? `${h} h ${String(m).padStart(2, '0')}`
                    : `${m} min`;
            }


            function dessinerTrace(trace) {
                const ns = 'http://www.w3.org/2000/svg';
                const svg = document.createElementNS(ns, 'svg');

                svg.setAttribute('viewBox', '0 0 100 60');
                svg.setAttribute('width', '90');
                svg.setAttribute('height', '54');
                svg.style.flexShrink = '0';

                if (!trace || trace.length < 2) {
                    return svg;
                }

                const lats = trace.map(p => p[0]);
                const lons = trace.map(p => p[1]);
                const minLat = Math.min(...lats);
                const maxLat = Math.max(...lats);
                const minLon = Math.min(...lons);
                const maxLon = Math.max(...lons);

                const kx = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180);
                const largeur = ((maxLon - minLon) * kx) || 1e-6;
                const hauteur = (maxLat - minLat) || 1e-6;
                const echelle = Math.min(92 / largeur, 52 / hauteur);
                const dx = (100 - largeur * echelle) / 2;
                const dy = (60 - hauteur * echelle) / 2;

                const points = trace.map(p => {
                    const x = dx + (p[1] - minLon) * kx * echelle;
                    const y = dy + (maxLat - p[0]) * echelle;
                    return `${x.toFixed(1)},${y.toFixed(1)}`;
                }).join(' ');

                const ligne = document.createElementNS(ns, 'polyline');
                ligne.setAttribute('points', points);
                ligne.setAttribute('fill', 'none');
                ligne.setAttribute('stroke', '#3b82f6');
                ligne.setAttribute('stroke-width', '2');
                ligne.setAttribute('stroke-linejoin', 'round');
                ligne.setAttribute('stroke-linecap', 'round');

                svg.appendChild(ligne);

                return svg;
            }


            async function chargerSorties() {
                const liste = document.getElementById('sortiesList');

                try {
                    const res = await fetch('/api/sorties', { headers: getHeaders() });
                    if (!res.ok) throw new Error();

                    const sorties = await res.json();
                    liste.innerHTML = '';

                    if (sorties.length === 0) {
                        const vide = document.createElement('p');
                        vide.style.color = 'var(--text-muted)';
                        vide.style.fontSize = '0.85rem';
                        vide.textContent = 'Aucune sortie importée.';
                        liste.appendChild(vide);
                        return;
                    }

                    sorties.forEach(s => {
                        const item = document.createElement('div');
                        item.className = 'history-item';

                        const details = document.createElement('div');
                        details.className = 'history-details';

                        const titre = document.createElement('strong');
                        const jour = new Date(s.debut).toLocaleDateString('fr-FR');
                        titre.textContent = `${s.nom} · ${jour}`;

                        const ligne1 = document.createElement('div');
                        ligne1.style.color = 'var(--text-muted)';
                        ligne1.style.marginTop = '4px';
                        ligne1.textContent =
                            `${s.distance_km} km · ${s.denivele_pos} m D+ · ` +
                            `${formaterDuree(s.duree_s)} · ${s.vitesse_moy} km/h`;

                        const ligne2 = document.createElement('div');
                        ligne2.style.color = 'var(--text-muted)';
                        ligne2.style.display = 'flex';
                        ligne2.style.flexWrap = 'wrap';
                        ligne2.style.alignItems = 'center';
                        ligne2.style.gap = '6px';
                        ligne2.style.marginTop = '4px';

                        const menuTerrain = document.createElement('select');
                        menuTerrain.setAttribute('aria-label', 'Terrain de la sortie');
                        menuTerrain.style.cssText =
                            'width:auto;flex:0 0 auto;margin:0;padding:3px 6px;font-size:0.8rem;';

                        const cles = Object.keys(LIBELLES_TERRAIN);

                        if (!cles.includes(s.terrain)) {
                            cles.push(s.terrain);
                        }

                        cles.forEach(cle => {
                            const choix = document.createElement('option');
                            choix.value = cle;
                            choix.textContent = LIBELLES_TERRAIN[cle] || cle;
                            menuTerrain.appendChild(choix);
                        });

                        menuTerrain.value = s.terrain;
                        menuTerrain.addEventListener('change', () => {
                            changerTerrain(s.id, menuTerrain.value, menuTerrain, s.terrain);
                        });

                        ligne2.appendChild(menuTerrain);

                        if (s.notes) {
                            const note = document.createElement('span');
                            note.textContent = `· ${s.notes}`;
                            ligne2.appendChild(note);
                        }

                        const bouton = document.createElement('button');
                        bouton.className = 'btn-action';
                        bouton.style.marginTop = '8px';
                        bouton.style.width = 'auto';
                        bouton.style.padding = '6px 12px';
                        bouton.textContent = '🗑️ Supprimer';
                        bouton.addEventListener('click', () => {
                            supprimerSortie(s.id);
                        });

                        const nbPhotos = s.photos ? s.photos.length : 0;
                        const boutonPhotos = document.createElement('button');
                        boutonPhotos.className = 'btn-action';
                        boutonPhotos.style.marginTop = '8px';
                        boutonPhotos.style.marginRight = '8px';
                        boutonPhotos.style.width = 'auto';
                        boutonPhotos.style.padding = '6px 12px';
                        boutonPhotos.textContent = '📷 Ajouter des photos';
                        boutonPhotos.disabled = nbPhotos >= MAX_PHOTOS;

                        const choixPhotos = document.createElement('input');
                        choixPhotos.type = 'file';
                        choixPhotos.accept = 'image/*';
                        choixPhotos.multiple = true;
                        choixPhotos.style.display = 'none';
                        choixPhotos.addEventListener('change', () => {
                            ajouterPhotos(s.id, nbPhotos, choixPhotos, boutonPhotos);
                        });
                        boutonPhotos.addEventListener('click', () => {
                            choixPhotos.click();
                        });

                        details.appendChild(titre);
                        details.appendChild(ligne1);
                        details.appendChild(ligne2);
                        details.appendChild(boutonPhotos);
                        details.appendChild(choixPhotos);
                        details.appendChild(bouton);

                        item.appendChild(details);

                        const mediaDiv = document.createElement('div');
                        mediaDiv.className = 'sortie-media';

                        if (s.photos && s.photos.length > 0) {
                            const gallery = document.createElement('div');
                            gallery.className = 'photos-gallery';
                            s.photos.forEach((pData, numero) => {
                                const img = document.createElement('img');
                                img.src = pData;
                                img.className = 'sortie-img';
                                img.style.cursor = 'pointer';
                                img.addEventListener('click', () => {
                                    ouvrirPhoto(s.id, numero, pData);
                                });
                                gallery.appendChild(img);
                            });
                            mediaDiv.appendChild(gallery);
                        }

                        mediaDiv.appendChild(dessinerTrace(s.trace));
                        item.appendChild(mediaDiv);

                        liste.appendChild(item);
                    });

                } catch (e) {
                    console.error(e);
                }
            }


            const MAX_PHOTOS = 8;

            document.getElementById('lightboxRetirer')
                .addEventListener('click', retirerPhotoOuverte);


            // Réduit une photo dans le navigateur avant l'envoi :
            // une version moyenne (affichée au clic) et une miniature.
            async function preparerPhoto(fichier) {
                const image = await createImageBitmap(fichier);

                const encoder = (cote, qualite) => {
                    const echelle = Math.min(
                        1,
                        cote / Math.max(image.width, image.height)
                    );
                    const canvas = document.createElement('canvas');
                    canvas.width = Math.max(1, Math.round(image.width * echelle));
                    canvas.height = Math.max(1, Math.round(image.height * echelle));
                    canvas.getContext('2d').drawImage(
                        image, 0, 0, canvas.width, canvas.height
                    );
                    return canvas.toDataURL('image/jpeg', qualite);
                };

                let photo = '';

                // On essaie des réglages de plus en plus légers
                // jusqu'à passer sous la limite du serveur.
                const essais = [[1280, 0.75], [1280, 0.6], [1024, 0.55], [800, 0.5]];

                for (const [cote, qualite] of essais) {
                    photo = encoder(cote, qualite);

                    if (photo.length <= 650000) {
                        break;
                    }
                }

                const mini = encoder(240, 0.7);

                if (image.close) {
                    image.close();
                }

                if (
                    !photo.startsWith('data:image/jpeg') ||
                    photo.length > 700000
                ) {
                    throw new Error('photo');
                }

                return { mini: mini, photo: photo };
            }


            let photoOuverte = null;


            async function retirerPhotoOuverte(evenement) {
                // Ne pas fermer la fenêtre par le clic sur le bouton lui-même.
                evenement.stopPropagation();

                if (!photoOuverte) {
                    return;
                }

                const { sortieId, numero } = photoOuverte;

                if (!confirm("Retirer cette photo de la sortie ?")) {
                    return;
                }

                try {
                    const res = await fetch(
                        `/api/sorties/${sortieId}/photos/${numero}/supprimer`,
                        { method: 'POST', headers: getHeaders() }
                    );

                    if (res.status === 401) {
                        fermerPhoto();
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    fermerPhoto();
                    await chargerEtat();

                } catch (e) {
                    alert("Impossible de retirer la photo.");
                }
            }


            async function ouvrirPhoto(sortieId, numero, miniature) {
                const fenetre = document.getElementById('lightbox');
                const image = document.getElementById('lightboxImg');

                photoOuverte = { sortieId: sortieId, numero: numero };

                // La miniature s'affiche tout de suite, la photo suit.
                image.src = miniature;
                fenetre.style.display = 'flex';

                try {
                    const res = await fetch(
                        `/api/sorties/${sortieId}/photos/${numero}`,
                        { headers: getHeaders() }
                    );

                    if (res.ok && fenetre.style.display === 'flex') {
                        const data = await res.json();

                        if (data.image) {
                            image.src = data.image;
                        }
                    }
                } catch (e) {
                    console.error(e);
                }
            }


            function fermerPhoto() {
                photoOuverte = null;
                document.getElementById('lightbox').style.display = 'none';
                document.getElementById('lightboxImg').removeAttribute('src');
            }


            async function telechargerSauvegarde() {
                try {
                    const res = await fetch('/api/sauvegarde', {
                        headers: getHeaders()
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    const blob = await res.blob();
                    const lien = document.createElement('a');
                    const adresse = URL.createObjectURL(blob);

                    lien.href = adresse;
                    lien.download =
                        `sauvegarde_velo_${new Date().toISOString().slice(0, 10)}.json`;

                    document.body.appendChild(lien);
                    lien.click();
                    lien.remove();

                    setTimeout(() => URL.revokeObjectURL(adresse), 2000);

                } catch (e) {
                    alert("Sauvegarde impossible.");
                }
            }


            const MAX_FICHIERS_GPX = 30;


            // Affiche des lignes de texte dans un élément (sans HTML : sûr).
            function afficherLignes(element, lignes) {
                element.replaceChildren();

                lignes.forEach(texte => {
                    const ligne = document.createElement('div');
                    ligne.textContent = texte;
                    element.appendChild(ligne);
                });
            }


            function nombre(n, singulier, pluriel) {
                return `${n} ${n > 1 ? pluriel : singulier}`;
            }


            function texteErreur(data, defaut) {
                return data && typeof data.detail === 'string'
                    ? data.detail
                    : defaut;
            }


            // Envoie un fichier GPX au serveur et rend le statut et la réponse.
            async function envoyerSortie(gpx, photos) {
                return envoyerCharge(
                    { gpx: gpx, photos: photos },
                    {
                        terrain: document.getElementById('gpxTerrain').value,
                        notes: document.getElementById('gpxNotes').value.trim() || null
                    }
                );
            }


            // charge : { gpx } ou { fit } (+ nom, photos) ; reglages : { terrain, notes }
            async function envoyerCharge(charge, reglages) {
                const res = await fetch('/api/sorties/import', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify(Object.assign({}, charge, {
                        terrain: reglages.terrain,
                        notes: reglages.notes
                    }))
                });

                let data = null;

                try {
                    data = await res.json();
                } catch (e) {
                    data = null;
                }

                return { status: res.status, data: data };
            }


            async function importerGpx() {
                const fichiersGpx = Array.from(document.getElementById('gpxFile').files);
                const fichiersPhotos = document.getElementById('gpxPhotos').files;
                const message = document.getElementById('gpxMessage');
                const bouton = document.getElementById('gpxBouton');

                if (fichiersGpx.length === 0) {
                    afficherLignes(message, ["Choisissez d'abord un fichier GPX."]);
                    return;
                }

                if (fichiersGpx.length > MAX_FICHIERS_GPX) {
                    afficherLignes(message, [
                        `${MAX_FICHIERS_GPX} fichiers maximum à la fois.`
                    ]);
                    return;
                }

                if (fichiersGpx.length > 1 && fichiersPhotos.length > 0) {
                    afficherLignes(message, [
                        "Les photos se joignent à une seule sortie à la fois : " +
                        "retirez-les ou importez les sorties une par une."
                    ]);
                    return;
                }

                // Le bouton est bloqué pendant l'import : pas de double envoi.
                bouton.disabled = true;

                try {
                    if (fichiersGpx.length === 1) {
                        await importerUneSortie(fichiersGpx[0], fichiersPhotos, message);
                    } else {
                        await importerPlusieursSorties(fichiersGpx, message);
                    }
                } finally {
                    bouton.disabled = false;
                }
            }


            async function importerUneSortie(fichierGpx, fichiersPhotos, message) {
                afficherLignes(message, ['Import en cours...']);

                try {
                    const gpx = await fichierGpx.text();
                    const photosList = [];

                    if (fichiersPhotos.length > MAX_PHOTOS) {
                        afficherLignes(message, [
                            `${MAX_PHOTOS} photos maximum par sortie.`
                        ]);
                        return;
                    }

                    for (let i = 0; i < fichiersPhotos.length; i++) {
                        afficherLignes(message, [
                            `Préparation des photos (${i + 1}/${fichiersPhotos.length})...`
                        ]);

                        try {
                            photosList.push(await preparerPhoto(fichiersPhotos[i]));
                        } catch (e) {
                            afficherLignes(message, [
                                `La photo « ${fichiersPhotos[i].name} » ne peut pas être lue ` +
                                `(format non pris en charge). Retirez-la ou choisissez un JPEG.`
                            ]);
                            return;
                        }
                    }

                    afficherLignes(message, ['Import en cours...']);

                    const r = await envoyerSortie(gpx, photosList);

                    if (r.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (r.status < 200 || r.status >= 300) {
                        throw new Error(texteErreur(r.data, "Fichier refusé."));
                    }

                    const data = r.data;

                    afficherLignes(message, [
                        `✅ Sortie importée : ${data.distance_km} km, ` +
                        `${data.denivele_pos} m D+. ` +
                        (data.ajoute_au_total
                            ? `Kilométrage total : ${data.km_total} km.`
                            : "Cette sortie date d'avant votre dernière mise à jour " +
                              "du kilométrage : elle n'a pas été ajoutée au total.")
                    ]);

                    document.getElementById('gpxFile').value = '';
                    document.getElementById('gpxPhotos').value = '';
                    document.getElementById('gpxNotes').value = '';

                    await chargerEtat();

                } catch (e) {
                    afficherLignes(message, [e.message || "Import impossible."]);
                }
            }


            async function importerPlusieursSorties(fichiersGpx, message) {
                const elements = fichiersGpx.map(fichier => ({
                    nom: fichier.name,
                    charger: async () => ({ gpx: await fichier.text() })
                }));

                await importerLot(
                    elements,
                    message,
                    {
                        terrain: document.getElementById('gpxTerrain').value,
                        notes: document.getElementById('gpxNotes').value.trim() || null
                    },
                    'fichiers',
                    () => {
                        document.getElementById('gpxFile').value = '';
                        document.getElementById('gpxNotes').value = '';
                    }
                );
            }


            // elements : [{ nom, charger: async () => charge }]
            async function importerLot(elements, message, reglages, mot, terminer) {
                const bilan = {
                    traitees: 0,
                    importees: 0,
                    ajoutees: 0,
                    anterieures: 0,
                    deja: 0,
                    refusees: [],
                    kmTotal: null,
                    interrompu: null
                };

                for (let i = 0; i < elements.length; i++) {
                    const element = elements[i];

                    afficherLignes(message, [
                        `Import de la sortie ${i + 1}/${elements.length}...`
                    ]);

                    let charge;

                    try {
                        charge = await element.charger();
                    } catch (e) {
                        bilan.traitees++;
                        bilan.refusees.push(`${element.nom} : fichier illisible`);
                        continue;
                    }

                    let r;

                    try {
                        r = await envoyerCharge(charge, reglages);
                    } catch (e) {
                        bilan.interrompu = "connexion perdue";
                        break;
                    }

                    if (r.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        bilan.interrompu = "code d'accès à saisir de nouveau";
                        break;
                    }

                    if (r.status === 503) {
                        bilan.interrompu = "code d'accès non réglé sur le serveur";
                        break;
                    }

                    bilan.traitees++;

                    if (r.status === 409) {
                        bilan.deja++;
                    } else if (r.status >= 200 && r.status < 300) {
                        bilan.importees++;
                        bilan.kmTotal = r.data.km_total;

                        if (r.data.ajoute_au_total) {
                            bilan.ajoutees++;
                        } else {
                            bilan.anterieures++;
                        }
                    } else {
                        const raison = r.status >= 500
                            ? 'erreur du serveur'
                            : texteErreur(r.data, 'fichier refusé');

                        bilan.refusees.push(`${element.nom} : ${raison}`);
                    }
                }

                const lignes = [];

                if (bilan.importees > 0) {
                    let ligne =
                        `✅ ${nombre(bilan.importees, 'sortie importée', 'sorties importées')}.`;

                    if (bilan.ajoutees > 0) {
                        ligne +=
                            ` ${nombre(bilan.ajoutees, 'ajoutée', 'ajoutées')} ` +
                            `au kilométrage total (${bilan.kmTotal} km).`;
                    }

                    if (bilan.anterieures > 0) {
                        ligne +=
                            ` ${nombre(bilan.anterieures, 'antérieure', 'antérieures')} ` +
                            `à votre dernière mise à jour du kilométrage, ` +
                            `donc déjà comptée${bilan.anterieures > 1 ? 's' : ''}.`;
                    }

                    lignes.push(ligne);
                }

                if (bilan.deja > 0) {
                    lignes.push(
                        `ℹ️ ${nombre(bilan.deja, 'sortie déjà présente', 'sorties déjà présentes')} ` +
                        `(ignorée${bilan.deja > 1 ? 's' : ''}).`
                    );
                }

                if (bilan.refusees.length > 0) {
                    lignes.push(
                        `⚠️ ${nombre(bilan.refusees.length, 'sortie refusée', 'sorties refusées')} :`
                    );
                    bilan.refusees.forEach(texte => lignes.push(`• ${texte}`));
                }

                if (bilan.interrompu) {
                    lignes.push(
                        `⛔ Import interrompu (${bilan.interrompu}) après ` +
                        `${bilan.traitees}/${elements.length} ${mot}. ` +
                        `Vous pouvez relancer : les sorties déjà importées seront ignorées.`
                    );
                }

                if (lignes.length === 0) {
                    lignes.push('Aucune sortie importée.');
                }

                afficherLignes(message, lignes);

                // En cas d'interruption, la sélection est gardée pour relancer.
                if (!bilan.interrompu && terminer) {
                    terminer();
                }

                await chargerEtat();
            }


            async function changerTerrain(id, terrain, menu, ancien) {
                menu.disabled = true;

                try {
                    const res = await fetch(`/api/sorties/${id}/terrain`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ terrain: terrain })
                    });

                    if (res.status === 401) {
                        menu.value = ancien;
                        menu.disabled = false;
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    // Recharge tout : l'usure des pièces change avec le terrain.
                    await chargerEtat();

                } catch (e) {
                    menu.value = ancien;
                    menu.disabled = false;
                    alert("Changement de terrain impossible.");
                }
            }


            async function ajouterPhotos(id, dejaLa, champ, bouton) {
                const fichiers = Array.from(champ.files || []);
                champ.value = '';

                if (fichiers.length === 0) {
                    return;
                }

                if (dejaLa + fichiers.length > MAX_PHOTOS) {
                    alert(
                        `${MAX_PHOTOS} photos maximum par sortie ` +
                        `(il y en a déjà ${dejaLa}).`
                    );
                    return;
                }

                const texteBouton = bouton.textContent;
                bouton.disabled = true;

                try {
                    const photos = [];

                    for (let i = 0; i < fichiers.length; i++) {
                        bouton.textContent =
                            `Préparation ${i + 1}/${fichiers.length}...`;
                        photos.push(await preparerPhoto(fichiers[i]));
                    }

                    bouton.textContent = 'Envoi...';

                    const res = await fetch(`/api/sorties/${id}/photos`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ photos: photos })
                    });

                    if (res.status === 401) {
                        bouton.textContent = texteBouton;
                        bouton.disabled = false;
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    await chargerEtat();

                } catch (e) {
                    bouton.textContent = texteBouton;
                    bouton.disabled = false;
                    alert("Ajout des photos impossible (photo illisible ou trop lourde ?).");
                }
            }


            // ============================================================
            // Import de l'archive Strava (fichier .zip lu dans le navigateur)
            // ============================================================

            const MAX_SORTIES_STRAVA = 1000;
            const TAILLE_MAX_ACTIVITE = 8 * 1024 * 1024;
            const TAILLE_MAX_CSV = 80 * 1024 * 1024;
            let archiveStrava = null;


            async function lireOctets(fichier, debut, fin) {
                return new Uint8Array(await fichier.slice(debut, fin).arrayBuffer());
            }


            async function decompresser(source, format) {
                const blob = source instanceof Blob ? source : new Blob([source]);
                const flux = blob.stream().pipeThrough(new DecompressionStream(format));
                return new Uint8Array(await new Response(flux).arrayBuffer());
            }


            // Lit la liste des fichiers d'un zip sans le charger en entier.
            async function lireEntreesZip(fichier) {
                const tailleQueue = Math.min(fichier.size, 65535 + 22 + 20);
                const queue = await lireOctets(
                    fichier, fichier.size - tailleQueue, fichier.size
                );
                const vq = new DataView(queue.buffer);
                let fin = -1;

                for (let i = queue.length - 22; i >= 0; i--) {
                    if (vq.getUint32(i, true) === 0x06054b50) {
                        fin = i;
                        break;
                    }
                }

                if (fin < 0) {
                    throw new Error('zip');
                }

                let total = vq.getUint16(fin + 10, true);
                let tailleCd = vq.getUint32(fin + 12, true);
                let debutCd = vq.getUint32(fin + 16, true);

                if (total === 0xFFFF || tailleCd === 0xFFFFFFFF || debutCd === 0xFFFFFFFF) {
                    const loc = fin - 20;

                    if (loc < 0 || vq.getUint32(loc, true) !== 0x07064b50) {
                        throw new Error('zip');
                    }

                    const decalage64 = Number(vq.getBigUint64(loc + 8, true));
                    const e64 = await lireOctets(fichier, decalage64, decalage64 + 56);
                    const v64 = new DataView(e64.buffer);

                    if (v64.getUint32(0, true) !== 0x06064b50) {
                        throw new Error('zip');
                    }

                    total = Number(v64.getBigUint64(32, true));
                    tailleCd = Number(v64.getBigUint64(40, true));
                    debutCd = Number(v64.getBigUint64(48, true));
                }

                const cd = await lireOctets(fichier, debutCd, debutCd + tailleCd);
                const vc = new DataView(cd.buffer);
                const decodeur = new TextDecoder('utf-8');
                const entrees = new Map();
                let p = 0;

                while (p + 46 <= cd.length && vc.getUint32(p, true) === 0x02014b50) {
                    const drapeaux = vc.getUint16(p + 8, true);
                    const methode = vc.getUint16(p + 10, true);
                    let tailleComp = vc.getUint32(p + 20, true);
                    let tailleReelle = vc.getUint32(p + 24, true);
                    const lNom = vc.getUint16(p + 28, true);
                    const lExtra = vc.getUint16(p + 30, true);
                    const lCom = vc.getUint16(p + 32, true);
                    let decalage = vc.getUint32(p + 42, true);
                    const nom = decodeur.decode(cd.subarray(p + 46, p + 46 + lNom));

                    let q = p + 46 + lNom;
                    const finExtra = q + lExtra;

                    while (q + 4 <= finExtra) {
                        const id = vc.getUint16(q, true);
                        const longueur = vc.getUint16(q + 2, true);

                        if (id === 1) {
                            let r = q + 4;

                            if (tailleReelle === 0xFFFFFFFF) {
                                tailleReelle = Number(vc.getBigUint64(r, true));
                                r += 8;
                            }

                            if (tailleComp === 0xFFFFFFFF) {
                                tailleComp = Number(vc.getBigUint64(r, true));
                                r += 8;
                            }

                            if (decalage === 0xFFFFFFFF) {
                                decalage = Number(vc.getBigUint64(r, true));
                            }
                        }

                        q += 4 + longueur;
                    }

                    entrees.set(nom, {
                        nom: nom,
                        methode: methode,
                        tailleComp: tailleComp,
                        tailleReelle: tailleReelle,
                        decalage: decalage,
                        chiffre: (drapeaux & 1) === 1
                    });

                    p += 46 + lNom + lExtra + lCom;
                }

                return entrees;
            }


            async function lireEntreeZip(fichier, entree, maximum) {
                if (entree.chiffre || entree.tailleReelle > maximum) {
                    throw new Error('entree');
                }

                const entete = await lireOctets(
                    fichier, entree.decalage, entree.decalage + 30
                );
                const ve = new DataView(entete.buffer);

                if (ve.getUint32(0, true) !== 0x04034b50) {
                    throw new Error('zip');
                }

                const debut =
                    entree.decalage + 30 +
                    ve.getUint16(26, true) + ve.getUint16(28, true);
                const blob = fichier.slice(debut, debut + entree.tailleComp);
                let octets;

                if (entree.methode === 0) {
                    octets = new Uint8Array(await blob.arrayBuffer());
                } else if (entree.methode === 8) {
                    octets = await decompresser(blob, 'deflate-raw');
                } else {
                    throw new Error('methode');
                }

                if (octets.length > maximum) {
                    throw new Error('entree');
                }

                return octets;
            }


            // Lit un fichier CSV (champs entre guillemets, retours à la ligne inclus).
            function lireCsv(texte) {
                const NL = String.fromCharCode(10);
                const RC = String.fromCharCode(13);

                if (texte.charCodeAt(0) === 0xFEFF) {
                    texte = texte.slice(1);
                }

                const premiere = texte.slice(0, 2000).split(NL)[0];
                const separateur =
                    premiere.split(';').length > premiere.split(',').length ? ';' : ',';

                const lignes = [];
                let ligne = [];
                let champ = '';
                let guillemets = false;

                for (let i = 0; i < texte.length; i++) {
                    const c = texte[i];

                    if (guillemets) {
                        if (c === '"') {
                            if (texte[i + 1] === '"') {
                                champ += '"';
                                i++;
                            } else {
                                guillemets = false;
                            }
                        } else {
                            champ += c;
                        }
                    } else if (c === '"') {
                        guillemets = true;
                    } else if (c === separateur) {
                        ligne.push(champ);
                        champ = '';
                    } else if (c === NL || c === RC) {
                        if (c === RC && texte[i + 1] === NL) {
                            i++;
                        }

                        ligne.push(champ);
                        champ = '';

                        if (ligne.length > 1 || ligne[0] !== '') {
                            lignes.push(ligne);
                        }

                        ligne = [];
                    } else {
                        champ += c;
                    }
                }

                if (champ !== '' || ligne.length > 0) {
                    ligne.push(champ);
                    lignes.push(ligne);
                }

                return lignes;
            }


            function ressembleActivite(valeur) {
                const v = valeur.trim().toLowerCase();

                return ['.gpx', '.fit', '.tcx', '.gpx.gz', '.fit.gz', '.tcx.gz']
                    .some(fin => v.endsWith(fin));
            }


            function trouverEntree(entrees, chemin) {
                if (entrees.has(chemin)) {
                    return entrees.get(chemin);
                }

                const fin = '/' + chemin;

                for (const [nom, entree] of entrees) {
                    if (nom.endsWith(fin)) {
                        return entree;
                    }
                }

                return null;
            }


            // Repère les colonnes (même si l'export est dans une autre langue)
            // et rend la liste des activités dont le fichier est dans l'archive.
            function lireActivitesStrava(lignes, entrees) {
                if (lignes.length < 2) {
                    return [];
                }

                const entete = lignes[0].map(t => t.toLowerCase());
                const rangees = lignes.slice(1);
                let colFichier = -1;
                let meilleur = 0;

                for (let j = 0; j < entete.length; j++) {
                    let n = 0;

                    for (const r of rangees) {
                        if (ressembleActivite(r[j] || '')) {
                            n++;
                        }
                    }

                    if (n > meilleur) {
                        meilleur = n;
                        colFichier = j;
                    }
                }

                if (colFichier < 0) {
                    return [];
                }

                const trouverCol = (mots, exclusions, defaut) => {
                    for (let j = 0; j < entete.length; j++) {
                        if (
                            mots.some(m => entete[j].includes(m)) &&
                            !exclusions.some(m => entete[j].includes(m))
                        ) {
                            return j;
                        }
                    }

                    return defaut;
                };

                const colType = trouverCol(['type'], [], 3);
                const colVelo = trouverCol(
                    ['gear', 'équipement', 'equipement', 'ausrüstung', 'equipo',
                     'attrezzatura', 'matériel', 'materiel'], [], 11
                );
                const colNom = trouverCol(
                    ['name', 'nom'], ['file', 'fichier', 'dateiname'], 2
                );

                const activites = [];

                for (const r of rangees) {
                    const chemin = (r[colFichier] || '').trim();

                    if (!ressembleActivite(chemin)) {
                        continue;
                    }

                    const entree = trouverEntree(entrees, chemin);

                    if (!entree) {
                        continue;
                    }

                    activites.push({
                        nom: (r[colNom] || '').trim(),
                        type: (r[colType] || '').trim(),
                        velo: (r[colVelo] || '').trim(),
                        chemin: chemin,
                        entree: entree
                    });
                }

                return activites;
            }


            const SANS_VELO = '__sans_velo__';
            const TOUT = '__tout__';


            function remplirChoix(select, compteurs, libelleTout, libelleVide, preferee) {
                select.replaceChildren();

                const ajouter = (valeur, texte) => {
                    const option = document.createElement('option');
                    option.value = valeur;
                    option.textContent = texte;
                    select.appendChild(option);
                };

                const noms = Array.from(compteurs.keys())
                    .filter(nom => nom !== '')
                    .sort((a, b) => compteurs.get(b) - compteurs.get(a));

                noms.forEach(nom => ajouter(nom, `${nom} (${compteurs.get(nom)})`));

                if (compteurs.has('')) {
                    ajouter(SANS_VELO, `${libelleVide} (${compteurs.get('')})`);
                }

                ajouter(TOUT, libelleTout);

                const voulue = preferee ? noms.find(preferee) : null;
                select.value = voulue || (noms.length > 0 ? noms[0] : TOUT);
            }


            function compter(activites, champ) {
                const compteurs = new Map();

                activites.forEach(a => {
                    compteurs.set(a[champ], (compteurs.get(a[champ]) || 0) + 1);
                });

                return compteurs;
            }


            function selectionStrava() {
                if (!archiveStrava) {
                    return [];
                }

                const velo = document.getElementById('stravaVelo').value;
                const type = document.getElementById('stravaType').value;

                return archiveStrava.activites.filter(a => {
                    const veloOk =
                        velo === TOUT ||
                        (velo === SANS_VELO ? a.velo === '' : a.velo === velo);
                    const typeOk =
                        type === TOUT ||
                        (type === SANS_VELO ? a.type === '' : a.type === type);

                    return veloOk && typeOk;
                });
            }


            function majCompteStrava() {
                const choisies = selectionStrava();
                const total = archiveStrava ? archiveStrava.activites.length : 0;
                const bouton = document.getElementById('stravaBouton');
                let texte =
                    `${nombre(choisies.length, 'sortie sera importée', 'sorties seront importées')} ` +
                    `(sur ${total} dans l'archive).`;

                if (choisies.length > MAX_SORTIES_STRAVA) {
                    texte +=
                        ` Seules les ${MAX_SORTIES_STRAVA} dernières seront importées ; ` +
                        `relancez ensuite pour les suivantes.`;
                }

                document.getElementById('stravaCompte').textContent = texte;
                bouton.disabled = choisies.length === 0;
            }


            async function chargerArchiveStrava() {
                const message = document.getElementById('stravaMessage');
                const choix = document.getElementById('stravaChoix');
                const fichier = document.getElementById('stravaZip').files[0];

                archiveStrava = null;
                choix.style.display = 'none';
                afficherLignes(message, []);

                if (!fichier) {
                    return;
                }

                if (typeof DecompressionStream === 'undefined') {
                    afficherLignes(message, [
                        "Ce navigateur est trop ancien pour lire l'archive. " +
                        "Utilisez une version récente de Chrome, Safari ou Firefox."
                    ]);
                    return;
                }

                afficherLignes(message, ["Lecture de l'archive..."]);

                try {
                    const entrees = await lireEntreesZip(fichier);
                    let csv = null;

                    for (const [nom, entree] of entrees) {
                        if (nom.toLowerCase().endsWith('activities.csv')) {
                            csv = entree;
                            break;
                        }
                    }

                    if (!csv) {
                        afficherLignes(message, [
                            "Ce fichier ne ressemble pas à une archive Strava " +
                            "(activities.csv introuvable)."
                        ]);
                        return;
                    }

                    const octets = await lireEntreeZip(fichier, csv, TAILLE_MAX_CSV);
                    const lignes = lireCsv(new TextDecoder('utf-8').decode(octets));
                    const activites = lireActivitesStrava(lignes, entrees);

                    if (activites.length === 0) {
                        afficherLignes(message, [
                            "Aucune sortie avec fichier GPS trouvée dans cette archive."
                        ]);
                        return;
                    }

                    archiveStrava = { fichier: fichier, activites: activites };

                    remplirChoix(
                        document.getElementById('stravaVelo'),
                        compter(activites, 'velo'),
                        'Tous les vélos et sorties',
                        'Sans vélo associé',
                        nom => nom.toLowerCase().includes('cube')
                    );

                    remplirChoix(
                        document.getElementById('stravaType'),
                        compter(activites, 'type'),
                        "Tous les types d'activité",
                        'Sans type',
                        null
                    );

                    // Par défaut : tous les types (le vélo choisi suffit à filtrer).
                    document.getElementById('stravaType').value = TOUT;

                    majCompteStrava();
                    afficherLignes(message, []);
                    choix.style.display = 'block';

                } catch (e) {
                    afficherLignes(message, [
                        "Impossible de lire ce fichier : est-ce bien l'archive .zip de Strava ?"
                    ]);
                }
            }


            function versBase64(octets) {
                let texte = '';

                for (let i = 0; i < octets.length; i += 0x8000) {
                    texte += String.fromCharCode.apply(
                        null, octets.subarray(i, i + 0x8000)
                    );
                }

                return btoa(texte);
            }


            async function chargerActiviteStrava(activite) {
                let octets = await lireEntreeZip(
                    archiveStrava.fichier, activite.entree, TAILLE_MAX_ACTIVITE
                );
                let nom = activite.chemin.toLowerCase();

                if (octets.length > 2 && octets[0] === 0x1f && octets[1] === 0x8b) {
                    octets = await decompresser(octets, 'gzip');
                }

                if (nom.endsWith('.gz')) {
                    nom = nom.slice(0, -3);
                }

                const charge = { nom: activite.nom ? activite.nom.slice(0, 120) : null };

                if (nom.endsWith('.fit')) {
                    charge.fit = versBase64(octets);
                } else {
                    charge.gpx = new TextDecoder('utf-8').decode(octets);
                }

                return charge;
            }


            async function importerStrava() {
                const message = document.getElementById('stravaMessage');
                const bouton = document.getElementById('stravaBouton');
                let choisies = selectionStrava();

                if (choisies.length === 0) {
                    afficherLignes(message, ["Aucune sortie à importer avec ces choix."]);
                    return;
                }

                if (choisies.length > MAX_SORTIES_STRAVA) {
                    choisies = choisies.slice(choisies.length - MAX_SORTIES_STRAVA);
                }

                const elements = choisies.map(activite => ({
                    nom: activite.nom || activite.chemin,
                    charger: () => chargerActiviteStrava(activite)
                }));

                bouton.disabled = true;
                document.getElementById('stravaZip').disabled = true;

                try {
                    await importerLot(
                        elements,
                        message,
                        {
                            terrain: document.getElementById('stravaTerrain').value,
                            notes: null
                        },
                        'sorties',
                        () => {
                            document.getElementById('stravaZip').value = '';
                            archiveStrava = null;
                            document.getElementById('stravaChoix').style.display = 'none';
                        }
                    );
                } finally {
                    document.getElementById('stravaZip').disabled = false;
                    bouton.disabled = archiveStrava === null;
                }
            }


            document.getElementById('stravaZip')
                .addEventListener('change', chargerArchiveStrava);
            document.getElementById('stravaVelo')
                .addEventListener('change', majCompteStrava);
            document.getElementById('stravaType')
                .addEventListener('change', majCompteStrava);


            async function supprimerSortie(id) {
                if (!confirm("Supprimer cette sortie ?")) {
                    return;
                }

                try {
                    const res = await fetch(`/api/sorties/${id}/supprimer`, {
                        method: 'POST',
                        headers: getHeaders()
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) throw new Error();
                    await chargerEtat();

                } catch (e) {
                    alert("Suppression impossible.");
                }
            }


            const CHAMPS_REGLAGES = {
                terrain_route: 'regRoute',
                terrain_mixte: 'regMixte',
                terrain_chemin: 'regChemin',
                terrain_sentier: 'regSentier',
                poids_denivele: 'regDenivele'
            };


            async function chargerReglages() {
                try {
                    const res = await fetch('/api/reglages', { headers: getHeaders() });
                    if (!res.ok) return;

                    const reglages = await res.json();

                    Object.entries(CHAMPS_REGLAGES).forEach(([cle, id]) => {
                        const champ = document.getElementById(id);
                        if (champ && reglages[cle] !== undefined) {
                            champ.value = reglages[cle];
                        }
                    });

                } catch (e) {
                    console.error(e);
                }
            }


            async function enregistrerReglages() {
                const message = document.getElementById('reglagesMessage');
                const corps = {};

                for (const [cle, id] of Object.entries(CHAMPS_REGLAGES)) {
                    const valeur = parseFloat(
                        document.getElementById(id).value.replace(',', '.')
                    );

                    if (isNaN(valeur)) {
                        message.textContent = "Vérifiez les nombres saisis.";
                        return;
                    }

                    corps[cle] = valeur;
                }

                try {
                    const res = await fetch('/api/reglages', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify(corps)
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) throw new Error();

                    message.textContent = '✅ Réglages enregistrés.';
                    await chargerEtat();

                } catch (e) {
                    message.textContent = "Enregistrement impossible.";
                }
            }
        </script>
    </body>
    </html>
    """

    return html_content


# ============================================================
# API : ÉTAT DU VÉLO ET POURCENTAGES D'USURE
# ============================================================

@app.get("/api/etat")
def obtenir_etat(x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT km_total FROM velo WHERE id = 1")
        velo = cursor.fetchone()

        km_global = (
            velo["km_total"]
            if velo and velo["km_total"] is not None
            else 2700
        )

        cursor.execute("SELECT date_achat FROM velo WHERE id = 1")
        ligne_achat = cursor.fetchone()
        date_achat = ligne_achat[0] if ligne_achat and ligne_achat[0] else DATE_ACHAT_PAR_DEFAUT

        cursor.execute("SELECT * FROM composants ORDER BY id")
        rows = cursor.fetchall()

        reglages = lire_reglages(cursor)
        aujourdhui = date.today()

        cursor.execute("""
            SELECT debut, distance_km, denivele_pos, denivele_neg, terrain
            FROM sorties
        """)
        sorties = cursor.fetchall()

        cursor.execute("""
            SELECT composant_id, MAX(date_remplacement)
            FROM historique
            GROUP BY composant_id
        """)
        dernier_entretien = {r[0]: r[1] for r in cursor.fetchall()}

        resultat = []

        for row in rows:
            km_depart = row["km_depart"] or 0
            seuil = row["seuil_alerte"] or 0

            km = max(0, km_global - km_depart)

            famille = famille_de(row["nom"])
            depuis = dernier_entretien.get(row["id"])
            surplus = 0.0

            for s in sorties:
                if depuis is None or s[0][:10] >= depuis:
                    facteur = facteur_usure(
                        famille, s[1], s[2], s[3], s[4] or "mixte", reglages
                    )
                    surplus += float(s[1] or 0) * (facteur - 1)

            km_usure = max(0.0, km + surplus)

            pourcentage_km = (
                round((km_usure / seuil) * 100)
                if seuil > 0
                else 0
            )

            seuil_jours = row["seuil_jours"] or 0
            jours_ecoules = None
            pourcentage_jours = 0

            if seuil_jours > 0:
                try:
                    reference = date.fromisoformat((depuis or date_achat)[:10])
                except ValueError:
                    reference = date.fromisoformat(DATE_ACHAT_PAR_DEFAUT)

                jours_ecoules = max(0, (aujourdhui - reference).days)
                pourcentage_jours = round(jours_ecoules / seuil_jours * 100)

            pourcentage = max(pourcentage_km, pourcentage_jours)

            est_verification = bool(row["verification"])
            numero = None

            if not est_verification:
                numero = sum(1 for r in resultat if not r["verification"]) + 1

            resultat.append({
                "id": row["id"],
                "numero": numero,
                "verification": est_verification,
                "nom": row["nom"],
                "km_actuel": km,
                "km_usure": round(km_usure, 1),
                "km_depart": km_depart,
                "seuil_alerte": seuil,
                "pourcentage_usure": pourcentage,
                "seuil_jours": seuil_jours,
                "jours_ecoules": jours_ecoules,
                "alerte_entretien": pourcentage >= 100,
                "categorie": row["categorie"],
                "pos_x": row["pos_x"],
                "pos_y": row["pos_y"]
            })

        # Liste des priorités : la pièce la plus usée en haut.
        # Le numéro (repère sur le schéma du vélo) reste fixe.
        resultat.sort(key=lambda r: (-r["pourcentage_usure"], r["id"]))

        return {
            "km_total": km_global,
            "composants": resultat
        }

    finally:
        conn.close()


# ============================================================
# API : HISTORIQUE DES INTERVENTIONS
# ============================================================

@app.get("/api/historique")
def obtenir_historique(x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT
                h.id,
                h.composant_id,
                c.nom AS composant_nom,
                h.date_remplacement,
                h.cout,
                h.notes,
                h.km_intervention
            FROM historique h
            JOIN composants c ON h.composant_id = c.id
            ORDER BY h.id DESC
        """)

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    finally:
        conn.close()


@app.post("/api/historique")
def ajouter_historique(data: HistoriqueRequest, x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM composants WHERE id = ?",
            (data.composant_id,)
        )

        if cursor.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail="Composant introuvable."
            )

        cursor.execute("SELECT km_total FROM velo WHERE id = 1")
        velo = cursor.fetchone()

        km_total = (
            velo[0]
            if velo and velo[0] is not None
            else 2700
        )

        if data.km_intervention > km_total:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Le kilométrage de l'intervention ne peut pas "
                    "dépasser celui du vélo."
                )
            )

        cursor.execute(
            "SELECT km_depart FROM composants WHERE id = ?",
            (data.composant_id,)
        )
        km_avant = cursor.fetchone()[0] or 0

        cursor.execute("""
            INSERT INTO historique (
                composant_id,
                date_remplacement,
                cout,
                notes,
                km_intervention,
                km_avant
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            data.composant_id,
            data.date_remplacement,
            data.cout or 0.0,
            data.notes or "Ancien entretien",
            data.km_intervention,
            km_avant
        ))

        cursor.execute("""
            UPDATE composants
            SET km_depart = ?
            WHERE id = ?
        """, (
            data.km_intervention,
            data.composant_id
        ))

        conn.commit()

        return {
            "statut": "success",
            "message": "Intervention enregistrée.",
            "km_intervention": data.km_intervention
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/historique/{historique_id}/supprimer")
def supprimer_intervention(
    historique_id: int,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT composant_id, km_avant FROM historique WHERE id = ?",
            (historique_id,)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(
                status_code=404,
                detail="Intervention introuvable."
            )

        composant_id = ligne[0]
        km_avant = ligne[1]

        cursor.execute(
            "DELETE FROM historique WHERE id = ?",
            (historique_id,)
        )

        cursor.execute("SELECT km_depart FROM composants WHERE id = ?", (composant_id,))
        actuel = cursor.fetchone()
        km_depart = (actuel[0] or 0) if actuel else 0

        if composant_id is not None:
            cursor.execute("""
                SELECT id
                FROM historique
                WHERE composant_id = ? AND id > ?
                ORDER BY id
                LIMIT 1
            """, (composant_id, historique_id))
            suivante = cursor.fetchone()

            if suivante is None:
                # C'était la dernière intervention saisie : la pièce
                # revient exactement à l'état d'avant cette intervention.
                if km_avant is not None:
                    km_depart = km_avant
                else:
                    # Ligne ancienne, sans mémoire de l'état d'avant :
                    # on reprend l'intervention précédente, ou 0.
                    cursor.execute("""
                        SELECT km_intervention
                        FROM historique
                        WHERE composant_id = ? AND km_intervention IS NOT NULL
                        ORDER BY id DESC
                        LIMIT 1
                    """, (composant_id,))
                    derniere = cursor.fetchone()
                    km_depart = derniere[0] if derniere else 0

                cursor.execute(
                    "UPDATE composants SET km_depart = ? WHERE id = ?",
                    (km_depart, composant_id)
                )
            else:
                # Une intervention plus récente existe : l'état actuel de
                # la pièce ne change pas. On "recolle" la chaîne pour que
                # supprimer la suivante revienne au bon état d'avant.
                cursor.execute(
                    "UPDATE historique SET km_avant = ? WHERE id = ?",
                    (km_avant, suivante[0])
                )

        conn.commit()

        return {
            "statut": "success",
            "composant_id": composant_id,
            "km_depart": km_depart
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sync")
def synchroniser_kilometrage(data: SynchroRequest, x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            UPDATE velo
            SET km_total = ?, km_frac = 0, km_maj_le = ?
            WHERE id = 1
        """, (data.km_total, maintenant_utc()))

        conn.commit()

        return {
            "statut": "success",
            "km_total": data.km_total
        }

    finally:
        conn.close()


@app.post("/api/composants/{composant_id}/remplacer")
def remplacer_composant(
    composant_id: int,
    data: RemplacementRequest,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM composants WHERE id = ?",
            (composant_id,)
        )

        if cursor.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail="Composant introuvable."
            )

        cursor.execute("SELECT km_total FROM velo WHERE id = 1")
        velo = cursor.fetchone()

        km_global = (
            velo[0]
            if velo and velo[0] is not None
            else 2700
        )

        date_resp = (
            data.date_remplacement
            if data.date_remplacement
            else str(date.today())
        )

        notes_resp = (
            data.notes
            if data.notes
            else "Intervention standard"
        )

        cout_resp = (
            data.cout
            if data.cout is not None
            else 0.0
        )

        cursor.execute(
            "SELECT km_depart FROM composants WHERE id = ?",
            (composant_id,)
        )
        km_avant = cursor.fetchone()[0] or 0

        cursor.execute("""
            UPDATE composants
            SET km_depart = ?
            WHERE id = ?
        """, (
            km_global,
            composant_id
        ))

        cursor.execute("""
            INSERT INTO historique (
                composant_id,
                date_remplacement,
                cout,
                notes,
                km_intervention,
                km_avant
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            composant_id,
            date_resp,
            cout_resp,
            notes_resp,
            km_global,
            km_avant
        ))

        conn.commit()

        return {
            "statut": "success",
            "message": "Intervention enregistrée.",
            "km_intervention": km_global
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


TERRAINS = {"mixte", "route", "chemin", "sentier"}


def ajuster_km_total(cursor, delta_km):
    cursor.execute("SELECT km_total, km_frac FROM velo WHERE id = 1")
    ligne = cursor.fetchone()

    base = ligne[0] if ligne and ligne[0] is not None else 2700
    frac = ligne[1] if ligne and ligne[1] is not None else 0.0

    total = max(0.0, base + frac + delta_km)
    entier = int(math.floor(total + 1e-9))

    cursor.execute(
        "UPDATE velo SET km_total = ?, km_frac = ? WHERE id = 1",
        (entier, total - entier)
    )

    return entier


@app.get("/api/mes-reglages")
def lire_mes_reglages(x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT cle, valeur, maj_le FROM mes_reglages")
        valeurs = {}
        maj = None

        for ligne in cursor.fetchall():
            valeurs[ligne[0]] = ligne[1]

            if ligne[2] and (maj is None or ligne[2] > maj):
                maj = ligne[2]

        return {"valeurs": valeurs, "maj_le": maj}

    finally:
        conn.close()


@app.post("/api/mes-reglages")
def enregistrer_mes_reglages(
    data: MesReglagesRequest,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)

    for cle, valeur in data.valeurs.items():
        if cle not in MES_REGLAGES_CLES:
            raise HTTPException(status_code=400, detail="Réglage inconnu : " + cle)

        if len(valeur.strip()) > 200:
            raise HTTPException(status_code=400, detail="Valeur trop longue.")

    aujourdhui = date.today().isoformat()
    conn = db.connect()
    cursor = conn.cursor()

    try:
        for cle, valeur in data.valeurs.items():
            valeur = valeur.strip()

            if valeur == "":
                cursor.execute("DELETE FROM mes_reglages WHERE cle = ?", (cle,))
                continue

            cursor.execute("""
                INSERT INTO mes_reglages (cle, valeur, maj_le)
                VALUES (?, ?, ?)
                ON CONFLICT (cle) DO UPDATE
                SET valeur = excluded.valeur, maj_le = excluded.maj_le
            """, (cle, valeur, aujourdhui))

        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/composants/{composant_id}/seuils")
def modifier_seuils(
    composant_id: int,
    data: SeuilsRequest,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)

    if data.seuil_alerte == 0 and data.seuil_jours == 0:
        raise HTTPException(
            status_code=400,
            detail="Indiquez au moins un seuil (en km ou en jours)."
        )

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id FROM composants WHERE id = ?", (composant_id,))

        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Composant introuvable.")

        cursor.execute(
            "UPDATE composants SET seuil_alerte = ?, seuil_jours = ? WHERE id = ?",
            (data.seuil_alerte, data.seuil_jours, composant_id)
        )
        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/composants/{composant_id}/position")
def deplacer_repere(
    composant_id: int,
    data: PositionRequest,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id FROM composants WHERE id = ?", (composant_id,))

        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Composant introuvable.")

        cursor.execute(
            "UPDATE composants SET pos_x = ?, pos_y = ? WHERE id = ?",
            (data.pos_x, data.pos_y, composant_id)
        )
        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/composants/{composant_id}/supprimer")
def retirer_composant(
    composant_id: int,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Retire une pièce de la liste, avec son historique."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id FROM composants WHERE id = ?", (composant_id,))

        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Composant introuvable.")

        cursor.execute(
            "DELETE FROM historique WHERE composant_id = ?",
            (composant_id,)
        )
        cursor.execute("DELETE FROM composants WHERE id = ?", (composant_id,))
        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/import")
def importer_sortie(data: ImportGpxRequest, x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)

    if not data.gpx and not data.fit:
        raise HTTPException(status_code=400, detail="Aucun fichier reçu.")

    try:
        if data.fit:
            try:
                octets = base64.b64decode(data.fit, validate=True)
            except ValueError:
                raise gpx_import.GpxError("Fichier FIT illisible.")

            s = gpx_import.analyser_activite(octets=octets)
        else:
            s = gpx_import.analyser_activite(texte=data.gpx)
    except gpx_import.GpxError as e:
        raise HTTPException(status_code=400, detail=str(e))

    terrain = (data.terrain or "mixte").lower()

    if terrain not in TERRAINS:
        terrain = "mixte"

    nom = (data.nom or s["nom"] or "Sortie à vélo")[:120]
    notes = (data.notes or "").strip()[:500] or None
    photos = data.photos or []

    if len(photos) > MAX_PHOTOS_PAR_SORTIE:
        raise HTTPException(
            status_code=400,
            detail=f"{MAX_PHOTOS_PAR_SORTIE} photos maximum par sortie."
        )

    photos_stockees = []

    for p in photos:
        verifier_photo(p.mini, MAX_CHARS_MINIATURE)
        verifier_photo(p.photo, MAX_CHARS_PHOTO)
        photos_stockees.append({"mini": p.mini, "photo": p.photo})

    photos_json = json.dumps(photos_stockees) if photos_stockees else None

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id FROM sorties WHERE debut = ?", (s["debut"],))

        if cursor.fetchone() is not None:
            raise HTTPException(
                status_code=409,
                detail="Cette sortie est déjà importée."
            )

        # Même sortie importée depuis une autre source (Komoot puis Strava) :
        # l'heure de départ peut différer de quelques secondes ou minutes.
        depart = datetime.strptime(s["debut"], "%Y-%m-%dT%H:%M:%SZ")
        format_heure = "%Y-%m-%dT%H:%M:%SZ"
        cursor.execute(
            "SELECT distance_km FROM sorties WHERE debut >= ? AND debut <= ?",
            (
                (depart - timedelta(minutes=10)).strftime(format_heure),
                (depart + timedelta(minutes=10)).strftime(format_heure),
            )
        )

        for proche in cursor.fetchall():
            ecart = abs((proche[0] or 0) - s["distance_km"])

            if ecart <= max(0.3, 0.1 * s["distance_km"]):
                raise HTTPException(
                    status_code=409,
                    detail="Cette sortie est déjà importée."
                )

        cursor.execute("SELECT km_maj_le FROM velo WHERE id = 1")
        velo = cursor.fetchone()
        derniere_maj = velo[0] if velo else None

        compte = 1 if (derniere_maj and s["debut"] > derniere_maj) else 0

        cursor.execute("""
            INSERT INTO sorties (
                nom, debut, sport, distance_km, denivele_pos, denivele_neg,
                duree_s, vitesse_moy, alt_max, pente_moy_montee,
                terrain, notes, km_compte, trace, photos
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            nom, s["debut"], s["sport"], s["distance_km"],
            s["denivele_pos"], s["denivele_neg"], s["duree_s"],
            s["vitesse_moy"], s["alt_max"], s["pente_moy_montee"],
            terrain, notes, compte, s["trace"], photos_json
        ))

        if compte:
            km_total = ajuster_km_total(cursor, s["distance_km"])
        else:
            cursor.execute("SELECT km_total FROM velo WHERE id = 1")
            km_total = cursor.fetchone()[0]

        conn.commit()

        return {
            "statut": "success",
            "nom": nom,
            "debut": s["debut"],
            "distance_km": s["distance_km"],
            "denivele_pos": s["denivele_pos"],
            "duree_s": s["duree_s"],
            "ajoute_au_total": bool(compte),
            "km_total": km_total
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.get("/api/sorties")
def lister_sorties(x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT * FROM sorties ORDER BY debut DESC")

        resultat = []

        for row in cursor.fetchall():
            sortie = dict(row)

            try:
                sortie["trace"] = json.loads(sortie["trace"] or "[]")
            except ValueError:
                sortie["trace"] = []

            try:
                brutes = json.loads(sortie["photos"] or "[]")
            except ValueError:
                brutes = []

            if not isinstance(brutes, list):
                brutes = []

            # La liste n'envoie que de petites miniatures :
            # la photo complète se charge au clic.
            sortie["photos"] = [miniature_de(p) for p in brutes]

            resultat.append(sortie)

        return resultat

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/supprimer")
def supprimer_sortie(sortie_id: int, x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT distance_km, km_compte FROM sorties WHERE id = ?",
            (sortie_id,)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(
                status_code=404,
                detail="Sortie introuvable."
            )

        if ligne[1]:
            ajuster_km_total(cursor, -float(ligne[0] or 0))

        cursor.execute("DELETE FROM sorties WHERE id = ?", (sortie_id,))
        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/terrain")
def changer_terrain(
    sortie_id: int,
    data: TerrainRequest,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    terrain = data.terrain.strip().lower()

    if terrain not in TERRAINS:
        raise HTTPException(status_code=400, detail="Terrain inconnu.")

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id FROM sorties WHERE id = ?", (sortie_id,))

        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable.")

        cursor.execute(
            "UPDATE sorties SET terrain = ? WHERE id = ?",
            (terrain, sortie_id)
        )
        conn.commit()

        return {"statut": "success", "terrain": terrain}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.get("/api/reglages")
def obtenir_reglages(x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        return lire_reglages(cursor)

    finally:
        conn.close()


@app.post("/api/reglages")
def enregistrer_reglages(data: ReglagesRequest, x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        for cle, valeur in data.model_dump().items():
            cursor.execute("""
                INSERT INTO reglages (cle, valeur)
                VALUES (?, ?)
                ON CONFLICT (cle) DO UPDATE SET valeur = excluded.valeur
            """, (cle, valeur))

        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()



@app.post("/api/sorties/{sortie_id}/photos")
def ajouter_photos(
    sortie_id: int,
    data: AjoutPhotosRequest,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Joint des photos à une sortie déjà importée (8 au total maximum)."""
    verifier_auth(x_code_acces)

    if not data.photos:
        raise HTTPException(status_code=400, detail="Aucune photo reçue.")

    nouvelles = []

    for p in data.photos:
        verifier_photo(p.mini, MAX_CHARS_MINIATURE)
        verifier_photo(p.photo, MAX_CHARS_PHOTO)
        nouvelles.append({"mini": p.mini, "photo": p.photo})

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT photos FROM sorties WHERE id = ?",
            (sortie_id,)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable.")

        try:
            existantes = json.loads(ligne[0] or "[]")
        except ValueError:
            existantes = []

        if not isinstance(existantes, list):
            existantes = []

        if len(existantes) + len(nouvelles) > MAX_PHOTOS_PAR_SORTIE:
            raise HTTPException(
                status_code=400,
                detail=f"{MAX_PHOTOS_PAR_SORTIE} photos maximum par sortie."
            )

        total = existantes + nouvelles

        cursor.execute(
            "UPDATE sorties SET photos = ? WHERE id = ?",
            (json.dumps(total), sortie_id)
        )
        conn.commit()

        return {"statut": "success", "nombre": len(total)}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/photos/{numero}/supprimer")
def retirer_photo(
    sortie_id: int,
    numero: int,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Retire une seule photo d'une sortie (les autres restent)."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT photos FROM sorties WHERE id = ?",
            (sortie_id,)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable.")

        try:
            photos = json.loads(ligne[0] or "[]")
        except ValueError:
            photos = []

        if not isinstance(photos, list) or not 0 <= numero < len(photos):
            raise HTTPException(status_code=404, detail="Photo introuvable.")

        del photos[numero]

        cursor.execute(
            "UPDATE sorties SET photos = ? WHERE id = ?",
            (json.dumps(photos) if photos else None, sortie_id)
        )
        conn.commit()

        return {"statut": "success", "nombre": len(photos)}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.get("/api/sorties/{sortie_id}/photos/{numero}")
def obtenir_photo(
    sortie_id: int,
    numero: int,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT photos FROM sorties WHERE id = ?",
            (sortie_id,)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable.")

        try:
            photos = json.loads(ligne[0] or "[]")
        except ValueError:
            photos = []

        if not isinstance(photos, list) or not 0 <= numero < len(photos):
            raise HTTPException(status_code=404, detail="Photo introuvable.")

        return {"image": photo_complete_de(photos[numero])}

    finally:
        conn.close()


@app.get("/api/sauvegarde")
def sauvegarde(x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT km_total FROM velo WHERE id = 1")
        velo = cursor.fetchone()

        cursor.execute("SELECT * FROM composants ORDER BY id")
        composants = [dict(r) for r in cursor.fetchall()]

        cursor.execute("SELECT * FROM historique ORDER BY id")
        historique = [dict(r) for r in cursor.fetchall()]

        cursor.execute("SELECT * FROM sorties ORDER BY debut")
        sorties = [dict(r) for r in cursor.fetchall()]

        return {
            "date_sauvegarde": str(date.today()),
            "km_total": velo[0] if velo else None,
            "composants": composants,
            "historique": historique,
            "sorties": sorties,
            "reglages": lire_reglages(cursor)
        }

    finally:
        conn.close()
