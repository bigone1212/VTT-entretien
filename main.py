from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import db
import gpx_import
import json
import math
from typing import Optional, List
from datetime import date, datetime, timezone

app = FastAPI(title="VTTAE Entretien")

# Code d'accès de l'application (modifiable ici)
CODE_ACCES = "Cube144"

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


def verifier_auth(x_code_acces: Optional[str] = Header(default=None)):
    """Vérifie le code d'accès envoyé par l'en-tête HTTP."""
    if x_code_acces != CODE_ACCES:
        raise HTTPException(status_code=401, detail="Code d'accès incorrect.")


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

    if famille == "transmission":
        montee = (d_pos or 0) / distance
        return coef_terrain * (1 + poids * montee / 100)

    if famille == "freins":
        descente = (d_neg or 0) / distance
        return coef_terrain * (1 + poids * descente / 100)

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
    photos: Optional[List[str]] = None


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
                box-shadow: 0 4px 20px rgba(0,0,0,0.5);
                border: 1px solid rgba(255,255,255,0.05);
                width: 100%;
                max-width: 400px;
                text-align: center;
            }

            .auth-card h2 {
                margin-top: 0;
                font-size: 1.2rem;
                color: #fff;
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
            input[type="password"],
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
        <div id="authScreen">
            <div class="auth-card">
                <h2>🔐 Accès protégé</h2>
                <p class="form-help">Entrez le code d'accès de l'application</p>
                <input type="password" id="authInput" placeholder="Code secret" style="margin-bottom: 12px;">
                <button class="btn-primary" style="width: 100%;" onclick="tenterConnexion()">Valider</button>
                <p id="authError" style="color: var(--danger); font-size: 0.8rem; margin-top: 10px;"></p>
            </div>
        </div>

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
                src="https://shop.roulezjeunesse.com/cdn/shop/files/CubeStereoHybridONE44EXC8001.jpg?v=1736521382&width=1220"
                alt="Mon Cube Stereo Hybrid"
                class="bike-img"
            >

            <div id="hotspotsContainer"></div>
        </div>

        <div id="composantsList" class="grid">
            <div class="loading">Chargement des composants...</div>
        </div>

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

        <script>
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
                        document.getElementById('authError').textContent = "Code incorrect.";
                    }
                } catch (e) {
                    document.getElementById('authError').textContent = "Erreur de connexion.";
                }
            }

            // Vérifie si un code est déjà en mémoire
            if (sessionStorage.getItem('vtt_code')) {
                fetch('/api/etat', { headers: getHeaders() }).then(res => {
                    if (res.ok) {
                        document.getElementById('authScreen').style.display = 'none';
                        chargerEtat();
                    }
                });
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
                        `Kilométrage total : ${data.km_total} km`;

                    const container =
                        document.getElementById('composantsList');

                    const hotspotsContainer =
                        document.getElementById('hotspotsContainer');

                    const selectComp =
                        document.getElementById('histComposant');

                    container.innerHTML = '';
                    hotspotsContainer.innerHTML = '';
                    selectComp.innerHTML = '';

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

                        const badgeText =
                            c.pourcentage_usure >= 100
                                ? `À faire · ${c.pourcentage_usure}%`
                                : `${c.pourcentage_usure}%`;

                        const btnLabel =
                            c.categorie === 'maintenance'
                                ? '🔧 Marquer fait'
                                : '⚙️ Remplacer';

                        const option = document.createElement('option');
                        option.value = c.id;
                        option.textContent = c.nom;
                        selectComp.appendChild(option);

                        const hotspot = document.createElement('div');
                        hotspot.className = `hotspot ${hotspotClass}`;
                        hotspot.style.top = `${c.pos_y}%`;
                        hotspot.style.left = `${c.pos_x}%`;
                        hotspot.title = `${c.nom} (${c.pourcentage_usure}%)`;
                        hotspot.textContent = index + 1;

                        hotspot.addEventListener('click', () => {
                            remplacer(c.id, c.nom);
                        });

                        hotspotsContainer.appendChild(hotspot);

                        const card = document.createElement('div');
                        card.className = 'card';
                        card.id = `card-${c.id}`;

                        const cardTop = document.createElement('div');
                        cardTop.className = 'card-top';

                        const componentName = document.createElement('div');
                        componentName.className = 'component-name';
                        componentName.textContent =
                            `📍 #${index + 1} - ${c.nom}`;

                        const badge = document.createElement('div');
                        badge.className = `badge ${statusClass}`;
                        badge.textContent = badgeText;

                        cardTop.appendChild(componentName);
                        cardTop.appendChild(badge);

                        const statsRow = document.createElement('div');
                        statsRow.className = 'stats-row';

                        const kmText = document.createElement('span');
                        kmText.textContent =
                            `Parcouru depuis l'intervention : ${c.km_actuel} km`;

                        const thresholdText = document.createElement('span');
                        thresholdText.textContent =
                            `Seuil : ${c.seuil_alerte} km`;

                        statsRow.appendChild(kmText);
                        statsRow.appendChild(thresholdText);

                        const barBg = document.createElement('div');
                        barBg.className = 'bar-bg';

                        const barFill = document.createElement('div');
                        barFill.className = `bar-fill ${statusClass}`;
                        barFill.style.width =
                            `${Math.min(c.pourcentage_usure, 100)}%`;

                        barBg.appendChild(barFill);

                        const button = document.createElement('button');
                        button.className = 'btn-action';
                        button.textContent = btnLabel;
                        button.addEventListener('click', () => {
                            remplacer(c.id, c.nom);
                        });

                        card.appendChild(cardTop);
                        card.appendChild(statsRow);

                        if (
                            c.km_usure !== undefined &&
                            Math.abs(c.km_usure - c.km_actuel) >= 1
                        ) {
                            const usureInfo = document.createElement('div');
                            usureInfo.className = 'form-help';
                            usureInfo.style.margin = '0 0 6px';
                            usureInfo.textContent =
                                `Usure estimée selon vos sorties : ${c.km_usure} km`;
                            card.appendChild(usureInfo);
                        }

                        card.appendChild(barBg);
                        card.appendChild(button);

                        container.appendChild(card);
                    });

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
                        ligne2.textContent =
                            (LIBELLES_TERRAIN[s.terrain] || s.terrain) +
                            (s.notes ? ` · ${s.notes}` : '');

                        const bouton = document.createElement('button');
                        bouton.className = 'btn-action';
                        bouton.style.marginTop = '8px';
                        bouton.style.width = 'auto';
                        bouton.style.padding = '6px 12px';
                        bouton.textContent = '🗑️ Supprimer';
                        bouton.addEventListener('click', () => {
                            supprimerSortie(s.id);
                        });

                        details.appendChild(titre);
                        details.appendChild(ligne1);
                        details.appendChild(ligne2);
                        details.appendChild(bouton);

                        item.appendChild(details);

                        const mediaDiv = document.createElement('div');
                        mediaDiv.className = 'sortie-media';

                        if (s.photos && s.photos.length > 0) {
                            const gallery = document.createElement('div');
                            gallery.className = 'photos-gallery';
                            s.photos.forEach(pData => {
                                const img = document.createElement('img');
                                img.src = pData;
                                img.className = 'sortie-img';
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


            async function importerGpx() {
                const fichierGpx = document.getElementById('gpxFile').files[0];
                const fichiersPhotos = document.getElementById('gpxPhotos').files;
                const message = document.getElementById('gpxMessage');

                if (!fichierGpx) {
                    message.textContent = "Choisissez d'abord un fichier GPX.";
                    return;
                }

                message.textContent = 'Import en cours...';

                try {
                    const gpx = await fichierGpx.text();
                    let photosList = [];

                    for (let i = 0; i < fichiersPhotos.length; i++) {
                        const base64 = await new Promise((resolve) => {
                            const reader = new FileReader();
                            reader.onload = (e) => resolve(e.target.result);
                            reader.readAsDataURL(fichiersPhotos[i]);
                        });
                        photosList.push(base64);
                    }

                    const res = await fetch('/api/sorties/import', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({
                            gpx: gpx,
                            terrain: document.getElementById('gpxTerrain').value,
                            notes: document.getElementById('gpxNotes').value.trim() || null,
                            photos: photosList
                        })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || "Fichier refusé.");

                    message.textContent =
                        `✅ Sortie importée : ${data.distance_km} km, ` +
                        `${data.denivele_pos} m D+. ` +
                        (data.ajoute_au_total
                            ? `Kilométrage total : ${data.km_total} km.`
                            : "Cette sortie date d'avant votre dernière mise à jour " +
                              "du kilométrage : elle n'a pas été ajoutée au total.");

                    document.getElementById('gpxFile').value = '';
                    document.getElementById('gpxPhotos').value = '';
                    document.getElementById('gpxNotes').value = '';

                    await chargerEtat();

                } catch (e) {
                    message.textContent = e.message || "Import impossible.";
                }
            }


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

        cursor.execute("SELECT * FROM composants")
        rows = cursor.fetchall()

        reglages = lire_reglages(cursor)

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

            pourcentage = (
                round((km_usure / seuil) * 100)
                if seuil > 0
                else 0
            )

            resultat.append({
                "id": row["id"],
                "nom": row["nom"],
                "km_actuel": km,
                "km_usure": round(km_usure, 1),
                "km_depart": km_depart,
                "seuil_alerte": seuil,
                "pourcentage_usure": pourcentage,
                "alerte_entretien": km_usure >= seuil if seuil > 0 else False,
                "categorie": row["categorie"],
                "pos_x": row["pos_x"],
                "pos_y": row["pos_y"]
            })

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

        cursor.execute("""
            INSERT INTO historique (
                composant_id,
                date_remplacement,
                cout,
                notes,
                km_intervention
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            data.composant_id,
            data.date_remplacement,
            data.cout or 0.0,
            data.notes or "Ancien entretien",
            data.km_intervention
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
                km_intervention
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            composant_id,
            date_resp,
            cout_resp,
            notes_resp,
            km_global
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


@app.post("/api/sorties/import")
def importer_sortie(data: ImportGpxRequest, x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    try:
        s = gpx_import.analyser_gpx(data.gpx)
    except gpx_import.GpxError as e:
        raise HTTPException(status_code=400, detail=str(e))

    terrain = (data.terrain or "mixte").lower()

    if terrain not in TERRAINS:
        terrain = "mixte"

    nom = (data.nom or s["nom"] or "Sortie à vélo")[:120]
    notes = (data.notes or "").strip()[:500] or None
    photos_json = json.dumps(data.photos) if data.photos else None

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id FROM sorties WHERE debut = ?", (s["debut"],))

        if cursor.fetchone() is not None:
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
                sortie["photos"] = json.loads(sortie["photos"] or "[]")
            except ValueError:
                sortie["photos"] = []

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