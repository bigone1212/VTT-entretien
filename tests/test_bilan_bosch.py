"""Tests du bilan des sorties calculé avec les données Bosch. Aucun appel réel à Bosch."""
import json
import os
import sys
from pathlib import Path

os.environ["CODE_ACCES"] = "code-de-test"
os.environ["SQLITE_PATH"] = "/tmp/test_bilan_bosch.db"
if os.path.exists("/tmp/test_bilan_bosch.db"):
    os.remove("/tmp/test_bilan_bosch.db")
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient

VRAIE_LECTURE = main.bosch_lire_toutes_activites

ENTETE = {"x-code-acces": "code-de-test"}
BIKE = "0ede75c1-3f16-46a0-9c18-f16afd601b50"
AUTRE = "11111111-2222-3333-4444-555555555555"


def preparer():
    main.BILAN_BOSCH_CACHE.clear()
    conn = main.db.connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM bosch_jetons")
    cur.execute("INSERT INTO bosch_jetons (personne_id, access_token, refresh_token, expire_le, mis_a_jour_le) VALUES (1, 'A', 'R', 9999999999, 'x')")
    cur.execute("UPDATE velos SET bosch_bike_id = NULL WHERE id = 1")
    conn.commit()
    conn.close()


def relier():
    conn = main.db.connect()
    cur = conn.cursor()
    cur.execute("UPDATE velos SET bosch_bike_id = ? WHERE id = 1", (BIKE,))
    conn.commit()
    conn.close()


ACTIVITES = [
    {"id": "a", "bikeId": BIKE, "startTime": "2026-11-20T23:30:00Z", "timeZone": "Europe/Paris", "distance": 25500,
     "durationWithoutStops": 5400, "riderPower": {"average": 120.4}, "caloriesBurned": 800, "elevation": {"gain": 640.4}},
    {"id": "b", "bikeId": AUTRE, "startTime": "2026-11-21T08:00:00Z", "timeZone": "Europe/Paris", "distance": 10000},
    {"id": "c", "bikeId": BIKE, "startTime": "2026-03-02T08:00:00Z", "distance": 12000, "durationWithoutStops": 3000},
    {"id": "d", "bikeId": BIKE, "startTime": "pas une date", "distance": 5000},
]


def test_resume_activite_jour_local_et_unites():
    r = main.resume_activite_bosch(ACTIVITES[0])
    assert r["jour"] == "2026-11-21"  # 23h30 UTC = 00h30 à Paris
    assert r["debut"] == "2026-11-20T23:30:00Z"
    assert r["debut_m"] is None and r["fin_m"] is None
    assert main.resume_activite_bosch({**ACTIVITES[0], "startOdometer": 3090700, "distance": 6200})["fin_m"] == 3096900
    assert r["km"] == 25.5 and r["dplus"] == 640 and r["duree"] == 5400 and r["watts"] == 120.4 and r["cal"] == 800
    r = main.resume_activite_bosch(ACTIVITES[2])
    assert r["jour"] == "2026-03-02" and r["watts"] is None and r["cal"] is None and r["dplus"] == 0
    assert main.resume_activite_bosch(ACTIVITES[3]) is None
    assert main.resume_activite_bosch({"startTime": "2026-01-01T10:00:00Z"}) is None


def test_bilan_non_relie():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer()
        assert c.get("/api/bosch/bilan?velo_id=1", headers=ENTETE).json() == {"lie": False, "activites": []}


def test_bilan_filtre_le_velo_et_garde_en_memoire():
    appels = []

    def faux(acces):
        appels.append(1)
        return 200, ACTIVITES

    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer()
        relier()
        main.bosch_lire_toutes_activites = faux
        r = c.get("/api/bosch/bilan?velo_id=1", headers=ENTETE).json()
        assert r["lie"] is True and [a["jour"] for a in r["activites"]] == ["2026-11-21", "2026-03-02"]
        assert "velo" not in r["activites"][0]
        c.get("/api/bosch/bilan?velo_id=1", headers=ENTETE)
        assert len(appels) == 1  # deuxième lecture servie par la mémoire


def test_bilan_erreur_bosch():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer()
        relier()
        main.bosch_lire_toutes_activites = lambda acces: (403, [])
        assert c.get("/api/bosch/bilan?velo_id=1", headers=ENTETE).json() == {"lie": True, "erreur": 403, "activites": [], "compteur_m": None, "releves": []}


def test_lecture_par_pages():
    import urllib.request as ur

    class Rep:
        def __init__(self, t): self.t = t
        def read(self): return self.t.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    adresses = []

    def faux_urlopen(demande, timeout=0):
        adresses.append(demande.full_url)
        debut = 0 if "offset=0" in demande.full_url else 100
        n = 100 if debut == 0 else 30
        page = [{"id": str(debut + i), "bikeId": BIKE, "startTime": "2026-01-01T10:00:00Z", "distance": 1000} for i in range(n)]
        return Rep(json.dumps({"activitySummaries": page, "pagination": {"total": 130}}))

    ancien = ur.urlopen
    ur.urlopen = faux_urlopen
    try:
        code, toutes = VRAIE_LECTURE("A")
    finally:
        ur.urlopen = ancien
    assert code == 200 and len(toutes) == 130 and len(adresses) == 2


def test_compteur_releve_a_chaque_synchro_et_efface_a_la_deliaison():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer()
        relier()
        main.bosch_lire_toutes_activites = lambda acces: (200, [])
        main.bosch_lire_velos = lambda acces: (None, [{"id": BIKE, "driveUnit": {"odometer": 3000000}}])
        assert c.post("/api/bosch/synchro", headers=ENTETE).status_code == 200
        main.bosch_lire_velos = lambda acces: (None, [{"id": BIKE, "driveUnit": {"odometer": 3012500}}])
        assert c.post("/api/bosch/synchro", headers=ENTETE).status_code == 200
        r = c.get("/api/bosch/bilan?velo_id=1", headers=ENTETE).json()
        assert r["compteur_m"] == 3012500
        assert len(r["releves"]) == 1 and r["releves"][0]["m"] == 3012500  # une seule valeur par jour : la dernière
        assert c.delete("/api/bosch/lier/1", headers=ENTETE).status_code == 200
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM bosch_odometre_jours WHERE velo_id = 1")
        assert cur.fetchone()[0] == 0
        conn.close()


def test_records_de_chaque_sortie():
    r = main.resume_activite_bosch({
        "bikeId": BIKE, "startTime": "2026-06-01T10:00:00Z", "distance": 30000,
        "speed": {"average": 18, "maximum": 54.3}, "riderPower": {"average": 110, "maximum": 640},
        "tricks": {"jumps": {"amount": 4, "maxDistance": 5.2, "maxDuration": 900, "maxHeight": 1250},
                   "wheelies": {"amount": 2, "maxDistance": 12.5, "maxAngle": 30},
                   "manuals": {"amount": 0, "maxDistance": 0}},
    })
    assert r["rec"] == {"vmax": 54.3, "pmax": 640, "saut_nb": 4, "saut_long_m": 5.2, "saut_haut_m": 1.25,
                        "wheelie_nb": 2, "wheelie_long_m": 12.5, "wheelie_angle": 30.0}
    assert main.resume_activite_bosch({"startTime": "2026-06-01T10:00:00Z", "distance": 1000})["rec"] == {}
