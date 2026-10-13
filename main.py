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
            # 4. Plaquettes de frein arrière -> légèrement remonté
            ("Plaquettes de frein arrière", 0, 1500, "piece", 30, 47),
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
            .badge.warning { background: rgba(239, 68
