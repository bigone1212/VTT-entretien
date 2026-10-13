from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import sqlite3
from typing import List, Optional

app = FastAPI(
    title="API Entretien VTTAE",
    description="API de suivi d'usure pour Cube Stereo Hybrid (Smart System)",
    version="1.0"
)

DB_NAME = "vtt_entretien.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Table des composants
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS composants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            km_actuel INTEGER DEFAULT 0,
            seuil_alerte INTEGER NOT NULL
        )
    """)
    
    # Table de l'historique des entretiens/remplacements
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
    
    # Insertion des composants initiaux s'ils n'existent pas déjà
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

# Initialisation de la base au démarrage
init_db()

class SynchroRequest(BaseModel):
    km_total: int

class RemplacementRequest(BaseModel):
    notes: Optional[str] = "Remplacement standard"
    cout: Optional[float] = 0.0

@app.get("/")
def accueil():
    return {"message": "Bienvenue sur l'API d'entretien de ton VTTAE 🚴!"}

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
    # Met à jour le kilométrage de tous les composants par rapport au total vélo
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE composants SET km_actuel = ?", (data.km_total,))
    conn.commit()
    conn.close()
    return {"statut": "success", "nouveau_km_global": data.km_total}

@app.post("/api/composants/{composant_id}/remplacer")
def remplacer_composant(composant_id: int, data: RemplacementRequest):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Vérifier si le composant existe
    cursor.execute("SELECT * FROM composants WHERE id = ?", (composant_id,))
    comp = cursor.fetchone()
    if not comp:
        conn.close()
        raise HTTPException(status_code=404, detail="Composant non trouvé")
        
    # Remettre le km à 0 pour ce composant
    cursor.execute("UPDATE composants SET km_actuel = 0 WHERE id = ?", (composant_id,))
    
    # Enregistrer dans l'historique
    cursor.execute(
        "INSERT INTO historique (composant_id, cout, notes) VALUES (?, ?, ?)",
        (composant_id, data.cout, data.notes)
    )
    
    conn.commit()
    conn.close()
    return {"statut": "success", "message": f"Composant ID {composant_id} réinitialisé avec succès."}
