from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import sqlite3
from typing import Optional
from datetime import date

app = FastAPI(title="VTTAE Entretien")

DB_NAME = "vtt_entretien.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS velo (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            km_total INTEGER DEFAULT 2700
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
            date_remplacement TEXT,
            cout REAL DEFAULT 0,
            notes TEXT,
            FOREIGN KEY (composant_id) REFERENCES composants (id)
        )
    """)
    cursor.execute("SELECT COUNT(*) FROM composants")
    if cursor.fetchone()[0] == 0:
        composants_initiaux = [
            ("Chaîne Shimano XT (Remplacement)", 1200, 2000, "piece", 27, 76),
            ("Nettoyage & Lubrification chaîne", 2445, 300, "maintenance", 35, 76),
            ("Plaquettes de frein avant (N04C)", 0, 1500, "piece", 70, 62),
            ("Plaquettes de frein arrière", 0, 1500, "piece", 22, 47),
            ("Fourche Fox 36 - Entretien 50h", 0, 800, "maintenance", 75, 44),
            ("Fourche Fox 36 - Révision lourde 200h", 0, 3200, "maintenance", 71, 34),
            ("Cassette Shimano XT 12-speed", 0, 4000, "piece", 20, 63),
            ("Pneu arrière / tubeless", 0, 2500, "piece", 7, 58)
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
    date_remplacement: Optional[str] = None

class HistoriqueRequest(BaseModel):
    composant_id: int
    date_remplacement: str
    cout: Optional[float] = 0.0
    notes: Optional[str] = "Ancien entretien"

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
                max-width: 600px;
                margin-left: auto;
                margin-right: auto;
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
            input[type="number"], input[type="text"], input[type="date"], select {
                flex: 1;
                background: #0f1117;
                border: 1px solid #2a303c;
                border-radius: 8px;
                padding: 10px;
                color: #fff;
                font-size: 1rem;
                outline: none;
                width: 100%;
                box-sizing: border-box;
                margin-bottom: 10px;
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
                0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7); }
                70% { box-shadow: 0 0 0 10px rgba(239, 68, 68, 0); }
                100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
            }
            @keyframes pulse-orange {
                0% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.7); }
                70% { box-shadow: 0 0 0 10px rgba(245, 158, 11, 0); }
                100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); }
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
            .badge.near-warning { background: rgba(245, 158, 11, 0.15); color: var(--warning); }
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
            .bar-fill.near-warning { background: var(--warning); }
            .bar-fill.warning { background: var(--danger); }
            
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
                padding: 8px 0;
                border-bottom: 1px solid rgba(255,255,255,0.05);
                display: flex;
                justify-content: space-between;
            }
            .history-item:last-child {
                border-bottom: none;
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

        <div class="sync-card">
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

        <div class="history-section">
            <h2>📜 Ajouter un ancien entretien</h2>
            <select id="histComposant"></select>
            <input type="date" id="histDate">
            <input type="number" step="0.01" id="histCout" placeholder="Coût en € (optionnel)">
            <input type="text" id="histNotes" placeholder="Notes (ex: Acheté chez le bouclard, purge, etc.)">
            <button class="btn-primary" style="width:100%; padding:10px;" onclick="ajouterHistorique()">Enregistrer l'ancien entretien</button>
            
            <h2 style="margin-top: 24px;">📋 Historique des interventions</h2>
            <div id="historyList">
                <div class="loading">Chargement de l'historique...</div>
            </div>
        </div>

        <script>
            async function chargerEtat() {
                try {
                    const res = await fetch('/api/etat');
                    const data = await res.json();
                    
                    document.getElementById('veloInfo').innerText = `Kilométrage total : ${data.km_total} km`;
                    
                    const container = document.getElementById('composantsList');
                    const hotspotsContainer = document.getElementById('hotspotsContainer');
                    const selectComp = document.getElementById('histComposant');
                    
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

                        const badgeText = c.pourcentage_usure >= 100 ? 'À faire' : `${c.pourcentage_usure}%`;
                        const btnLabel = c.categorie === 'maintenance' ? '🔧 Marquer Fait' : '⚙️ Remplacer';
                        
                        selectComp.innerHTML += `<option value="${c.id}">${c.nom}</option>`;

                        hotspotsContainer.innerHTML += `
                            <div class="hotspot ${hotspotClass}" style="top: ${c.pos_y}%; left: ${c.pos_x}%;" title="${c.nom} (${c.pourcentage_usure}%)" onclick="remplacer(${c.id}, '${c.nom}')">
                                ${index + 1}
                            </div>
                        `;

                        container.innerHTML += `
                            <div class="card" id="card-${c.id}">
                                <div class="card-top">
                                    <div class="component-name">📍 #${index + 1} - ${c.nom}</div>
                                    <div class="badge ${statusClass}">${badgeText}</div>
                                </div>
                                <div class="stats-row">
                                    <span>Parcouru : <strong>${c.km_actuel} km</strong></span>
                                    <span>Seuil : <strong>${c.seuil_alerte} km</strong></span>
                                </div>
                                <div class="bar-bg">
                                    <div class="bar-fill ${statusClass}" style="width: ${Math.min(c.pourcentage_usure, 100)}%"></div>
                                </div>
                                <button class="btn-action" onclick="remplacer(${c.id}, '${c.nom}')">${btnLabel}</button>
                            </div>
                        `;
                    });

                    chargerHistorique();
                } catch (e) {
                    console.error(e);
                }
            }

            async function chargerHistorique() {
                const res = await fetch('/api/historique');
                const history = await res.json();
                const listDiv = document.getElementById('historyList');
                listDiv.innerHTML = '';
                
                if(history.length === 0) {
                    listDiv.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem;">Aucune intervention enregistrée.</p>';
                    return;
                }

                history.forEach(h => {
                    listDiv.innerHTML += `
                        <div class="history-item">
                            <div>
                                <strong>${h.composant_nom}</strong><br>
                                <span style="color: var(--text-muted);">${h.notes || 'Intervention réalisée'} - <em>${h.date_remplacement}</em></span>
                            </div>
                            <div style="text-align: right; font-weight: 600; color: var(--accent);">
                                ${h.cout > 0 ? h.cout + ' €' : ''}
                            </div>
                        </div>
                    `;
                });
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
                
                const aujourdhui = new Date().toISOString().split('T')[0];
                
                await fetch(`/api/composants/${id}/remplacer`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        notes: "Intervention standard", 
                        cout: 0.0,
                        date_remplacement: aujourdhui
                    })
                });
                chargerEtat();
            }

            async function ajouterHistorique() {
                const composant_id = document.getElementById('histComposant').value;
                const date_remplacement = document.getElementById('histDate').value;
                const cout = parseFloat(document.getElementById('histCout').value) || 0.0;
                const notes = document.getElementById('histNotes').value || "Ancien entretien";

                if(!date_remplacement) {
                    alert("Veuillez sélectionner une date.");
                    return;
                }

                await fetch(`/api/historique`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({composant_id: parseInt(composant_id), date_remplacement, cout, notes})
                });

                document.getElementById('histDate').value = '';
                document.getElementById('histCout').value = '';
                document.getElementById('histNotes').value = '';
                chargerEtat();
            }

            chargerEtat();
        </script>
    </body>
    </html>
    """
    return html_content

@app.get("/api/etat")
def obtenir_etat():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT km_total FROM velo WHERE id = 1")
    velo = cursor.fetchone()
    km_global = velo["km_total"] if velo and velo["km_total"] is not None else 2700
    
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

@app.get("/api/historique")
def obtenir_historique():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT h.id, c.nom as composant_nom, h.date_remplacement, h.cout, h.notes 
        FROM historique h 
        JOIN composants c ON h.composant_id = c.id 
        ORDER BY h.id DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

@app.post("/api/historique")
def ajouter_historique(data: HistoriqueRequest):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO historique (composant_id, date_remplacement, cout, notes) VALUES (?, ?, ?, ?)",
        (data.composant_id, data.date_remplacement, data.cout, data.notes)
    )
    conn.commit()
    conn.close()
    return {"statut": "success"}

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
    km_global = velo["km_total"] if velo and velo["km_total"] is not None else 2700
    
    cursor.execute("UPDATE composants SET km_depart = ? WHERE id = ?", (km_global, composant_id))
    
    date_resp = data.date_remplacement if data.date_remplacement else str(date.today())
    notes_resp = data.notes if data.notes else "Intervention standard"
    cout_resp = data.cout if data.cout is not None else 0.0
    
    cursor.execute(
        "INSERT INTO historique (composant_id, date_remplacement, cout, notes) VALUES (?, ?, ?, ?)",
        (composant_id, date_resp, cout_resp, notes_resp)
    )
    
    conn.commit()
    conn.close()
    return {"statut": "success"}
