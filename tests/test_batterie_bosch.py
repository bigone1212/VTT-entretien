"""Tests de l'encadré batterie (cycles de charge, énergie délivrée). Aucun appel réel à Bosch."""
import os
import sys
from pathlib import Path

os.environ["CODE_ACCES"] = "code-de-test"
os.environ["SQLITE_PATH"] = "/tmp/test_batterie_bosch.db"
if os.path.exists("/tmp/test_batterie_bosch.db"):
    os.remove("/tmp/test_batterie_bosch.db")
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient

ENTETE = {"x-code-acces": "code-de-test"}
BIKE = "0ede75c1-3f16-46a0-9c18-f16afd601b50"


def faux_bosch(batteries):
    velo = {"id": BIKE, "driveUnit": {"odometer": 3096890, "powerOnTime": {"total": 150.0}}}
    if batteries is not None:
        velo["batteries"] = batteries
    main.bosch_lire_velos = lambda acces: (None, [velo])


def preparer():
    conn = main.db.connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM bosch_jetons")
    cur.execute(
        "INSERT INTO bosch_jetons (personne_id, access_token, refresh_token, expire_le, mis_a_jour_le) VALUES (1, 'A', 'R', 9999999999, 'x')"
    )
    cur.execute("UPDATE velos SET bosch_bike_id = NULL, bosch_odometre_m = NULL, bosch_heures = NULL, bosch_batterie = NULL WHERE id = 1")
    conn.commit()
    conn.close()


def test_resume_batterie():
    assert main.resume_batterie_bosch(None) is None
    assert main.resume_batterie_bosch([]) is None
    assert main.resume_batterie_bosch([{"productName": "PowerTube"}]) is None
    r = main.resume_batterie_bosch([
        {"productName": "PowerTube 750", "deliveredWhOverLifetime": 10000, "chargeCycles": {"total": 27.9}},
        {"productName": "PowerTube 500", "deliveredWhOverLifetime": 2500, "chargeCycles": {"total": 10.1}},
    ])
    assert r["cycles"] == 38.0 and r["wh"] == 12500 and r["nb"] == 2


def test_sans_bosch_pas_d_encadre():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer()
        assert c.get("/api/etat?velo_id=1", headers=ENTETE).json()["batterie"] is None


def test_synchro_garde_la_batterie_puis_deliaison_l_efface():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer()
        faux_bosch([{"productName": "PowerTube 750", "deliveredWhOverLifetime": 10000, "chargeCycles": {"total": 27.9}}])
        assert c.post("/api/bosch/synchro", headers=ENTETE).status_code == 200
        b = c.get("/api/etat?velo_id=1", headers=ENTETE).json()["batterie"]
        assert b["cycles"] == 27.9 and b["wh"] == 10000

        # Bosch ne renvoie plus de batterie : l'encadré disparaît
        faux_bosch(None)
        c.post("/api/bosch/synchro", headers=ENTETE)
        assert c.get("/api/etat?velo_id=1", headers=ENTETE).json()["batterie"] is None

        # Retour de la batterie, puis déliaison : tout est effacé
        faux_bosch([{"deliveredWhOverLifetime": 5000, "chargeCycles": {"total": 12}}])
        c.post("/api/bosch/synchro", headers=ENTETE)
        assert c.get("/api/etat?velo_id=1", headers=ENTETE).json()["batterie"]["cycles"] == 12
        assert c.delete("/api/bosch/lier/1", headers=ENTETE).status_code == 200
        assert c.get("/api/etat?velo_id=1", headers=ENTETE).json()["batterie"] is None
