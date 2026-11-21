"""Tests de la connexion Bosch (aucun appel réel : les réponses de Bosch sont simulées)."""
import os
import sys
import urllib.parse
from pathlib import Path

os.environ["CODE_ACCES"] = "code-de-test"
os.environ["SQLITE_PATH"] = "/tmp/test_bosch.db"
if os.path.exists("/tmp/test_bosch.db"):
    os.remove("/tmp/test_bosch.db")
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient

ENTETE = {"x-code-acces": "code-de-test"}


def client():
    return TestClient(main.app, base_url="https://maillons-test-2026.onrender.com", follow_redirects=False)


def demarrer():
    with client() as c:
        return c


def test_connexion_complete_et_lecture():
    with TestClient(main.app, base_url="https://maillons-test-2026.onrender.com", follow_redirects=False) as c:
        # 1. Sans code d'accès : refusé
        assert c.post("/api/bosch/connecter").status_code == 401

        # 2. L'adresse envoyée à Bosch contient tout ce qu'il faut
        r = c.post("/api/bosch/connecter", headers=ENTETE)
        assert r.status_code == 200
        url = r.json()["url"]
        assert url.startswith("https://p9.authz.bosch.com/auth/realms/obc/protocol/openid-connect/auth?")
        p = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert p["client_id"] == main.BOSCH_CLIENT_ID
        assert p["redirect_uri"] == "https://maillons-test-2026.onrender.com/api/bosch/callback"
        assert p["code_challenge_method"] == "S256"
        assert p["response_type"] == "code"
        assert len(p["code_challenge"]) == 43

        # 3. Un état inconnu est refusé
        r = c.get("/api/bosch/callback?code=abc&state=inconnu")
        assert r.status_code == 303 and "bosch=expire" in r.headers["location"]

        # 4. Bosch renvoie la personne avec un bon état : l'échange du code est simulé
        demandes = []

        def faux_jeton(champs):
            demandes.append(champs)
            return {"access_token": "ACCES1", "refresh_token": "REFRESH1", "expires_in": 3600}

        main.bosch_demander_jeton = faux_jeton
        r = c.get("/api/bosch/callback", params={"code": "CODE", "state": p["state"]})
        assert r.status_code == 303 and "bosch=ok" in r.headers["location"]
        assert demandes[0]["grant_type"] == "authorization_code"
        assert demandes[0]["code_verifier"]
        assert demandes[0]["redirect_uri"] == p["redirect_uri"]
        assert "client_secret" not in demandes[0]

        # 5. L'état ne sert qu'une fois
        r = c.get("/api/bosch/callback", params={"code": "CODE", "state": p["state"]})
        assert "bosch=expire" in r.headers["location"]

        # 6. Connecté
        assert c.get("/api/bosch/etat", headers=ENTETE).json()["connecte"] is True

        # 7. Lecture des vélos (réponse de Bosch simulée)
        class FausseReponse:
            def __init__(self, texte):
                self.texte = texte

            def read(self):
                return self.texte.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        vus = []

        def faux_urlopen(demande, timeout=0):
            vus.append((demande.full_url, demande.get_header("Authorization")))
            return FausseReponse(
                '{"bikes":[{"id":"11111111-1111-1111-1111-111111111111","createdAt":"2025-01-01T00:00:00Z",'
                '"driveUnit":{"odometer":12345678,"name":"CX"}}]}'
            )

        vrai = main.urllib.request.urlopen
        main.urllib.request.urlopen = faux_urlopen
        try:
            r = c.get("/api/bosch/velos", headers=ENTETE)
        finally:
            main.urllib.request.urlopen = vrai
        assert r.status_code == 200
        d = r.json()
        assert d["nb"] == 1
        assert d["odometres"] == [{"chemin": "[0].driveUnit.odometer", "valeur": 12345678}]
        assert vus[0][0] == "https://api.bosch-ebike.com/bike-profile/smart-system/v1/bikes"
        assert vus[0][1] == "Bearer ACCES1"

        # 8. Autorisation expirée : elle est renouvelée avec le refresh token
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("UPDATE bosch_jetons SET expire_le = 1")
        conn.commit()
        conn.close()
        demandes.clear()
        main.bosch_demander_jeton = lambda champs: (demandes.append(champs) or
                                                    {"access_token": "ACCES2", "expires_in": 3600})
        assert main.bosch_jeton_valide(1) == "ACCES2"
        assert demandes[0]["grant_type"] == "refresh_token"
        assert demandes[0]["refresh_token"] == "REFRESH1"
        # le refresh token est conservé si Bosch n'en renvoie pas de nouveau
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("SELECT refresh_token FROM bosch_jetons WHERE personne_id = 1")
        assert cur.fetchone()[0] == "REFRESH1"
        conn.close()

        # 9. Déconnexion
        assert c.delete("/api/bosch/connexion", headers=ENTETE).status_code == 200
        assert c.get("/api/bosch/etat", headers=ENTETE).json()["connecte"] is False
        assert c.get("/api/bosch/velos", headers=ENTETE).status_code == 409


def test_refus_de_bosch():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        r = c.get("/api/bosch/callback?error=access_denied")
        assert r.status_code == 303 and "bosch=refus" in r.headers["location"]


def test_liaison_et_synchro_du_kilometrage():
    with TestClient(main.app, base_url="https://x.onrender.com", follow_redirects=False) as c:
        BIKE = "0ede75c1-3f16-46a0-9c18-f16afd601b50"
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("DELETE FROM bosch_jetons")
        cur.execute(
            "INSERT INTO bosch_jetons (personne_id, access_token, refresh_token, expire_le, mis_a_jour_le) VALUES (1, 'A', 'R', 9999999999, 'x')"
        )
        conn.commit()
        conn.close()

        odometre = {"valeur": 3096890}
        main.bosch_lire_velos = lambda acces: (None, [{"id": BIKE, "driveUnit": {"odometer": odometre["valeur"]}}])

        def km():
            conn = main.db.connect()
            cur = conn.cursor()
            cur.execute("SELECT km_total, bosch_odometre_m FROM velos WHERE id = 1")
            ligne = cur.fetchone()
            conn.close()
            return ligne[0], ligne[1]

        # Sans liaison : rien ne bouge
        avant = km()[0]
        assert c.post("/api/bosch/synchro", headers=ENTETE).json() == {"liees": 0, "modifies": []}
        assert km()[0] == avant

        # Un vélo qui n'existe pas ou n'est pas à soi est refusé
        assert c.post("/api/bosch/lier", headers=ENTETE, json={"bosch_id": BIKE, "velo_id": 9999}).status_code in (403, 404)

        # Liaison puis première synchro : le compteur prend la valeur de Bosch (en km)
        assert c.post("/api/bosch/lier", headers=ENTETE, json={"bosch_id": BIKE, "velo_id": 1}).status_code == 200
        assert c.get("/api/bosch/etat", headers=ENTETE).json()["liaisons"] == {BIKE: 1}
        r = c.post("/api/bosch/synchro", headers=ENTETE).json()
        assert r["modifies"] == [{"velo_id": 1, "km_total": 3097}]
        assert km() == (3097, 3096890)

        # Même odomètre : les km des sorties importées entre-temps ne sont pas écrasés
        conn = main.db.connect()
        cur = conn.cursor()
        cur.execute("UPDATE velos SET km_total = 3110 WHERE id = 1")
        conn.commit()
        conn.close()
        assert c.post("/api/bosch/synchro", headers=ENTETE).json()["modifies"] == []
        assert km()[0] == 3110

        # Bosch annonce plus : le compteur est recalé sur Bosch
        odometre["valeur"] = 3200400
        assert c.post("/api/bosch/synchro", headers=ENTETE).json()["modifies"] == [{"velo_id": 1, "km_total": 3200}]
        assert km() == (3200, 3200400)

        # Déliaison
        assert c.delete("/api/bosch/lier/1", headers=ENTETE).status_code == 200
        assert c.get("/api/bosch/etat", headers=ENTETE).json()["liaisons"] == {}
