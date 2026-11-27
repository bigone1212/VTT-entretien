"""Tests de l'usure selon l'effort (puissance moyenne Bosch). Aucun appel réel à Bosch."""
import json
import os
import sys
from pathlib import Path

os.environ["CODE_ACCES"] = "code-de-test"
os.environ["SQLITE_PATH"] = "/tmp/test_usure_effort.db"
if os.path.exists("/tmp/test_usure_effort.db"):
    os.remove("/tmp/test_usure_effort.db")
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient

ENTETE = {"x-code-acces": "code-de-test"}


def test_puissance_moyenne_lue_dans_les_chiffres_bosch():
    assert main.puissance_moyenne_sortie(None) is None
    assert main.puissance_moyenne_sortie("pas du json") is None
    assert main.puissance_moyenne_sortie({"calories": 500}) is None
    assert main.puissance_moyenne_sortie({"puissance_moy": 0}) is None
    assert main.puissance_moyenne_sortie({"puissance_moy": True}) is None
    assert main.puissance_moyenne_sortie('{"puissance_moy": 130.5}') == 130.5
    assert main.puissance_moyenne_sortie({"puissance_moy": 90}) == 90.0


def test_effort_relatif_compare_a_la_puissance_habituelle():
    r = main.efforts_des_sorties([100, 125, 50, 250, None], 100)
    assert r[0] == 1.0 and abs(r[1] - 1.25) < 1e-9
    assert abs(r[2] - 0.6) < 1e-9          # plancher à 0,6
    assert abs(r[3] - 1.6) < 1e-9          # plafond à 1,6
    assert r[4] == 1.0                     # sans chiffres Bosch : neutre
    assert main.efforts_des_sorties([150], 150) == [1.0]
    assert main.efforts_des_sorties([100], 0) == [1.0]   # référence absurde : 100 W


def test_effort_ne_joue_que_sur_la_transmission():
    reglages = dict(main.REGLAGES_PAR_DEFAUT)
    reglages["poids_effort"] = 0.5
    base = main.facteur_usure("transmission", 30, 600, 600, "mixte", reglages, 1.0, 1.0)
    fort = main.facteur_usure("transmission", 30, 600, 600, "mixte", reglages, 1.0, 1.4)
    doux = main.facteur_usure("transmission", 30, 600, 600, "mixte", reglages, 1.0, 0.8)
    assert abs(fort / base - 1.2) < 1e-9     # 1 + 0,5 × (1,4 - 1)
    assert abs(doux / base - 0.9) < 1e-9     # 1 + 0,5 × (0,8 - 1)
    for famille in ("freins", "autre", "pneus"):
        assert main.facteur_usure(famille, 30, 600, 600, "mixte", reglages, 1.0, 1.4) == \
            main.facteur_usure(famille, 30, 600, 600, "mixte", reglages, 1.0, 1.0)
    # Poids à zéro : l'effort est ignoré
    reglages["poids_effort"] = 0
    assert main.facteur_usure("transmission", 30, 600, 600, "mixte", reglages, 1.0, 1.4) == base


def test_reglage_poids_effort_enregistre_et_relu():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        corps = {"terrain_route": 1.0, "terrain_mixte": 1.15, "terrain_chemin": 1.3, "terrain_sentier": 1.6, "poids_denivele": 1.0}
        # Ancienne version de l'appli (sans le nouveau champ) : accepté, valeur par défaut
        assert c.post("/api/reglages", headers=ENTETE, json=corps).status_code == 200
        assert c.get("/api/reglages", headers=ENTETE).json()["poids_effort"] == 0.5
        assert c.get("/api/reglages", headers=ENTETE).json()["puissance_reference"] == 100.0
        assert c.post("/api/reglages", headers=ENTETE, json={**corps, "poids_effort": 1.0}).status_code == 200
        assert c.get("/api/reglages", headers=ENTETE).json()["poids_effort"] == 1.0
        assert c.post("/api/reglages", headers=ENTETE, json={**corps, "poids_effort": 5}).status_code == 422


def test_usure_de_la_chaine_depend_de_l_effort():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("DELETE FROM sorties WHERE velo_id = 1")
        # 3 sorties douces (90 W) et 1 sortie appuyée (160 W), identiques par ailleurs
        for i, watts in enumerate([90, 90, 90, 160]):
            cur.execute(
                "INSERT INTO sorties (velo_id, debut, distance_km, denivele_pos, denivele_neg, terrain, donnees_bosch, km_compte)"
                " VALUES (1, ?, 40, 800, 800, 'mixte', ?, 0)",
                ("2026-11-%02dT08:00:00Z" % (10 + i), json.dumps({"puissance_moy": watts})),
            )
        conn.commit()
        conn.close()

        def usure_chaine():
            etat = c.get("/api/etat?velo_id=1", headers=ENTETE).json()
            composants = etat["composants"] if "composants" in etat else etat
            chaine = [x for x in composants if "chaîne" in x["nom"].lower() or "chaine" in x["nom"].lower()]
            assert chaine, [x["nom"] for x in composants][:10]
            return chaine[0]["km_usure"]

        corps = {"terrain_route": 1.0, "terrain_mixte": 1.15, "terrain_chemin": 1.3, "terrain_sentier": 1.6, "poids_denivele": 1.0}
        c.post("/api/reglages", headers=ENTETE, json={**corps, "poids_effort": 0, "puissance_reference": 100})
        sans_effort = usure_chaine()
        c.post("/api/reglages", headers=ENTETE, json={**corps, "poids_effort": 1.0, "puissance_reference": 100})
        avec_effort = usure_chaine()
        # Puissances 90, 90, 90 et 160 W contre 100 W : efforts 0,9 ×3 et 1,6 -> plus d'usure au total
        assert avec_effort > sans_effort
        # Une puissance habituelle plus haute allège l'usure
        c.post("/api/reglages", headers=ENTETE, json={**corps, "poids_effort": 1.0, "puissance_reference": 200})
        assert usure_chaine() < sans_effort
