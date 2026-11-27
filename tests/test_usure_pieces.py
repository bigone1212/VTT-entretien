"""Tests de l'usure adaptée à chaque famille de pièces (terrain, pluie). Aucun appel réel au réseau."""
import json
import os
import sys
import time
from pathlib import Path

os.environ["CODE_ACCES"] = "code-de-test"
os.environ["SQLITE_PATH"] = "/tmp/test_usure_pieces.db"
if os.path.exists("/tmp/test_usure_pieces.db"):
    os.remove("/tmp/test_usure_pieces.db")
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient

main.pluie_pause_jusqua = time.time() + 10 ** 9   # aucun appel réseau pendant les tests
ENTETE = {"x-code-acces": "code-de-test"}
REGLAGES = dict(main.REGLAGES_PAR_DEFAUT)


def trace_droite(n):
    return [[44.0 + i * 0.001, 2.0, i * 10, 500] for i in range(n)]


def test_parts_du_terrain_analyse_ou_choisi():
    # Pas d'analyse : répartition du terrain choisi
    assert main.parts_terrain_sortie("[]", None, "route") == ({"route": 1.0}, False)
    parts, analyse = main.parts_terrain_sortie("[]", None, "mixte")
    assert analyse is False and abs(sum(parts.values()) - 1.0) < 1e-9
    # Analyse : 5 points (4 tronçons égaux) = 2 route, 1 chemin, 1 sentier
    trace = json.dumps(trace_droite(5))
    parts, analyse = main.parts_terrain_sortie(trace, "rrcsu", "route")
    assert analyse is True
    # le dernier code (point d'arrivée) n'a pas de tronçon ; tronçons : r, r, c, s
    assert abs(parts["route"] - 0.5) < 1e-9 and abs(parts["chemin"] - 0.25) < 1e-9 and abs(parts["sentier"] - 0.25) < 1e-9
    # Partie inconnue : suit le terrain choisi (ici chemin)
    parts, _ = main.parts_terrain_sortie(trace, "ruuuu", "chemin")
    assert abs(parts["route"] - 0.25) < 1e-9 and abs(parts["chemin"] - 0.75) < 1e-9


def test_chaque_famille_reagit_au_terrain():
    route = ({"route": 1.0}, False)
    f = lambda famille: main.coef_terrain_famille(famille, route[0], route[1], "route", REGLAGES)
    assert abs(f("pneu") - 0.65) < 1e-9          # 1 500 km conseillés -> environ 2 300 km sur route
    assert abs(f("suspension") - 0.15) < 1e-9    # la fourche ne s'use presque pas sur la route
    assert abs(f("freins") - 0.8) < 1e-9
    assert f("autre") == 1.0
    # Sentier : tout s'use plus vite que sur route
    sentier = ({"sentier": 1.0}, False)
    for famille in ("pneu", "suspension", "freins", "transmission"):
        assert main.coef_terrain_famille(famille, sentier[0], sentier[1], "sentier", REGLAGES) > f(famille)
    # Terrain analysé : les coefficients des réglages pondèrent chaque type de chemin
    analyse = ({"route": 0.5, "chemin": 0.5, "sentier": 0.0}, True)
    attendu = 0.5 * 1.0 * 0.65 + 0.5 * 1.3 * 0.85
    assert abs(main.coef_terrain_famille("pneu", analyse[0], True, "mixte", REGLAGES) - attendu) < 1e-9


def test_pluie_alourdit_surtout_freins_et_chaine():
    assert main.facteur_pluie("freins", None) == 1.0
    assert main.facteur_pluie("freins", 0) == 1.0
    assert abs(main.facteur_pluie("freins", 3) - 1.6) < 1e-9
    assert abs(main.facteur_pluie("freins", 30) - 1.6) < 1e-9       # plafonné
    assert abs(main.facteur_pluie("transmission", 1.5) - 1.2) < 1e-9
    assert main.facteur_pluie("autre", 5) == 1.0
    sec = main.facteur_usure("freins", 30, 500, 500, "mixte", REGLAGES, 1.0, 1.0, 0)
    mouille = main.facteur_usure("freins", 30, 500, 500, "mixte", REGLAGES, 1.0, 1.0, 3)
    assert abs(mouille / sec - 1.6) < 1e-9


def test_lecture_de_la_pluie_open_meteo():
    import urllib.request as ur

    class Rep:
        def __init__(self, t): self.t = t
        def read(self): return self.t.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    adresses = []

    def faux(valeurs):
        def f(demande, timeout=0):
            adresses.append(demande.full_url)
            heures = ["2026-10-05T%02d:00" % h for h in range(7, 11)]
            return Rep(json.dumps({"hourly": {"time": heures, "precipitation": valeurs}}))
        return f

    ancien = ur.urlopen
    try:
        # Sortie de 8 h 30 à 9 h 30 (UTC) : une demi-heure dans chacune des heures 8 h et 9 h
        ur.urlopen = faux([0.0, 2.0, 4.0, 1.0])
        pluie = main.lire_pluie_open_meteo(44.0, 2.0, "2026-10-05T08:30:00Z", 3600)
        assert abs(pluie - 3.0) < 1e-9
        assert "archive-api" in adresses[0] or "forecast" in adresses[0]
        # Mesure manquante : on réessaiera plus tard
        ur.urlopen = faux([0.0, None, 4.0, 1.0])
        assert main.lire_pluie_open_meteo(44.0, 2.0, "2026-10-05T08:30:00Z", 3600) is None
    finally:
        ur.urlopen = ancien


def test_usure_des_pieces_dans_l_etat():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("DELETE FROM sorties WHERE velo_id = 1")
        for i in range(4):
            cur.execute(
                "INSERT INTO sorties (velo_id, debut, distance_km, denivele_pos, denivele_neg, terrain, km_compte)"
                " VALUES (1, ?, 100, 200, 200, 'route', 0)", ("2026-11-%02dT08:00:00Z" % (10 + i),))
        conn.commit()
        conn.close()

        def pieces():
            etat = c.get("/api/etat?velo_id=1", headers=ENTETE).json()
            liste = etat["composants"] if "composants" in etat else etat
            par_famille = {}
            for x in liste:
                if x.get("verification"):
                    continue
                par_famille.setdefault(main.famille_de(x["nom"]), []).append(x)
            return par_famille

        sec = pieces()
        assert sec["pneu"] and sec["freins"] and sec["suspension"]
        pneu = sec["pneu"][0]
        fourche = sec["suspension"][0]
        # 400 km de route : le pneu s'use moins vite que les km, la fourche presque pas
        assert pneu["km_usure"] < pneu["km_actuel"]
        assert fourche["km_usure"] < pneu["km_usure"]

        # Les mêmes sorties sous la pluie : plaquettes plus usées, fourche et pneu un peu aussi
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("UPDATE sorties SET pluie_mm = 6 WHERE velo_id = 1")
        conn.commit()
        conn.close()
        mouille = pieces()
        assert mouille["freins"][0]["km_usure"] > sec["freins"][0]["km_usure"]
        assert mouille["pneu"][0]["km_usure"] >= sec["pneu"][0]["km_usure"]


def test_cadence_basse_use_la_chaine():
    assert main.facteur_cadence(None) == 1.0
    assert main.facteur_cadence(80) == 1.0
    assert main.facteur_cadence(60) == 1.0
    assert abs(main.facteur_cadence(50) - 1.12) < 1e-9
    assert main.facteur_cadence(10) == 1.3          # plafonné
    assert abs(main.facteur_cadence(105) - 1.04) < 1e-9
    assert main.facteur_cadence(200) == 1.1
    reglages = dict(REGLAGES)
    reglages["poids_effort"] = 1.0
    doux = main.facteur_usure("transmission", 30, 500, 500, "mixte", reglages, 1.0, 1.0, None, 85)
    lent = main.facteur_usure("transmission", 30, 500, 500, "mixte", reglages, 1.0, 1.0, None, 50)
    assert abs(lent / doux - 1.12) < 1e-9
    # Les autres familles ignorent la cadence
    freins_lent = main.facteur_usure("freins", 30, 500, 500, "mixte", reglages, 1.0, 1.0, None, 50)
    freins_doux = main.facteur_usure("freins", 30, 500, 500, "mixte", reglages, 1.0, 1.0, None, 85)
    assert freins_lent == freins_doux


def test_chaleur_poids_et_chocs():
    assert main.facteur_chaleur("freins", 20) == 1.0
    assert abs(main.facteur_chaleur("freins", 30) - 1.05) < 1e-9
    assert main.facteur_chaleur("freins", 60) == 1.15
    assert main.facteur_chaleur("pneu", 35) == 1.0
    assert main.facteur_chaleur("freins", None) == 1.0
    # Poids : référence 85 + 2,5 + 25 kg ; plus lourd = plus d'usure des freins, de la fourche et des roues
    assert main.facteur_charge("freins", 85, 2.5) == 1.0
    assert main.facteur_charge("freins", 105, 2.5) > 1.0
    assert main.facteur_charge("freins", 70, 2.5) < 1.0
    assert main.facteur_charge("transmission", 120, 5) == 1.0
    assert main.facteur_charge("freins", None, None) == 1.0   # poids inconnu : référence
    # Chocs : 10 sauts sur 10 km = surplus plafonné à 30 % x poids de la famille
    assert main.facteur_chocs("suspension", 0) == 1.0
    assert abs(main.facteur_chocs("suspension", 5) - 1.15) < 1e-9
    assert abs(main.facteur_chocs("suspension", 50) - 1.3) < 1e-9
    assert abs(main.facteur_chocs("roues", 50) - 1.24) < 1e-9
    assert main.facteur_chocs("freins", 50) == 1.0


def test_donnees_bosch_cadence_et_sauts():
    assert main.cadence_moyenne_sortie(None) is None
    assert main.cadence_moyenne_sortie({"cadence_moy": 78.5}) == 78.5
    assert main.cadence_moyenne_sortie('{"cadence_moy": 0}') is None
    assert main.sauts_pour_10_km(None, 20) == 0.0
    d = {"sauts": {"jumps": {"nombre": 6}}}
    assert abs(main.sauts_pour_10_km(d, 30) - 2.0) < 1e-9
    assert main.sauts_pour_10_km(d, 0) == 0.0


def test_nouvelle_famille_roues():
    for nom in ("Rayons et voile des roues (contrôle)", "Roulements de moyeux", "Roulements de direction", "Jante arrière"):
        assert main.famille_de(nom) == "roues", nom
    assert main.famille_de("Pneu arrière") == "pneu"
    route = main.coef_terrain_famille("roues", {"route": 1.0}, False, "route", REGLAGES)
    sentier = main.coef_terrain_famille("roues", {"sentier": 1.0}, False, "sentier", REGLAGES)
    assert abs(route - 0.7) < 1e-9 and sentier > route


def test_lecture_meteo_pluie_et_temperature():
    import urllib.request as ur

    class Rep:
        def __init__(self, t): self.t = t
        def read(self): return self.t.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    heures = ["2026-10-05T%02d:00" % h for h in range(7, 11)]
    corps = json.dumps({"hourly": {"time": heures, "precipitation": [0.0, 2.0, 4.0, 1.0], "temperature_2m": [8.0, 10.0, 14.0, 16.0]}})
    ancien = ur.urlopen
    ur.urlopen = lambda demande, timeout=0: Rep(corps)
    try:
        meteo = main.lire_meteo_open_meteo(44.0, 2.0, "2026-10-05T08:30:00Z", 3600)
    finally:
        ur.urlopen = ancien
    assert abs(meteo["pluie_mm"] - 3.0) < 1e-9
    assert abs(meteo["temperature_c"] - 12.0) < 1e-9     # moyenne de 10 °C et 14 °C, une demi-heure chacun
