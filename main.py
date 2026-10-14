from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import db
from typing import Optional
from datetime import date

app = FastAPI(title="VTTAE Entretien")

# ============================================================
# BASE DE DONNÉES
# ============================================================

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

    # Migration automatique pour les anciennes bases :
    # ajoute la colonne kilométrage si elle n'existe pas encore.
    # (Inutile avec PostgreSQL : la colonne est créée dès le départ.)
    if not db.USE_PG:
        cursor.execute("PRAGMA table_info(historique)")
        colonnes = [row[1] for row in cursor.fetchall()]

        if "km_intervention" not in colonnes:
            cursor.execute("""
                ALTER TABLE historique
                ADD COLUMN km_intervention INTEGER
            """)

    # Création des composants uniquement si la table est vide.
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

        # Les deux entretiens déjà enregistrés par Math, remis en place
        # une seule fois, quand la base est toute neuve.
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
            }

            .history-item:last-child {
                border-bottom: none;
            }

            .history-details {
                overflow-wrap: anywhere;
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
            async function chargerEtat() {
                try {
                    const res = await fetch('/api/etat');

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
                        card.appendChild(barBg);
                        card.appendChild(button);

                        container.appendChild(card);
                    });

                    await chargerHistorique();

                } catch (e) {
                    console.error(e);
                    document.getElementById('composantsList').innerHTML =
                        '<div class="loading">Impossible de charger les composants.</div>';
                }
            }


            async function chargerHistorique() {
                try {
                    const res = await fetch('/api/historique');

                    if (!res.ok) {
                        throw new Error("Erreur lors du chargement de l'historique.");
                    }

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
                    document.getElementById('historyList').innerHTML =
                        '<p>Impossible de charger l’historique.</p>';
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
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            km_total: km
                        })
                    });

                    const data = await res.json();

                    if (!res.ok) {
                        throw new Error(data.detail || "Erreur de mise à jour.");
                    }

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

                const kmActuelText =
                    document.getElementById('veloInfo').innerText;

                const match = kmActuelText.match(/(\\d+)/);
                const kmActuel = match ? parseInt(match[1], 10) : 0;

                const aujourdhui =
                    new Date().toISOString().split('T')[0];

                try {
                    const res = await fetch(
                        `/api/composants/${id}/remplacer`,
                        {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify({
                                notes: "Intervention standard",
                                cout: 0.0,
                                date_remplacement: aujourdhui
                            })
                        }
                    );

                    const data = await res.json();

                    if (!res.ok) {
                        throw new Error(data.detail || "Erreur lors de l'intervention.");
                    }

                    await chargerEtat();

                } catch (e) {
                    alert(e.message || "Impossible d'enregistrer l'intervention.");
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
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            composant_id,
                            date_remplacement,
                            km_intervention,
                            cout,
                            notes
                        })
                    });

                    const data = await res.json();

                    if (!res.ok) {
                        throw new Error(
                            data.detail || "Impossible d'enregistrer l'intervention."
                        );
                    }

                    document.getElementById('histDate').value = '';
                    document.getElementById('histKm').value = '';
                    document.getElementById('histCout').value = '';
                    document.getElementById('histNotes').value = '';

                    // Actualise les barres et l'historique.
                    await chargerEtat();

                } catch (e) {
                    alert(e.message || "Une erreur est survenue.");
                }
            }


            chargerEtat();
        </script>
    </body>
    </html>
    """

    return html_content


# ============================================================
# API : ÉTAT DU VÉLO ET POURCENTAGES D'USURE
# ============================================================

@app.get("/api/etat")
def obtenir_etat():
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

        resultat = []

        for row in rows:
            km_depart = row["km_depart"] or 0
            seuil = row["seuil_alerte"] or 0

            # Kilométrage parcouru depuis le dernier entretien.
            km = max(0, km_global - km_depart)

            # Le pourcentage n'est pas plafonné :
            # un composant en retard peut afficher 125 %, 150 %, etc.
            pourcentage = (
                round((km / seuil) * 100)
                if seuil > 0
                else 0
            )

            resultat.append({
                "id": row["id"],
                "nom": row["nom"],
                "km_actuel": km,
                "km_depart": km_depart,
                "seuil_alerte": seuil,
                "pourcentage_usure": pourcentage,
                "alerte_entretien": km >= seuil if seuil > 0 else False,
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
def obtenir_historique():
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


# ============================================================
# API : AJOUT D'UNE ANCIENNE INTERVENTION
# ============================================================

@app.post("/api/historique")
def ajouter_historique(data: HistoriqueRequest):
    conn = db.connect()
    cursor = conn.cursor()

    try:
        # Vérifie que le composant existe.
        cursor.execute(
            "SELECT id FROM composants WHERE id = ?",
            (data.composant_id,)
        )

        if cursor.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail="Composant introuvable."
            )

        # Récupère le kilométrage actuel du vélo.
        cursor.execute("SELECT km_total FROM velo WHERE id = 1")
        velo = cursor.fetchone()

        km_total = (
            velo[0]
            if velo and velo[0] is not None
            else 2700
        )

        # Empêche de saisir une intervention dans le futur
        # par rapport au kilométrage actuel.
        if data.km_intervention > km_total:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Le kilométrage de l'intervention ne peut pas "
                    "dépasser celui du vélo."
                )
            )

        # Enregistre l'intervention et son kilométrage.
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

        # Utilise le kilométrage de l'intervention comme nouveau
        # point de départ pour le suivi du composant.
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


# ============================================================
# API : MISE À JOUR DU KILOMÉTRAGE DU VÉLO
# ============================================================

@app.post("/api/sync")
def synchroniser_kilometrage(data: SynchroRequest):
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            UPDATE velo
            SET km_total = ?
            WHERE id = 1
        """, (data.km_total,))

        conn.commit()

        return {
            "statut": "success",
            "km_total": data.km_total
        }

    finally:
        conn.close()


# ============================================================
# API : REMPLACEMENT OU ENTRETIEN RÉALISÉ AUJOURD'HUI
# ============================================================

@app.post("/api/composants/{composant_id}/remplacer")
def remplacer_composant(
    composant_id: int,
    data: RemplacementRequest
):
    conn = db.connect()
    cursor = conn.cursor()

    try:
        # Vérifie que le composant existe.
        cursor.execute(
            "SELECT id FROM composants WHERE id = ?",
            (composant_id,)
        )

        if cursor.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail="Composant introuvable."
            )

        # Récupère le kilométrage actuel du vélo.
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

        # Remet le compteur d'usure du composant à zéro
        # à partir du kilométrage actuel du vélo.
        cursor.execute("""
            UPDATE composants
            SET km_depart = ?
            WHERE id = ?
        """, (
            km_global,
            composant_id
        ))

        # Enregistre également le kilométrage dans l'historique.
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


# ============================================================
# API : SAUVEGARDE COMPLÈTE (à ouvrir de temps en temps, puis Ctrl+S)
# ============================================================

@app.get("/api/sauvegarde")
def sauvegarde():
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT km_total FROM velo WHERE id = 1")
        velo = cursor.fetchone()

        cursor.execute("SELECT * FROM composants ORDER BY id")
        composants = [dict(r) for r in cursor.fetchall()]

        cursor.execute("SELECT * FROM historique ORDER BY id")
        historique = [dict(r) for r in cursor.fetchall()]

        return {
            "date_sauvegarde": str(date.today()),
            "km_total": velo[0] if velo else None,
            "composants": composants,
            "historique": historique
        }

    finally:
        conn.close()
