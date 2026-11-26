"""Tests de la fiche technique automatique (composants Bosch : nom, série, référence). Aucun appel réel à Bosch."""
import os
import sys
from pathlib import Path

os.environ["CODE_ACCES"] = "code-de-test"
os.environ["SQLITE_PATH"] = "/tmp/test_composants_bosch.db"
if os.path.exists("/tmp/test_composants_bosch.db"):
    os.remove("/tmp/test_composants_bosch.db")
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient

ENTETE = {"x-code-acces": "code-de-test"}
BIKE = "0ede75c1-3f16-46a0-9c18-f16afd601b50"

VELO = {
    "id": BIKE,
    "driveUnit": {"productName": "Drive Unit Performance Line CX", "serialNumber": "17509-0059-99-300-00-0000",
                  "partNumber": "EB1110000A-50", "odometer": 3096890},
    "remoteControl": {"productName": "LED Remote", "serialNumber": "14026-0289-01-A10-00-0000", "partNumber": "EB1310000E"},
    "batteries": [{"productName": "PowerTube 750", "serialNumber": "17331-0067-02-797-00-0016", "partNumber": "EB1210000X"}],
    "antiLockBrakeSystems": [{"productName": "eBike ABS", "serialNumber": "12345-6789-12-345-11-1111", "partNumber": "EB11110002"}],
    "headUnit": {"productName": "Kiox 300", "serialNumber": "13916-0056-01-A06-00-0000", "partNumber": "EB13100003"},
}


def preparer():
    conn = main.db.connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM bosch_jetons")
    cur.execute(
        "INSERT INTO bosch_jetons (personne_id, access_token, refresh_token, expire_le, mis_a_jour_le) VALUES (1, 'A', 'R', 9999999999, 'x')"
    )
    cur.execute("UPDATE velos SET bosch_bike_id = NULL, bosch_odometre_m = NULL, bosch_heures = NULL, bosch_batterie = NULL, bosch_composants = NULL WHERE id = 1")
    conn.commit()
    conn.close()


def test_resume_composants():
    assert main.resume_composants_bosch(None) is None
    assert main.resume_composants_bosch({}) is None
    r = main.resume_composants_bosch(VELO)
    roles = [c["role"] for c in r]
    assert roles == ["Moteur", "Batterie", "Afficheur", "Télécommande", "ABS"]
    moteur = r[0]
    assert moteur["serie"] == "17509-0059-99-300-00-0000" and moteur["reference"] == "EB1110000A-50"
    # Deux batteries : numérotées ; une pièce sans rien : ignorée ; valeurs non texte : ignorées
    deux = main.resume_composants_bosch({
        "batteries": [{"productName": "PT 500", "serialNumber": "S1"}, {"productName": "PT 625", "serialNumber": "S2"}, {}],
        "headUnit": {"productName": True, "serialNumber": ["x"]},
    })
    assert [c["role"] for c in deux] == ["Batterie 1", "Batterie 2"]


def test_sans_bosch_aucun_composant():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer()
        assert c.get("/api/etat?velo_id=1", headers=ENTETE).json()["composants_bosch"] is None


def test_synchro_garde_les_composants_puis_deliaison_les_efface():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer()
        main.bosch_lire_velos = lambda acces: (None, [VELO])
        assert c.post("/api/bosch/synchro", headers=ENTETE).status_code == 200
        liste = c.get("/api/etat?velo_id=1", headers=ENTETE).json()["composants_bosch"]
        assert liste[0]["role"] == "Moteur" and liste[0]["serie"] == "17509-0059-99-300-00-0000"
        assert any(x["role"] == "Batterie" and x["reference"] == "EB1210000X" for x in liste)

        # Bosch ne renvoie plus rien de ce vélo : la fiche se vide
        main.bosch_lire_velos = lambda acces: (None, [{"id": BIKE, "driveUnit": {"odometer": 3096890}}])
        c.post("/api/bosch/synchro", headers=ENTETE)
        assert c.get("/api/etat?velo_id=1", headers=ENTETE).json()["composants_bosch"] is None

        main.bosch_lire_velos = lambda acces: (None, [VELO])
        c.post("/api/bosch/synchro", headers=ENTETE)
        assert c.get("/api/etat?velo_id=1", headers=ENTETE).json()["composants_bosch"]
        assert c.delete("/api/bosch/lier/1", headers=ENTETE).status_code == 200
        assert c.get("/api/etat?velo_id=1", headers=ENTETE).json()["composants_bosch"] is None
