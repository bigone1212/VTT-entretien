"""Tests des statistiques d'usure (durées réellement tenues). Aucun appel réel au réseau."""
import os
import sys
import time
from pathlib import Path

os.environ["CODE_ACCES"] = "code-de-test"
os.environ["SQLITE_PATH"] = "/tmp/test_stats_usure.db"
if os.path.exists("/tmp/test_stats_usure.db"):
    os.remove("/tmp/test_stats_usure.db")
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient

main.pluie_pause_jusqua = time.time() + 10 ** 9
ENTETE = {"x-code-acces": "code-de-test"}


def test_sorte_de_piece():
    assert main.sorte_de_piece("Chaîne Shimano XT (Remplacement)") == "chaine"
    assert main.sorte_de_piece("Nettoyage & Lubrification chaîne") is None
    assert main.sorte_de_piece("Plaquettes de frein arrière") == "plaquettes_ar"
    assert main.sorte_de_piece("Plaquettes de frein (arrière)") == "plaquettes_ar"
    assert main.sorte_de_piece("Pneu avant / tubeless") == "pneu_av"
    assert main.sorte_de_piece("Liquide préventif tubeless (2 pneus)") is None
    assert main.sorte_de_piece("Galets de dérailleur arrière") == "galets"
    assert main.sorte_de_piece("Disque de frein avant (203 mm)") == "disque_av"


def test_conseil_seulement_avec_accord_et_trois_remplacements():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("DELETE FROM historique")
        cur.execute("DELETE FROM sorties")
        cur.execute("SELECT id, seuil_alerte FROM composants WHERE velo_id = 1 AND categorie = 'piece'"
                    " AND LOWER(nom) LIKE '%pneu%arri%' AND COALESCE(verification, 0) = 0")
        pneu, seuil = cur.fetchone()
        for i, km in enumerate([3000, 6100]):
            cur.execute("INSERT INTO historique (velo_id, composant_id, date_remplacement, km_intervention)"
                        " VALUES (1, ?, ?, ?)", (pneu, "2026-0%d-01" % (i + 1), km))
        conn.commit()
        conn.close()

        groupes = lambda: c.get("/api/statistiques-usure", headers=ENTETE).json()["groupes"]
        assert groupes() == []                                        # pas d'accord : rien
        assert c.put("/api/moi/partage-stats", headers=ENTETE, json={"actif": True}).status_code == 200
        assert c.get("/api/moi", headers=ENTETE).json()["partage_stats"] is True
        g = groupes()[0]
        assert g["sorte"] == "pneu_ar" and g["nombre"] == 2 and g["tenu"] == 3050 and g["conseil"] is None

        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("INSERT INTO historique (velo_id, composant_id, date_remplacement, km_intervention)"
                    " VALUES (1, ?, '2026-03-01', 9300)", (pneu,))
        conn.commit()
        conn.close()
        g = groupes()[0]
        assert g["nombre"] == 3 and g["tenu"] == 3100 and g["prevu"] == seuil and g["conseil"] == 3100

        corps = {"sorte": "pneu_ar", "type_velo": g["type_velo"], "valeur": 999}
        assert c.post("/api/statistiques-usure/appliquer", headers=ENTETE, json=corps).status_code == 409
        corps["valeur"] = 3100
        assert c.post("/api/statistiques-usure/appliquer", headers=ENTETE, json=corps).status_code == 200
        assert groupes()[0]["prevu"] == 3100 and groupes()[0]["conseil"] is None
        assert c.get("/api/statistiques-usure").status_code in (401, 403)
