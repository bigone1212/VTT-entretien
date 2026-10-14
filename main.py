from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import db
import gpx_import
import json
import math
from typing import Optional
from datetime import date, datetime, timezone

app = FastAPI(title="VTTAE Entretien")

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

    if "plaquette" in nom or "frein" in nom:
        return "freins"

    if "fourche" in nom or "amortisseur" in nom:
        return "suspension"

    if "pneu" in nom:
        return "pneu"

    return "transmission"


def facteur_usure(famille, distance_km, d_pos, d_neg, terrain, reglages):
    """
    Combien de km d'usure équivaut 1 km de cette sortie pour cette famille.
    1,0 = usure normale ; 2,0 = deux fois plus vite.
    """
    coef_terrain = reglages.get(f"terrain_{terrain}", 1.0)
    distance = max(float(distance_km or 0), 0.1)
    poids = reglages.get("poids_denivele", 1.0)

    # Transmission : le dénivelé positif (montées) la fatigue.
    if famille == "transmission":
        montee = (d_pos or 0) / distance
        return coef_terrain * (1 + poids * montee / 100)

    # Freins : c'est le dénivelé négatif (descentes) qui compte.
    if famille == "freins":
        descente = (d_neg or 0) / distance
        return coef_terrain * (1 + poids * descente / 100)

    # Fourche, pneus : surtout le type de terrain.
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

    # Ajout automatique de la colonne photo si elle n'existe pas
    ajouter_colonne(cursor, "sorties", "photo", "TEXT")

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


class ImportGpxRequest(BaseModel):
    gpx: str = Field(min_length=50, max_length=3_000_000)
    terrain: Optional[str] = "mixte"
    notes: Optional[str] = None
    nom: Optional[str] = None
    photo: Optional[str] = None


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

        <title>Cube Stereo - Suivi Entretien</title>

        <style>
            :root {
                --bg-color: #12141c;
                --card-bg: #1e222d;
                --text-color: #f8f9fa;
                --text-muted: #94a3b8;
                --accent: #3b82f6;
                --accent-hover: #2563eb;
                --success: #10b981;
                --danger: #ef4444;
                --warning: #f59e0b;
            }

            * {
                box-sizing: border-box;
            }

            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                    Roboto, Helvetica, Arial, sans-serif;
                background-color: var(--bg-color);
                color: var(--text-color);
                margin: 0;
                padding: 16px;
                -webkit-font-smoothing: antialiased;
            }

            .header {
                text-align: center;
                margin-bottom: 20px;
            }

            .header h1 {
                font-size: 1.4rem;
                margin: 0 0 4px 0;
                color: #fff;
            }

            .header p {
                font-size: 0.85rem;
                color: var(--text-muted);
                margin: 0;
            }

            .sync-card {
                background: var(--card-bg);
                padding: 14px;
                border-radius: 14px;
                margin: 0 auto 20px auto;
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                border: 1px solid rgba(255,255,255,0.05);
                max-width: 600px;
            }

            .sync-card label {
                display: block;
                font-size: 0.75rem;
                color: var(--text-muted);
                margin-bottom: 6px;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            .sync-row {
                display: flex;
                gap: 10px;
            }

            input[type="number"],
            input[type="text"],
            input[type="date"],
            select {
                flex: 1;
                min-width: 0;
                background: #0f1117;
                border: 1px solid #2a303c;
                border-radius: 8px;
                padding: 10px;
                color: #fff;
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
                color: white;
                border: none;
                border-radius: 8px;
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
                border: 1px solid rgba(255,255,255,0.05);
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            }

            .bike-img {
                width: 100%;
                height: 100%;
                object-fit: contain;
                display: block;
            }

            .hotspot {
                position: absolute;
                width: 24px;
                height: 24px;
                background: var(--accent);
                border: 2px solid #fff;
                border-radius: 50%;
                transform: translate(-50%, -50%);
                cursor: pointer;
                box-shadow: 0 2px 6px rgba(0,0,0,0.4);
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 0.7rem;
                font-weight: bold;
                color: white;
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
                border-radius: 14px;
                padding: 16px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.2);
                border: 1px solid rgba(255,255,255,0.05);
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
                color: #fff;
            }

            .badge {
                flex-shrink: 0;
                font-size: 0.7rem;
                padding: 3px 8px;
                border-radius: 20px;
                font-weight: 600;
            }

            .badge.ok {
                background: rgba(16, 185, 129, 0.15);
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
                background: #0f1117;
                border-radius: 6px;
                height: 6px;
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
                background: rgba(255,255,255,0.05);
                color: var(--text-color);
                border: 1px solid rgba(255,255,255,0.1);
                width: 100%;
                padding: 8px;
                border-radius: 8px;
                font-size: 0.85rem;
                cursor: pointer;
            }

            button.btn-action:hover {
                background: rgba(255,255,255,0.1);
            }

            .history-section {
                max-width: 600px;
                margin: 0 auto;
                background: var(--card-bg);
                border-radius: 14px;
                padding: 16px;
                border: 1px solid rgba(255,255,255,0.05);
                box-shadow: 0 4px 12px rgba(0,0,0,0.2);
            }

            .history-section h2 {
                font-size: 1.1rem;
                margin-top: 0;
                color: #fff;
            }

            .history-item {
                font-size: 0.85rem;
                padding: 10px 0;
                border-bottom: 1px solid rgba(255,255,255,0.05);
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

            .sortie-img {
                width: 90px;
                height: 60px;
                object-fit: cover;
                border-radius: 6px;
                border: 1px solid rgba(255,255,255,0.1);
            }

            @media (max-width: 480px) {
                body {
                    padding: 10px;
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
        </style>
    </head>

    <body>
        <div class="header">
            <h1>🚲 Cube Stereo Hybrid</h1>
            <p id="veloInfo">Kilométrage total : chargement...</p>
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

        <div class="history-section" style="margin-bottom: 20px;">
            <h2>🚵 Sorties</h2>

            <label for="gpxFile">Importer une sortie (fichier GPX de Komoot)</label>
            <input
                type="file"
                id="gpxFile"
                accept=".gpx,application/gpx+xml,text/xml"
            >

            <label for="gpxPhoto">Photo souvenir (optionnel)</label>
            <input
                type="file"
                id="gpxPhoto"
                accept="image/*"
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
                style="width:100%;"
                onclick="importerGpx()"
            >
                Importer la sortie
            </button>

            <p class="form-help" id="gpxMessage" style="margin-top: 10px;"></p>

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

        <div class="bike-container">
            <img