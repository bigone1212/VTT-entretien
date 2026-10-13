from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import sqlite3
from datetime import datetime
import os

app = FastAPI(
    title="API Entretien VTTAE",
    description="API de suivi d'usure pour Cube Stereo Hybrid (Smart System)",
    version="1.0"
)

DB_NAME = "vtt_entretien.db"

class SynchroRequest(BaseModel):
    kilometrage: float

class RemplacementRequest(BaseModel):
    notes: str
    cout: float = 0.0

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

@app.on_event("startup")
def initialiser_base():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS velo (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        kilometrage_total REAL DEFAULT 0,
        derniere_synchro DATETIME
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS composants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        velo_id INTEGER,
        nom TEXT NOT NULL,
        type_suivi TEXT NOT NULL,
        seuil_alerte REAL NOT NULL,
        kilometrage_installation REAL DEFAULT 0,
        heures_installation REAL DEFAULT 0,
        FOREIGN KEY (velo_id) REFERENCES velo(id)
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS historique_entretien (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        composant_id INTEGER,
        date_intervention DATE NOT NULL,
        kilometrage_au_moment REAL NOT NULL,
        notes TEXT,
        cout REAL,
        FOREIGN KEY (composant_id) REFERENCES composants(id)
    );
    """)
    
    cursor.execute("SELECT COUNT(*) FROM velo")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO velo (id, nom, kilometrage_total, derniere_synchro) VALUES (1, 'Cube Stereo Hybrid ONE44 EXC', 1420.5, ?)", (datetime.now(),))
        composants_init = [
            (1, 'Chaîne Shimano XT 12v', 'km', 500, 1000.0, 0.0),
            (1, 'Plaquettes de frein avant', 'km', 1000, 420.0, 0.0),
            (1, 'Pneus Tubeless (Préventif)', 'km', 1500, 0.0, 0.0),
            (1, 'Entretien bas de fourche Fox 36 (50h)', 'heures', 50, 0.0, 12.0)
        ]
        cursor.executemany("INSERT INTO composants (velo_id, nom, type_suivi, seuil_alerte, kilometrage_installation, heures_installation) VALUES (?, ?, ?, ?, ?, ?)", composants_init)
        conn.commit()
    conn.close()

@app.get("/")
def accueil():
    return {"message": "Bienvenue sur l'API d'entretien de ton VTTAE 🚲 !"}

@app.get("/api/etat")
def obtenir_etat_velo():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM velo WHERE id = 1")
    velo = dict(cursor.fetchone())

    cursor.execute("SELECT * FROM composants WHERE velo_id = 1")
    composants_bruts = cursor.fetchall()
    
    composants_formes = []
    for comp in composants_bruts:
        c = dict(comp)
        if c['type_suivi'] == 'km':
            valeur_actuelle = velo['kilometrage_total'] - c['kilometrage_installation']
        else:
            heures_totales = velo['kilometrage_total'] / 25.0
            valeur_actuelle = heures_totales - c['heures_installation']

        pourcentage = (valeur_actuelle / c['seuil_alerte']) * 100

        if pourcentage >= 100:
            statut = "CRITIQUE"
        elif pourcentage >= 80:
            statut = "ATTENTION"
        else:
            statut = "OK"

        c['valeur_actuelle'] = round(valeur_actuelle, 1)
        c['pourcentage_usure'] = round(pourcentage, 1)
        c['statut'] = statut
        composants_formes.append(c)

    conn.close()
    return {
        "velo": velo,
        "composants": composants_formes
    }

@app.post("/api/sync")
def synchroniser_kilometrage(data: SynchroRequest):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE velo SET kilometrage_total = ?, derniere_synchro = ? WHERE id = 1", (data.kilometrage, datetime.now()))
    conn.commit()
    conn.close()
    return {"succes": True, "nouveau_kilometrage": data.kilometrage}

@app.post("/api/composants/{composant_id}/remplacer")
def remplacer_composant(composant_id: int, data: RemplacementRequest):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT kilometrage_total FROM velo WHERE id = 1")
    velo = cursor.fetchone()
    if not velo:
        conn.close()
        raise HTTPException(status_code=404, detail="Vélo non trouvé")
    km_actuel = velo['kilometrage_total']

    cursor.execute("SELECT * FROM composants WHERE id = ?", (composant_id,))
    comp = cursor.fetchone()
    if not comp:
        conn.close()
        raise HTTPException(status_code=404, detail="Composant non trouvé")

    cursor.execute("""
        INSERT INTO historique_entretien (composant_id, date_intervention, kilometrage_au_moment, notes, cout)
        VALUES (?, ?, ?, ?, ?)
    """, (composant_id, datetime.now().date(), km_actuel, data.notes, data.cout))

    if comp['type_suivi'] == 'km':
        cursor.execute("UPDATE composants SET kilometrage_installation = ? WHERE id = ?", (km_actuel, composant_id))
    else:
        heures_actuelles = km_actuel / 25.0
        cursor.execute("UPDATE composants SET heures_installation = ? WHERE id = ?", (heures_actuelles, composant_id))

    conn.commit()
    conn.close()
    return {"succes": True, "message": f"Composant {comp['nom']} réinitialisé à {km_actuel} km."}