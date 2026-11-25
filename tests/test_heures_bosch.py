"""Tests des entretiens comptés en heures (temps total du vélo Bosch). Aucun appel réel à Bosch."""
import os
import sys
from pathlib import Path

os.environ["CODE_ACCES"] = "code-de-test"
os.environ["SQLITE_PATH"] = "/tmp/test_heures_bosch.db"
if os.path.exists("/tmp/test_heures_bosch.db"):
    os.remove("/tmp/test_heures_bosch.db")
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient

ENTETE = {"x-code-acces": "code-de-test"}
BIKE = "0ede75c1-3f16-46a0-9c18-f16afd601b50"


def faux_bosch(heures, odo=3096890):
    drive = {"odometer": odo}
    if heures is not None:
        drive["powerOnTime"] = {"total": heures, "withMotorSupport": heures / 2}
    main.bosch_lire_velos = lambda acces: (None, [{"id": BIKE, "driveUnit": drive}])


def preparer(c):
    conn = main.db.connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM bosch_jetons")
    cur.execute(
        "INSERT INTO bosch_jetons (personne_id, access_token, refresh_token, expire_le, mis_a_jour_le) VALUES (1, 'A', 'R', 9999999999, 'x')"
    )
    cur.execute("UPDATE velos SET bosch_bike_id = NULL, bosch_odometre_m = NULL, bosch_heures = NULL WHERE id = 1")
    cur.execute("UPDATE composants SET heures_depart = NULL WHERE velo_id = 1")
    conn.commit()
    conn.close()


def piece_en_heures(c):
    pieces = c.get("/api/etat?velo_id=1", headers=ENTETE).json()["composants"]
    return pieces, [p for p in pieces if p["seuil_heures"] > 0]


def test_pieces_d_usage_ont_un_reglage_en_heures_et_le_calendrier_reste_en_jours():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        pieces, en_heures = piece_en_heures(c)
        noms = {p["nom"] for p in en_heures}
        assert "Pneus - pression et état (contrôle)" in noms
        assert "Fourche - pression et SAG (contrôle)" in noms
        # Le calendrier ne passe pas en heures
        for p in pieces:
            if p["nom"].startswith(("Liquide de frein", "Contrôle annuel", "Liquide tubeless", "Batterie")):
                assert p["seuil_heures"] == 0, p["nom"]
        # Sans Bosch : rien en heures, les jours restent la règle
        assert all(p["heures_ecoulees"] is None for p in pieces)
        pneus = [p for p in en_heures if p["nom"].startswith("Pneus")][0]
        assert pneus["seuil_jours"] > 0 and pneus["jours_ecoules"] is not None


def test_comptage_en_heures_puis_remise_a_zero_apres_entretien():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer(c)
        faux_bosch(100.0)
        assert c.post("/api/bosch/synchro", headers=ENTETE).status_code == 200

        # Le compteur démarre à la liaison : 0 h roulée, pas de fausse alerte
        _, en_heures = piece_en_heures(c)
        pneus = [p for p in en_heures if p["nom"].startswith("Pneus")][0]
        assert pneus["heures_ecoulees"] == 0 and pneus["pourcentage_usure"] < 100

        # 6 h plus tard : 6 h sur 8 h = 75 %
        faux_bosch(106.0)
        c.post("/api/bosch/synchro", headers=ENTETE)
        _, en_heures = piece_en_heures(c)
        pneus = [p for p in en_heures if p["nom"].startswith("Pneus")][0]
        assert pneus["heures_ecoulees"] == 6 and pneus["pourcentage_usure"] == 75

        # 10 h plus tard : plus de 100 %
        faux_bosch(110.0)
        c.post("/api/bosch/synchro", headers=ENTETE)
        _, en_heures = piece_en_heures(c)
        pneus = [p for p in en_heures if p["nom"].startswith("Pneus")][0]
        assert pneus["pourcentage_usure"] >= 100

        # « Marquer fait » : le compteur repart de zéro aux heures actuelles
        r = c.post(f"/api/composants/{pneus['id']}/remplacer?velo_id=1", headers=ENTETE, json={})
        assert r.status_code == 200, r.text
        _, en_heures = piece_en_heures(c)
        pneus = [p for p in en_heures if p["nom"].startswith("Pneus")][0]
        assert pneus["heures_ecoulees"] == 0 and pneus["pourcentage_usure"] < 100

        # Les autres pièces ne sont pas touchées par cet entretien
        fourche = [p for p in en_heures if p["nom"].startswith("Fourche")][0]
        assert fourche["heures_ecoulees"] == 10

        # Si on supprime cette ligne d'historique, on retrouve le compteur d'avant (départ à la liaison)
        h = c.get("/api/historique?velo_id=1", headers=ENTETE).json()
        ligne = [x for x in h if x["composant_id"] == pneus["id"]][0]
        assert c.post(f"/api/historique/{ligne['id']}/supprimer?velo_id=1", headers=ENTETE).status_code == 200
        _, en_heures = piece_en_heures(c)
        pneus = [p for p in en_heures if p["nom"].startswith("Pneus")][0]
        assert pneus["heures_ecoulees"] == 10


def test_changer_le_reglage_en_heures_et_ancienne_version_de_l_appli():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer(c)
        faux_bosch(200.0)
        c.post("/api/bosch/synchro", headers=ENTETE)
        pieces, en_heures = piece_en_heures(c)
        pneus = [p for p in en_heures if p["nom"].startswith("Pneus")][0]

        # Nouveau réglage : 20 h
        r = c.post(f"/api/composants/{pneus['id']}/seuils?velo_id=1", headers=ENTETE,
                   json={"seuil_alerte": 0, "seuil_jours": 14, "seuil_heures": 20})
        assert r.status_code == 200
        _, en_heures = piece_en_heures(c)
        assert [p for p in en_heures if p["id"] == pneus["id"]][0]["seuil_heures"] == 20

        # Ancienne version de l'appli (sans le champ heures) : le réglage en heures ne bouge pas
        r = c.post(f"/api/composants/{pneus['id']}/seuils?velo_id=1", headers=ENTETE,
                   json={"seuil_alerte": 0, "seuil_jours": 21})
        assert r.status_code == 200
        _, en_heures = piece_en_heures(c)
        assert [p for p in en_heures if p["id"] == pneus["id"]][0]["seuil_heures"] == 20

        # 0 h choisi par la personne : retour aux jours, et le démarrage ne le remplit pas à sa place
        r = c.post(f"/api/composants/{pneus['id']}/seuils?velo_id=1", headers=ENTETE,
                   json={"seuil_alerte": 0, "seuil_jours": 14, "seuil_heures": 0})
        assert r.status_code == 200
        conn = main.db.connect()
        cur = conn.cursor()
        main.appliquer_seuils_heures(cur)
        conn.commit()
        conn.close()
        pieces, _ = piece_en_heures(c)
        pneus = [p for p in pieces if p["id"] == pneus["id"]][0]
        assert pneus["seuil_heures"] == 0 and pneus["heures_ecoulees"] is None and pneus["jours_ecoules"] is not None

        # Au moins un seuil reste obligatoire
        r = c.post(f"/api/composants/{pneus['id']}/seuils?velo_id=1", headers=ENTETE,
                   json={"seuil_alerte": 0, "seuil_jours": 0, "seuil_heures": 0})
        assert r.status_code == 400


def test_sans_temps_total_le_km_continue_de_se_mettre_a_jour():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer(c)
        faux_bosch(None, odo=3200000)
        r = c.post("/api/bosch/synchro", headers=ENTETE).json()
        assert r["modifies"] == [{"velo_id": 1, "km_total": 3200}]
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("SELECT bosch_heures FROM velos WHERE id = 1")
        assert cur.fetchone()[0] is None
        conn.close()


def test_temps_total_visible_et_effacé_quand_on_delie_le_velo():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        preparer(c)
        faux_bosch(118.4)
        c.post("/api/bosch/synchro", headers=ENTETE)
        etat = c.get("/api/etat?velo_id=1", headers=ENTETE).json()
        assert etat["heures_total"] == 118.4

        # On délie le vélo : plus de temps total, les pièces repassent en jours
        assert c.delete("/api/bosch/lier/1", headers=ENTETE).status_code == 200
        etat = c.get("/api/etat?velo_id=1", headers=ENTETE).json()
        assert etat["heures_total"] is None
        assert all(p["heures_ecoulees"] is None for p in etat["composants"])
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM composants WHERE velo_id = 1 AND heures_depart IS NOT NULL")
        assert cur.fetchone()[0] == 0
        conn.close()
