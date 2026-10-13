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
        CREATE TABLE IF NOT EXISTS composants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            km_actuel INTEGER DEFAULT 0,
            seuil_alerte INTEGER NOT NULL
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
            ("Chaîne Shimano XT", 0, 2000),
            ("Plaquettes de frein avant (N04C)", 0, 1500),
            ("Plaquettes de frein arrière", 0, 1500),
            ("Fourche Fox 36 Float AWL", 0, 50),
            ("Cassette Shimano XT 12-speed", 0, 4000),
            ("Pneu arrière / tubeless", 0, 2500)
        ]
        cursor.executemany("INSERT INTO composants (nom, km_actuel, seuil_alerte) VALUES (?, ?, ?)", composants_initiaux)
        conn.commit()
    conn.close()

init_db()

class SynchroRequest(BaseModel):
    km_total: int

class RemplacementRequest(BaseModel):
    notes: Optional[str] = "Remplacement standard"
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
                margin-bottom: 24px;
            }
            .header h1 {
                font-size: 1.5rem;
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
                padding: 16px;
                border-radius: 14px;
                margin-bottom: 20px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                border: 1px solid rgba(255,255,255,0.05);
            }
            .sync-card label {
                display: block;
                font-size: 0.8rem;
                color: var(--text-muted);
                margin-bottom: 8px;
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
                padding: 12px;
                color: #fff;
                font-size: 1.1rem;
                outline: none;
            }
            input[type="number"]:focus {
                border-color: var(--accent);
            }
            button.btn-primary {
                background: var(--accent);
                color: white;
                border: none;
                border-radius: 8px;
                padding: 0 20px;
                font-weight: 600;
                font-size: 0.95rem;
                cursor: pointer;
                transition: background 0.2s;
            }
            button.btn-primary:active {
                background: var(--accent-hover);
            }
            .grid {
                display: flex;
                flex-direction: column;
                gap: 12px;
            }
            .card {
                background: var(--card-bg);
                border-radius: 14px;
                padding: 16px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.2);
                border: 1px solid rgba(255,255,255,0.05);
                position: relative;
                overflow: hidden;
            }
            .card-top {
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 10px;
            }
            .component-name {
                font-weight: 600;
                font-size: 1.05rem;
                color: #fff;
            }
            .badge {
                font-size: 0.75rem;
                padding: 4px 8px;
                border-radius: 20px;
                font-weight: 600;
            }
            .badge.ok { background: rgba(16, 185, 129, 0.15); color: var(--success); }
            .badge.warning { background: rgba(239, 68, 68, 0.15); color: var(--danger); }
            
            .stats-row {
                display: flex;
                justify-content: space-between;
                font-size: 0.85rem;
                color: var(--text-muted);
                margin-bottom: 8px;
            }
            .stats-row span strong {
                color: #fff;
            }
            .bar-bg {
                background: #0f1117;
                border-radius: 6px;
                height: 8px;
                width: 100%;
                overflow: hidden;
                margin-bottom: 14px;
            }
            .bar-fill {
                height: 100%;
                background: var(--success);
                border-radius: 6px;
                transition: width 0.4s ease;
            }
            .bar-fill.warning {
                background: var(--danger);
            }
            button.btn-action {
                background: rgba(255,255,255,0.05);
                color: var(--text-color);
                border: 1px solid rgba(255,255,255,0.1);
                width: 100%;
                padding: 10px;
                border-radius: 8px;
                font-size: 0.9rem;
                font-weight: 500;
                cursor: pointer;
                transition: all 0.2s;
            }
            button.btn-action:active {
                background: rgba(255,255,255,0.15);
            }
            .loading {
                text-align: center;
                color: var(--text-muted);
                padding: 40px;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🚲 Cube Stereo Hybrid</h1>
            <p>Tableau de bord d'usure & maintenance</p>
        </div>

        <div class="sync-card">
            <label for="kmInput">Kilométrage total du VTTAE</label>
            <div class="sync-row">
                <input type="number" id="kmInput" placeholder="Ex: 1250">
                <button class="btn-primary" onclick="syncKm()">Mettre à jour</button>
            </div>
        </div>

        <div id="composantsList" class="grid">
            <div class="loading">Chargement des composants...</div>
        </div>

        <script>
            async function chargerEtat() {
                try {
                    const res = await fetch('/api/etat');
                    const data = await res.json();
                    const container = document.getElementById('composantsList');
                    container.innerHTML = '';
                    
                    data.composants.forEach(c => {
                        const isWarn = c.pourcentage_usure >= 100;
                        const badgeClass = isWarn ? 'warning' : 'ok';
                        const badgeText = isWarn ? 'À remplacer' : `${c.pourcentage_usure}% usé`;
                        
                        container.innerHTML += `
                            <div class="card">
                                <div class="card-top">
                                    <div class="component-name">${c.nom}</div>
                                    <div class="badge ${badgeClass}">${badgeText}</div>
                                </div>
                                <div class="stats-row">
                                    <span>Actuel : <strong>${c.km_actuel} km</strong></span>
                                    <span>Seuil : <strong>${c.seuil_alerte} km</strong></span>
                                </div>
                                <div class="bar-bg">
                                    <div class="bar-fill ${isWarn ? 'warning' : ''}" style="width: ${Math.min(c.pourcentage_usure, 100)}%"></div>
                                </div>
                                <button class="btn-action" onclick="remplacer(${c.id}, '${c.nom}')">⚙️ Déclarer Remplacement</button>
                            </div>
                        `;
                    });
                } catch (e) {
                    document.getElementById('composantsList').innerHTML = '<div class="loading" style="color:var(--danger)">Erreur de chargement des données.</div>';
                }
            }

            async function syncKm() {
                const input = document.getElementById('kmInput');
                const km = parseInt(input.value);
                if(isNaN(km) || km < 0) {
                    alert("Entre un kilométrage valide.");
                    return;
                }
                await fetch('/api/sync', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({km_total: km})
                });
                input.value = '';
                chargerEtat();
            }

            async function remplacer(id, nom) {
                if(!confirm(`Confirmer le remplacement / entretien de : ${nom} ?`)) return;
                await fetch(`/api/composants/${id}/remplacer`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({notes: "Remplacement interface mobile", cout: 0})
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
    cursor.execute("SELECT * FROM composants")
    rows = cursor.fetchall()
    conn.close()
    
    resultat = []
    for row in rows:
        km = row["km_actuel"]
        seuil = row["seuil_alerte"]
        pourcentage = min(round((km / seuil) * 100), 100) if seuil > 0 else 0
        alerte = km >= seuil
        resultat.append({
            "id": row["id"],
            "nom": row["nom"],
            "km_actuel": km,
            "seuil_alerte": seuil,
            "pourcentage_usure": pourcentage,
            "alerte_entretien": alerte
        })
    return {"composants": resultat}

@app.post("/api/sync")
def synchroniser_kilometrage(data: SynchroRequest):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE composants SET km_actuel = ?", (data.km_total,))
    conn.commit()
    conn.close()
    return {"statut": "success"}

@app.post("/api/composants/{composant_id}/remplacer")
def remplacer_composant(composant_id: int, data: RemplacementRequest):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM composants WHERE id = ?", (composant_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Composant non trouvé")
    cursor.execute("UPDATE composants SET km_actuel = 0 WHERE id = ?", (composant_id,))
    cursor.execute("INSERT INTO historique (composant_id, cout, notes) VALUES (?, ?, ?)", (composant_id, data.cout, data.notes))
    conn.commit()
    conn.close()
    return {"statut": "success"}
