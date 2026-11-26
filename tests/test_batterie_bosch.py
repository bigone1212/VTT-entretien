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

VRAIE_LECTURE_CAPACITE = main.bosch_lire_capacite

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


def test_capacite_mesuree_lue_chez_bosch():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer()
        bat = {"productName": "PowerTube 750", "partNumber": "EB1210000X", "serialNumber": "17331-0067-02-797-00-0016",
               "deliveredWhOverLifetime": 10000, "chargeCycles": {"total": 27.9}}
        faux_bosch([bat])
        # Pas relié : rien à lire
        assert c.get("/api/bosch/capacite?velo_id=1", headers=ENTETE).json() == {"lie": False, "batteries": []}
        assert c.post("/api/bosch/synchro", headers=ENTETE).status_code == 200

        # Mesure de vélociste présente : la plus récente est renvoyée en premier
        main.bosch_lire_capacite = lambda acces, ref, serie: (200, [
            {"date": "2026-05-02", "mesuree_wh": 700.0, "nominale_wh": 750.0, "cycles": 20.0},
        ])
        r = c.get("/api/bosch/capacite?velo_id=1", headers=ENTETE).json()
        assert r["lie"] is True and r["batteries"][0]["mesures"][0]["mesuree_wh"] == 700.0

        # Aucune mesure, puis refus de Bosch : le code est transmis tel quel
        main.bosch_lire_capacite = lambda acces, ref, serie: (200, [])
        assert c.get("/api/bosch/capacite?velo_id=1", headers=ENTETE).json()["batteries"][0]["statut"] == 200
        main.bosch_lire_capacite = lambda acces, ref, serie: (403, [])
        r = c.get("/api/bosch/capacite?velo_id=1", headers=ENTETE).json()
        assert r["batteries"][0]["statut"] == 403 and r["batteries"][0]["mesures"] == []


def test_lecture_des_mesures_bosch_analyse_la_reponse():
    import json as _json
    import urllib.request as _ur

    class Rep:
        def __init__(self, texte): self.texte = texte
        def read(self): return self.texte.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    corps = _json.dumps({"capacityTesters": [
        {"capacityTesterSerialNumber": "BCT1", "createdAt": "2026-01-10T10:00:00Z",
         "batteryData": {"measuredCapacity": 680, "nominalCapacity": 750, "fullChargeCycles": 40.5}},
        {"createdAt": "2026-06-10T10:00:00Z", "batteryData": {"measuredCapacity": 650, "nominalCapacity": 750}},
        {"createdAt": "2026-07-01T10:00:00Z", "batteryData": {"nominalCapacity": 750}},
    ]})
    ancien = _ur.urlopen
    _ur.urlopen = lambda *a, **k: Rep(corps)
    try:
        code, mesures = VRAIE_LECTURE_CAPACITE("A", "EB1210000X", "S")
    finally:
        _ur.urlopen = ancien
    assert code == 200 and [m["date"] for m in mesures] == ["2026-06-10", "2026-01-10"]
    assert mesures[1]["cycles"] == 40.5 and mesures[0]["cycles"] is None


def test_resume_modes_bosch_convertit_les_metres_en_km():
    assert main.resume_modes_bosch(None) is None
    assert main.resume_modes_bosch({}) is None
    r = main.resume_modes_bosch({"activeAssistModes": [
        {"name": "Eco", "reachableRange": 60000},
        {"name": "Tour", "reachableRange": 50400},
        {"name": "eMTB", "reachableRange": 45},      # déjà en km : gardé tel quel
        {"name": "0", "reachableRange": 0},          # sans assistance : ignoré
        {"name": "Turbo", "reachableRange": True},   # valeur invalide : ignorée
        {"name": "Sport", "reachableRange": "x"},    # valeur invalide : ignorée
    ]})
    assert r == [{"nom": "Eco", "km": 60}, {"nom": "Tour", "km": 50}, {"nom": "eMTB", "km": 45}]


def test_synchro_garde_les_modes_avec_ou_sans_batterie():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer()
        modes = [{"name": "Eco", "reachableRange": 60000}, {"name": "Turbo", "reachableRange": 30000}]
        velo = {"id": BIKE, "driveUnit": {"odometer": 3096890, "activeAssistModes": modes},
                "batteries": [{"deliveredWhOverLifetime": 10000, "chargeCycles": {"total": 27.9}}]}
        main.bosch_lire_velos = lambda acces: (None, [velo])
        assert c.post("/api/bosch/synchro", headers=ENTETE).status_code == 200
        b = c.get("/api/etat?velo_id=1", headers=ENTETE).json()["batterie"]
        assert b["cycles"] == 27.9 and b["modes"] == [{"nom": "Eco", "km": 60}, {"nom": "Turbo", "km": 30}]

        # Sans données de batterie, la portée reste affichée
        velo2 = {"id": BIKE, "driveUnit": {"odometer": 3096890, "activeAssistModes": modes}}
        main.bosch_lire_velos = lambda acces: (None, [velo2])
        c.post("/api/bosch/synchro", headers=ENTETE)
        b = c.get("/api/etat?velo_id=1", headers=ENTETE).json()["batterie"]
        assert "cycles" not in b and len(b["modes"]) == 2
