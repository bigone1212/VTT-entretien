from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import sqlite3
from typing import Optional

app = FastAPI(title="VTTAE Entretien")

DB_NAME = "vtt_entretien.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS velo (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            km_total INTEGER DEFAULT 0
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO velo (id, km_total) VALUES (1, 2700)")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS composants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            km_depart INTEGER DEFAULT 0,
            seuil_alerte INTEGER NOT NULL,
            categorie TEXT DEFAULT 'piece',
            pos_x INTEGER DEFAULT 50,
            pos_y INTEGER DEFAULT 50
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historique (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            composant_id INTEGER,
            date_remplacement TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            cout REAL DEFAULT 0,
            notes TEXT,
            FOREIGN KEY (composant_id) REFERENCES composants (id)
        )
    """)
    cursor.execute("SELECT COUNT(*) FROM composants")
    if cursor.fetchone()[0] == 0:
        composants_initiaux = [
            # 1. Chaîne Shimano XT
            ("Chaîne Shimano XT (Remplacement)", 1200, 2000, "piece", 43, 72),
            # 2. Nettoyage chaîne
            ("Nettoyage & Lubrification chaîne", 2500, 300, "maintenance", 53, 76),
            # 3. Plaquettes de frein avant
            ("Plaquettes de frein avant (N04C)", 0, 1500, "piece", 78, 62),
            # 4. Plaquettes de frein arrière -> bien au-dessus de la cassette
            ("Plaquettes de frein arrière", 0, 1500, "piece", 27, 44),
            # 5. Fourche Fox 36 (50h)
            ("Fourche Fox 36 - Entretien 50h", 0, 800, "maintenance", 79, 44),
            # 6. Fourche Fox 36 (200h)
            ("Fourche Fox 36 - Révision lourde 200h", 0, 3200, "maintenance", 75, 34),
            # 7. Cassette Shimano XT
            ("Cassette Shimano XT 12-speed", 0, 4000, "piece", 31, 74),
            # 8. Pneu arrière / tubeless
            ("Pneu arrière / tubeless", 0, 2500, "piece", 16, 58)
        ]
        cursor.executemany("INSERT INTO composants (nom, km_depart, seuil_alerte, categorie, pos_x, pos_y) VALUES (?, ?, ?, ?, ?, ?)", composants_initiaux)
        conn.commit()
    conn.close()

init_db()

class SynchroRequest(BaseModel):
    km_total: int

class RemplacementRequest(BaseModel):
    notes: Optional[str] = "Intervention standard"
    cout: Optional[float] = 0.0

@app.get("/", response_class=HTMLResponse)
def accueil():
    return """
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
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
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
                margin-bottom: 20px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                border: 1px solid rgba(255,255,255,0.05);
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
            input[type="number"] {
                flex: 1;
                background: #0f1117;
                border: 1px solid #2a303c;
                border-radius: 8px;
                padding: 10px;
                color: #fff;
                font-size: 1.1rem;
                outline: none;
            }
            button.btn-primary {
                background: var(--accent);
                color: white;
                border: none;
                border-radius: 8px;
                padding: 0 16px;
                font-weight: 600;
                cursor: pointer;
            }
            .bike-container {
                position: relative;
                width: 100%;
                max-width: 600px;
                height: 260px;
                margin: 0 auto 20px auto;
                background: #ffffff;
                border-radius: 14px;
                overflow: hidden;
                border: 1px solid rgba(255,255,255,0.05);
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .bike-img {
                max-width: 100%;
                max-height: 100%;
                object-fit: contain;
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
                animation: pulse 1.5s infinite;
            }
            @keyframes pulse {
                0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7); }
                70% { box-shadow: 0 0 0 10px rgba(239, 68, 68, 0); }
                100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
            }
            .grid {
                display: flex;
                flex-direction: column;
                gap: 12px;
                max-width: 600px;
                margin: 0 auto;
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
                margin-bottom: 8px;
            }
            .component-name {
                font-weight: 600;
                font-size: 1rem;
                color: #fff;
            }
            .badge {
                font-size: 0.7rem;
                padding: 3px 8px;
                border-radius: 20px;
                font-weight: 600;
            }
            .badge.ok { background: rgba(16, 185, 129, 0.15); color: var(--success); }
            .badge.warning { background: rgba(239, 68, 68, 0.15); color: var(--danger); }
            
            .stats-row {
                display: flex;
                justify-content: space-between;
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
            .loading {
                text-align: center;
                color: var(--text-muted);
                padding: 30px;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🚲 Cube Stereo Hybrid</h1>
            <p id="veloInfo">Kilométrage total : chargement...</p>
        </div>

        <div class="sync-card" style="max-width: 600px; margin-left: auto; margin-right: auto;">
            <label for="kmInput">Kilométrage total du VTTAE</label>
            <div class="sync-row">
                <input type="number" id="kmInput" placeholder="Ex: 2700">
                <button class="btn-primary" onclick="syncKm()">Mettre à jour</button>
            </div>
        </div>

        <div class="bike-container">
            <img src="https://shop.roulezjeunesse.com/cdn/shop/files/CubeStereoHybridONE44EXC8001.jpg?v=1736521382&width=1220" alt="Mon Cube Stereo Hybrid" class="bike-img">
            <div id="hotspotsContainer"></div>
        </div>

        <div id="composantsList" class="grid">
            <div class="loading">Chargement des composants...</div>
        </div>

        <script>
            async function chargerEtat() {
                try {
                    const res = await fetch('/api/etat');
                    const data = await res.json();
                    
                    document.getElementById('veloInfo').innerText = `Kilométrage total : ${data.km_total} km`;
                    
                    const container = document.getElementById('composantsList');
                    const hotspotsContainer = document.getElementById('hotspotsContainer');
                    container.innerHTML = '';
                    hotspotsContainer.innerHTML = '';
                    
                    data.composants.forEach((c, index) => {
                        const isWarn = c.pourcentage_usure >= 100;
                        const badgeClass = isWarn ? 'warning' : 'ok';
                        const badgeText = isWarn ? 'À faire' : `${c.pourcentage_usure}%`;
                        const btnLabel = c.categorie === 'maintenance' ? '🔧 Marquer Fait' : '⚙️ Remplacer';
                        
                        hotspotsContainer.innerHTML += `
                            <div class="hotspot ${isWarn ? 'warning' : ''}" style="top: ${c.pos_y}%; left: ${c.pos_x}%;" title="${c.nom}" onclick="remplacer(${c.id}, '${c.nom}')">
                                ${index + 1}
                            </div>
                        `;

                        container.innerHTML += `
                            <div class="card" id="card-${c.id}">
                                <div class="card-top">
                                    <div class="component-name">📍 #${index + 1} - ${c.nom}</div>
                                    <div class="badge ${badgeClass}">${badgeText}</div>
                                </div>
                                <div class="stats-row">
                                    <span>Parcouru : <strong>${c.km_actuel} km</strong></span>
                                    <span>Seuil : <strong>${c.seuil_alerte} km</strong></span>
                                </div>
                                <div class="bar-bg">
                                    <div class="bar-fill ${isWarn ? 'warning' : ''}" style="width: ${Math.min(c.pourcentage_usure, 100)}%"></div>
                                </div>
                                <button class="btn-action" onclick="remplacer(${c.id}, '${c.nom}')">${btnLabel}</button>
                            </div>
                        `;
                    });
                } catch (e) {
                    console.error(e);
                }
            }

            async function syncKm() {
                const input = document.getElementById('kmInput');
                const km = parseInt(input.value);
                if(isNaN(km) || km < 0) return;
                await fetch('/api/sync', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({km_total: km})
                });
                input.value = '';
                chargerEtat();
            }

            async function remplacer(id, nom) {
                if(!confirm(`Confirmer la mise à jour pour : ${nom} ?`)) return;
                await fetch(`/api/composants/${id}/remplacer`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({notes: "Mise à jour visuelle mobile", cout: 0})
                });
                chargerEtat();
            }

            chargerEtat();
        </script>
    </body>
    </html>
    """

@app.get("/api/etat")
def obtenir_etat():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT km_total FROM velo WHERE id = 1")
    velo = cursor.fetchone()
    km_global = velo["km_total"] if velo else 0
    
    cursor.execute("SELECT * FROM composants")
    rows = cursor.fetchall()
    conn.close()
    
    resultat = []
    for row in rows:
        km_depart = row["km_depart"]
        km = max(0, km_global - km_depart)
        seuil = row["seuil_alerte"]
        pourcentage = min(round((km / seuil) * 100), 100) if seuil > 0 else 0
        alerte = km >= seuil
        
        resultat.append({
            "id": row["id"],
            "nom": row["nom"],
            "km_actuel": km,
            "seuil_alerte": seuil,
            "pourcentage_usure": pourcentage,
            "alerte_entretien": alerte,
            "categorie": row["categorie"],
            "pos_x": row["pos_x"],
            "pos_y": row["pos_y"]
        })
    return {"km_total": km_global, "composants": resultat}

@app.post("/api/sync")
def synchroniser_kilometrage(data: SynchroRequest):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE velo SET km_total = ? WHERE id = 1", (data.km_total,))
    conn.commit()
    conn.close()
    return {"statut": "success"}

@app.post("/api/composants/{composant_id}/remplacer")
def remplacer_composant(composant_id: int, data: RemplacementRequest):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("SELECT km_total FROM velo WHERE id = 1")
    velo = cursor.fetchone()
    km_global = velo["km_total"] if velo else 0
    
    cursor.execute("UPDATE composants SET km_depart = ? WHERE id = ?", (km_global, composant_id))
    cursor.execute(
        "INSERT INTO historique (composant_id, cout, notes) VALUES (?, ?, ?)",
        (composant_id, data.cout, data.notes)
    )
    
    conn.commit()
    conn.close()
    return {"statut": "success"}
