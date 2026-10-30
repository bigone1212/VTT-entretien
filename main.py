from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import base64
import db
import gpx_import
import hmac
import ipaddress
import json
import html as html_lib
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, List, Dict
from datetime import date, datetime, timedelta, timezone

app = FastAPI(title="VTTAE Entretien")

# Code d'accès de l'application.
# Il n'est PAS écrit ici : ce fichier est public sur GitHub.
# Il se règle dans Render : Environment > variable CODE_ACCES.
CODE_ACCES = os.environ.get("CODE_ACCES", "").strip()

MAX_PHOTOS_PAR_SORTIE = 8
MAX_CHARS_PHOTO = 700_000
MAX_CHARS_MINIATURE = 60_000
PREFIXE_JPEG = "data:image/jpeg;base64,"

# ============================================================
# BASE DE DONNÉES
# ============================================================

REGLAGES_PAR_DEFAUT = {
    "terrain_route": 1.0,
    "terrain_mixte": 1.15,
    "terrain_chemin": 1.3,
    "terrain_sentier": 1.6,
    "poids_denivele": 1.0,
}


def verifier_auth(x_code_acces=None):
    """Vérifie le code d'accès envoyé par l'en-tête HTTP."""
    if not CODE_ACCES:
        raise HTTPException(
            status_code=503,
            detail="Code d'accès non réglé sur le serveur."
        )

    envoye = (x_code_acces or "").encode("utf-8")

    if not hmac.compare_digest(envoye, CODE_ACCES.encode("utf-8")):
        time.sleep(0.5)
        raise HTTPException(status_code=401, detail="Code d'accès incorrect.")


def verifier_photo(texte, max_chars):
    """Vérifie qu'on reçoit bien une photo JPEG de taille raisonnable."""
    if not texte.startswith(PREFIXE_JPEG) or len(texte) > max_chars:
        raise HTTPException(
            status_code=400,
            detail="Photo refusée (JPEG de taille raisonnable attendu)."
        )

    try:
        octets = base64.b64decode(texte[len(PREFIXE_JPEG):], validate=True)
    except ValueError:
        raise HTTPException(status_code=400, detail="Photo illisible.")

    if not octets.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=400, detail="Photo illisible.")


def miniature_de(photo):
    """Miniature d'une photo stockée (ancien format : texte seul)."""
    if isinstance(photo, dict):
        return photo.get("mini") or ""

    return photo if isinstance(photo, str) else ""


def photo_complete_de(photo):
    if isinstance(photo, dict):
        return photo.get("photo") or ""

    return photo if isinstance(photo, str) else ""


def lire_reglages(cursor):
    """Réglages d'usure : valeurs enregistrées, sinon valeurs par défaut."""
    reglages = dict(REGLAGES_PAR_DEFAUT)

    cursor.execute("SELECT cle, valeur FROM reglages")

    for ligne in cursor.fetchall():
        if ligne[0] in reglages and ligne[1] is not None:
            reglages[ligne[0]] = float(ligne[1])

    return reglages


def famille_de(nom):
    """Famille d'une pièce, déduite de son nom."""
    nom = (nom or "").lower()

    if "plaquette" in nom or "frein" in nom or "disque" in nom:
        return "freins"

    if "fourche" in nom or "amortisseur" in nom or "suspension" in nom:
        return "suspension"

    if "pneu" in nom:
        return "pneu"

    if any(m in nom for m in (
        "chaîne", "chaine", "cassette", "plateau", "galet",
        "dérailleur", "derailleur", "transmission"
    )):
        return "transmission"

    # Entretiens liés au temps ou sans lien direct avec le terrain.
    return "autre"


def coef_terrain_sortie(trace_json, surfaces, terrain, reglages):
    """
    Coefficient de terrain d'une sortie : (coefficient, analysé ?).
    Si le type de chemin a été analysé, on pondère les coefficients route /
    chemin / sentier par la part réelle de chaque type ; les parties inconnues
    gardent le coefficient du terrain choisi pour la sortie.
    """
    base = reglages.get(f"terrain_{terrain}", 1.0)

    if not surfaces:
        return base, False

    try:
        trace = json.loads(trace_json or "[]")
    except ValueError:
        return base, False

    if not isinstance(trace, list) or len(trace) != len(surfaces) or len(trace) < 2:
        return base, False

    route = reglages.get("terrain_route", 1.0)
    coefs = {
        "r": route,
        "v": route,
        "c": reglages.get("terrain_chemin", 1.0),
        "s": reglages.get("terrain_sentier", 1.0),
    }
    total = 0.0
    somme = 0.0

    for i in range(len(trace) - 1):
        d = gpx_import._distance_m(trace[i], trace[i + 1])
        total += d
        somme += d * coefs.get(surfaces[i], base)

    if total <= 0:
        return base, False

    return somme / total, True


def facteur_usure(famille, distance_km, d_pos, d_neg, terrain, reglages, coef=None):
    """
    Combien de km d'usure équivaut 1 km de cette sortie pour cette famille.
    1,0 = usure normale ; 2,0 = deux fois plus vite.
    `coef` : coefficient de terrain déjà calculé (analyse du type de chemin).
    """
    coef_terrain = coef if coef is not None else reglages.get(f"terrain_{terrain}", 1.0)
    distance = max(float(distance_km or 0), 0.1)
    poids = reglages.get("poids_denivele", 1.0)

    if famille == "transmission":
        montee = (d_pos or 0) / distance
        return coef_terrain * (1 + poids * montee / 100)

    if famille == "freins":
        descente = (d_neg or 0) / distance
        return coef_terrain * (1 + poids * descente / 100)

    if famille == "autre":
        return 1.0

    return coef_terrain


def maintenant_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ajouter_colonne(cursor, table, colonne, definition):
    """Ajoute une colonne à une table existante, si elle n'existe pas encore."""
    if db.USE_PG:
        cursor.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {colonne} {definition}"
        )
        return

    cursor.execute(f"PRAGMA table_info({table})")
    existantes = [row[1] for row in cursor.fetchall()]

    if colonne not in existantes:
        cursor.execute(
            f"ALTER TABLE {table} ADD COLUMN {colonne} {definition}"
        )


DATE_ACHAT_PAR_DEFAUT = "2025-11-18"   # livraison du vélo (facture)

# Entretiens ajoutés pour le Cube Stereo Hybrid ONE44 EXC 800 (2025).
# (nom, seuil en km (0 = aucun), seuil en jours (0 = aucun), catégorie, x %, y %)
COMPOSANTS_V2 = [
    ("Disque de frein avant (203 mm)", 4000, 0, "piece", 79, 58),
    ("Disque de frein arrière (203 mm)", 4000, 0, "piece", 16, 67),
    ("Purge du liquide de frein (avant + arrière)", 0, 365, "maintenance", 57, 15),
    ("Plateau (pédalier ACID 34/36 dents)", 5000, 0, "piece", 43, 66),
    ("Galets de dérailleur arrière", 4000, 0, "piece", 21, 73),
    ("Câble et gaine de dérailleur", 0, 365, "maintenance", 30, 66),
    ("Pédales (graissage et jeu)", 0, 365, "maintenance", 45, 75),
    ("Pneu avant / tubeless", 3500, 0, "piece", 93, 63),
    ("Liquide préventif tubeless (2 pneus)", 0, 120, "maintenance", 87, 82),
    ("Roulements de moyeux (avant + arrière)", 5000, 365, "maintenance", 76, 68),
    ("Rayons et voile des roues (contrôle)", 0, 180, "maintenance", 84, 48),
    ("Amortisseur Fox - Entretien 50h", 800, 0, "maintenance", 44, 44),
    ("Amortisseur Fox - Révision complète 200h", 3200, 0, "maintenance", 48, 50),
    ("Roulements de suspension (biellettes)", 3000, 365, "maintenance", 35, 56),
    ("Jeu de direction (contrôle et graissage)", 5000, 365, "maintenance", 65, 20),
    ("Tige de selle télescopique (nettoyage et graissage)", 1600, 0, "maintenance", 33, 20),
    ("Contrôle des serrages (potence, cintre, roues, pédalier)", 0, 180, "maintenance", 63, 12),
    ("Contrôle annuel Bosch (moteur + logiciel)", 0, 365, "maintenance", 37, 70),
    ("Batterie PowerTube 800 (contacts et état)", 0, 180, "maintenance", 55, 45),
]


# Contrôles réguliers (pression, réglages) à faire souvent.
COMPOSANTS_V3 = [
    ("Pneus - pression et état (contrôle)", 0, 14, "maintenance", 92, 74),
    ("Fourche - pression et SAG (contrôle)", 0, 30, "maintenance", 80, 38),
    ("Amortisseur - pression et SAG (contrôle)", 0, 30, "maintenance", 41, 50),
]


def migrer_composants_v2(cursor):
    ajouter_composants_une_fois(cursor, "migration_composants_v2", COMPOSANTS_V2)
    ajouter_composants_une_fois(cursor, "migration_composants_v3", COMPOSANTS_V3)
    marquer_verifications_une_fois(cursor)


# Contrôles réguliers : affichés en pastilles sous la photo, pas dans la liste.
NOMS_VERIFICATIONS = (
    "Pneus - pression et état (contrôle)",
    "Fourche - pression et SAG (contrôle)",
    "Amortisseur - pression et SAG (contrôle)",
    "Rayons et voile des roues (contrôle)",
    "Contrôle des serrages (potence, cintre, roues, pédalier)",
    "Contrôle annuel Bosch (moteur + logiciel)",
    "Batterie PowerTube 800 (contacts et état)",
)


def marquer_verifications_une_fois(cursor):
    cursor.execute(
        "SELECT valeur FROM reglages WHERE cle = ?",
        ("migration_verifications_v1",)
    )

    if cursor.fetchone() is not None:
        return

    for nom in NOMS_VERIFICATIONS:
        cursor.execute(
            "UPDATE composants SET verification = 1 WHERE nom = ?",
            (nom,)
        )

    cursor.execute("""
        INSERT INTO reglages (cle, valeur)
        VALUES (?, 1)
        ON CONFLICT (cle) DO NOTHING
    """, ("migration_verifications_v1",))


def ajouter_composants_une_fois(cursor, marqueur, liste):
    """Ajoute une seule fois de nouveaux entretiens (sans toucher à l'existant)."""
    cursor.execute(
        "SELECT valeur FROM reglages WHERE cle = ?",
        (marqueur,)
    )

    if cursor.fetchone() is not None:
        return

    cursor.execute("SELECT nom FROM composants")
    existants = {r[0] for r in cursor.fetchall()}

    for nom, km, jours, categorie, x, y in liste:
        if nom in existants:
            continue

        cursor.execute("""
            INSERT INTO composants (
                nom, km_depart, seuil_alerte, seuil_jours, categorie, pos_x, pos_y
            )
            VALUES (?, 0, ?, ?, ?, ?, ?)
        """, (nom, km, jours, categorie, x, y))

    cursor.execute("""
        INSERT INTO reglages (cle, valeur)
        VALUES (?, 1)
        ON CONFLICT (cle) DO NOTHING
    """, (marqueur,))


# ------------------------------------------------------------
# Entretiens et contrôles conseillés pour les vélos autres que le CUBE,
# selon le type de vélo. Les noms sont génériques (sans marque) : les fiches
# « Comment faire » de la page sont rangées par nom exact de pièce.
# ------------------------------------------------------------
TYPES_ELECTRIQUES = ("vtt_electrique", "gravel_electrique", "route_electrique")
TYPES_SUSPENDUS = ("vtt_electrique", "vtt", "enduro", "descente")
TYPES_TUBELESS = (
    "vtt_electrique", "vtt", "enduro", "descente", "gravel", "gravel_electrique"
)

# Kilomètres conseillés par type :
# (chaîne, cassette, plaquettes, disques, pneu avant, pneu arrière,
#  nettoyage de la chaîne, entretien fourche et amortisseur)
KM_PAR_TYPE = {
    "vtt_electrique": (2000, 4000, 1500, 4000, 3500, 2500, 300, 800),
    "vtt": (2500, 5000, 2000, 4000, 3500, 2500, 300, 800),
    "enduro": (1500, 3000, 1000, 3000, 2500, 1500, 200, 600),
    "descente": (1000, 2000, 600, 2500, 1500, 1000, 150, 400),
    "gravel": (3000, 6000, 2000, 5000, 4000, 3000, 300, 0),
    "gravel_electrique": (2500, 5000, 1500, 4000, 4000, 3000, 300, 0),
    "route": (4000, 8000, 3000, 6000, 5000, 4000, 400, 0),
    "route_electrique": (3000, 6000, 2000, 5000, 5000, 4000, 300, 0),
}


def entretiens_du_type(type_velo, freins=None, suspension=None, tubeless=None, transmission=None):
    """Entretiens adaptés aux caractéristiques validées du vélo."""
    km = KM_PAR_TYPE.get(type_velo) or KM_PAR_TYPE["vtt_electrique"]
    chaine, cassette, plaquettes, disques, pneu_av, pneu_ar, nettoyage, susp = km
    piece, maintenance = "piece", "maintenance"

    liste = [
        ("Chaîne (remplacement)", chaine, 0, piece, 27, 76, 0),
        ("Nettoyage & Lubrification chaîne", nettoyage, 0, maintenance, 35, 76, 0),
        ("Cassette (remplacement)", cassette, 0, piece, 20, 63, 0),
        ("Plateau (remplacement)", cassette + cassette // 4, 0, piece, 43, 66, 0),
        ("Galets de dérailleur", cassette, 0, piece, 21, 73, 0),
        ("Câble et gaine de dérailleur", 0, 365, maintenance, 30, 66, 0),
        ("Plaquettes de frein (avant)", plaquettes, 0, piece, 70, 62, 0),
        ("Plaquettes de frein (arrière)", plaquettes, 0, piece, 22, 47, 0),
        ("Disque de frein (avant)", disques, 0, piece, 79, 58, 0),
        ("Disque de frein (arrière)", disques, 0, piece, 16, 67, 0),
        ("Purge du liquide de frein", 0, 180 if type_velo in ("enduro", "descente") else 365, maintenance, 57, 15, 0),
        ("Pneu (avant)", pneu_av, 0, piece, 93, 63, 0),
        ("Pneu (arrière)", pneu_ar, 0, piece, 7, 58, 0),
        ("Pédales (graissage et jeu)", 0, 365, maintenance, 45, 75, 0),
        ("Roulements de moyeux (avant + arrière)", 5000, 365, maintenance, 76, 68, 0),
        ("Jeu de direction (contrôle et graissage)", 5000, 365, maintenance, 65, 20, 0),
    ]

    if freins == "patins":
        liste = [ligne for ligne in liste if not any(mot in ligne[0].lower() for mot in ("plaquettes de frein", "disque de frein", "purge du liquide de frein"))]
        liste.extend([
            ("Patins de frein (avant)", plaquettes, 0, piece, 70, 62, 0),
            ("Patins de frein (arrière)", plaquettes, 0, piece, 22, 47, 0),
        ])
    elif freins in ("disque", "disque_hydraulique", "disque_mecanique"):
        if freins != "disque_hydraulique":
            liste = [ligne for ligne in liste if "purge du liquide de frein" not in ligne[0].lower()]
    else:
        liste = [ligne for ligne in liste if not any(mot in ligne[0].lower() for mot in ("plaquettes de frein", "disque de frein", "purge du liquide de frein", "patins de frein"))]
        liste.append(("Freins - contrôle général (type à confirmer)", 0, 30, maintenance, 57, 15, 1))

    if transmission == "courroie":
        termes_transmission = ("chaîne", "lubrification chaîne", "cassette", "plateau", "galets", "câble et gaine de dérailleur")
        liste = [ligne for ligne in liste if not any(terme in ligne[0].lower() for terme in termes_transmission)]
        liste.append(("Courroie (contrôle et remplacement)", 10000, 365, piece, 27, 76, 0))
    elif transmission != "chaine":
        termes_transmission = ("chaîne", "lubrification chaîne", "cassette", "plateau", "galets", "câble et gaine de dérailleur")
        liste = [ligne for ligne in liste if not any(terme in ligne[0].lower() for terme in termes_transmission)]

    if tubeless == "chambre":
        liste = [ligne for ligne in liste if "liquide préventif tubeless" not in ligne[0].lower()]
    elif tubeless == "tubeless":
        if not any("liquide préventif tubeless" in ligne[0].lower() for ligne in liste):
            liste.append(("Liquide préventif tubeless (2 pneus)", 0, 120, maintenance, 87, 82, 0))
    type_suspension = suspension if suspension in ("rigide", "avant", "double") else None
    liste = [ligne for ligne in liste if not any(mot in ligne[0].lower() for mot in ("fourche -", "amortisseur -", "roulements de suspension"))]
    if type_suspension in ("avant", "double"):
        seuil_susp = susp or 800
        liste.extend([
            ("Fourche - entretien (50 h)", seuil_susp, 0, maintenance, 75, 44, 0),
            ("Fourche - révision complète (200 h)", seuil_susp * 4, 0, maintenance, 71, 34, 0),
            ("Fourche - pression et SAG (contrôle)", 0, 30, maintenance, 80, 38, 1),
        ])
    if type_suspension == "double":
        seuil_susp = susp or 800
        liste.extend([
            ("Amortisseur - entretien (50 h)", seuil_susp, 0, maintenance, 44, 44, 0),
            ("Amortisseur - révision complète (200 h)", seuil_susp * 4, 0, maintenance, 48, 50, 0),
            ("Roulements de suspension (biellettes)", 3000, 365, maintenance, 35, 56, 0),
            ("Amortisseur - pression et SAG (contrôle)", 0, 30, maintenance, 41, 50, 1),
        ])

    # Contrôles réguliers, regroupés sous le bouton « Vérifications ».
    liste.extend([
        ("Pneus - pression et état (contrôle)", 0, 14, maintenance, 92, 74, 1),
        ("Rayons et voile des roues (contrôle)", 0, 180, maintenance, 84, 48, 1),
        ("Contrôle des serrages (vis et boulons)", 0, 180, maintenance, 63, 12, 1),
    ])

    if type_velo in TYPES_ELECTRIQUES:
        liste.extend([
            ("Contrôle annuel du moteur et du logiciel", 0, 365, maintenance, 37, 70, 1),
            ("Batterie (contacts et état)", 0, 180, maintenance, 55, 45, 1),
        ])

    return liste


# Anciens noms génériques donnés aux pièces des vélos ajoutés avant ce modèle :
# ancien nom -> (nouveau nom, vérification 0/1). Seuls le nom et le drapeau
# « vérification » changent : identifiant, historique, kilomètres et seuils restent.
ANCIENS_NOMS_GENERIQUES = {
    "Chaîne ou transmission": ("Chaîne (remplacement)", 0),
    "Frein avant": ("Plaquettes de frein (avant)", 0),
    "Frein arrière": ("Plaquettes de frein (arrière)", 0),
    "Pneus": ("Pneu (arrière)", 0),
    "Roues et rayons (contrôle)": ("Rayons et voile des roues (contrôle)", 1),
    "Direction et roulements (contrôle)": ("Jeu de direction (contrôle et graissage)", 0),
    "Contrôle général et serrages": ("Contrôle des serrages (vis et boulons)", 1),
    "Moteur électrique (contrôle)": ("Contrôle annuel du moteur et du logiciel", 1),
    "Batterie (contrôle)": ("Batterie (contacts et état)", 1),
    "Fourche (entretien)": ("Fourche - entretien (50 h)", 0),
    "Suspension arrière (entretien)": ("Amortisseur - entretien (50 h)", 0),
}


def marqueur_entretiens_velo(velo_id):
    return "migration_entretiens_type_v1_velo_" + str(int(velo_id))


def poser_marqueur_entretiens(cursor, velo_id):
    cursor.execute("""
        INSERT INTO reglages (cle, valeur)
        VALUES (?, 1)
        ON CONFLICT (cle) DO NOTHING
    """, (marqueur_entretiens_velo(velo_id),))


def mettre_a_niveau_un_velo(cursor, velo_id, type_velo):
    """Renomme les anciennes pièces génériques d'un vélo et ajoute celles du
    modèle qui manquent. Ne supprime rien et ne touche à aucun autre vélo."""
    cursor.execute("SELECT nom FROM composants WHERE velo_id = ?", (velo_id,))
    noms = {r[0] for r in cursor.fetchall()}

    for ancien, (nouveau, verification) in ANCIENS_NOMS_GENERIQUES.items():
        if ancien in noms and nouveau not in noms:
            cursor.execute("""
                UPDATE composants SET nom = ?, verification = ?
                WHERE velo_id = ? AND nom = ?
            """, (nouveau, verification, velo_id, ancien))
            noms.discard(ancien)
            noms.add(nouveau)

    for nom, km, jours, categorie, x, y, verification in entretiens_du_type(type_velo):
        if nom in noms:
            continue

        cursor.execute("""
            INSERT INTO composants (
                velo_id, nom, km_depart, seuil_alerte, seuil_jours,
                categorie, pos_x, pos_y, verification, coef_usure
            )
            VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, 1)
        """, (velo_id, nom, km, jours, categorie, x, y, verification))
        noms.add(nom)


def mettre_a_niveau_velos(cursor):
    """Une seule fois par vélo (marqueur dans reglages) ; le CUBE n'est jamais touché."""
    cursor.execute("""
        SELECT id, type FROM velos
        WHERE id <> 1 AND legacy_velo_id IS NULL
        ORDER BY id
    """)

    for velo_id, type_velo in cursor.fetchall():
        cursor.execute(
            "SELECT valeur FROM reglages WHERE cle = ?",
            (marqueur_entretiens_velo(velo_id),)
        )

        if cursor.fetchone() is not None:
            continue

        mettre_a_niveau_un_velo(cursor, velo_id, type_velo)
        poser_marqueur_entretiens(cursor, velo_id)


def init_db():
    conn = db.connect()
    cursor = conn.cursor()

    # Évite que le service de production et un aperçu Render exécutent
    # les migrations PostgreSQL en même temps sur la même base Neon.
    # Le verrou transactionnel est libéré au commit ou à la fermeture
    # de la connexion.
    if db.USE_PG:
        cursor.execute("SELECT pg_advisory_xact_lock(84720124)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS velo (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            km_total INTEGER DEFAULT 2700
        )
    """)

    cursor.execute("""
        INSERT INTO velo (id, km_total)
        VALUES (1, 2700)
        ON CONFLICT (id) DO NOTHING
    """)

    # Registre multi-vélos : ajout uniquement, sans remplacer la table historique
    # velo ni supprimer les données déjà présentes.
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS velos (
            id {db.AUTO_ID},
            nom TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'electrique',
            marque TEXT,
            modele TEXT,
            annee INTEGER,
            taille TEXT,
            actif INTEGER DEFAULT 1,
            km_total INTEGER DEFAULT 0,
            km_frac DOUBLE PRECISION DEFAULT 0,
            km_maj_le TEXT,
            date_achat TEXT,
            legacy_velo_id INTEGER UNIQUE
        )
    """)

    # Colonnes également ajoutées si le registre velos existait déjà
    # sur la branche de test avant cette correction.
    ajouter_colonne(cursor, "velos", "km_total", "INTEGER DEFAULT 0")
    ajouter_colonne(cursor, "velos", "km_frac", "DOUBLE PRECISION DEFAULT 0")
    ajouter_colonne(cursor, "velos", "km_maj_le", "TEXT")
    ajouter_colonne(cursor, "velos", "date_achat", "TEXT")
    ajouter_colonne(cursor, "velos", "moteur", "TEXT")
    ajouter_colonne(cursor, "velos", "batterie", "TEXT")
    ajouter_colonne(cursor, "velos", "photo", "TEXT")
    # Jour où le suivi du vélo démarre dans Maillons (point de départ du
    # compteur de jours des pièces). Vide pour le CUBE et les vélos déjà créés :
    # on retombe alors sur la date d'achat, comme avant.
    ajouter_colonne(cursor, "velos", "suivi_depuis", "TEXT")
    ajouter_colonne(cursor, "velos", "freins", "TEXT")
    ajouter_colonne(cursor, "velos", "suspension", "TEXT")
    ajouter_colonne(cursor, "velos", "tubeless", "TEXT")
    ajouter_colonne(cursor, "velos", "transmission", "TEXT")

    # Le CUBE reçoit TOUJOURS l'identifiant 1 : les pièces et les sorties
    # existantes sont rattachées au vélo n° 1 (velo_id par défaut).
    cursor.execute("""
        INSERT INTO velos (
            id, nom, type, marque, modele, annee, km_total,
            km_frac, km_maj_le, date_achat, legacy_velo_id
        )
        VALUES (1, 'Mon CUBE', 'electrique', 'Cube',
                'Stereo Hybrid ONE44 EXC 800 (Bosch)', 2025,
                2700, 0, NULL, NULL, 1)
        ON CONFLICT DO NOTHING
    """)

    # PostgreSQL : le numéro automatique ne sait pas qu'on a imposé l'id 1.
    # On avance la séquence pour que le prochain vélo ajouté ne reçoive pas
    # un numéro déjà pris. (SQLite le fait tout seul.)
    if db.USE_PG:
        cursor.execute("""
            SELECT setval(
                pg_get_serial_sequence('velos', 'id'),
                GREATEST((SELECT MAX(id) FROM velos), 1)
            )
        """)


    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS composants (
            id {db.AUTO_ID},
            velo_id INTEGER DEFAULT 1,
            nom TEXT NOT NULL,
            km_depart INTEGER DEFAULT 0,
            seuil_alerte INTEGER NOT NULL,
            categorie TEXT DEFAULT 'piece',
            pos_x INTEGER DEFAULT 50,
            pos_y INTEGER DEFAULT 50
        )
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS historique (
            id {db.AUTO_ID},
            composant_id INTEGER,
            date_remplacement TEXT,
            cout DOUBLE PRECISION DEFAULT 0,
            notes TEXT,
            km_intervention INTEGER,
            FOREIGN KEY (composant_id) REFERENCES composants (id)
        )
    """)

    if not db.USE_PG:
        cursor.execute("PRAGMA table_info(historique)")
        colonnes = [row[1] for row in cursor.fetchall()]

        if "km_intervention" not in colonnes:
            cursor.execute("""
                ALTER TABLE historique
                ADD COLUMN km_intervention INTEGER
            """)

    # Kilométrage de la pièce juste AVANT l'intervention : permet de
    # remettre la pièce exactement comme avant si on supprime la ligne.
    ajouter_colonne(cursor, "historique", "km_avant", "INTEGER")
    ajouter_colonne(cursor, "historique", "velo_id", "INTEGER DEFAULT 1")
    cursor.execute("""
        UPDATE historique
        SET velo_id = COALESCE(
            (SELECT velo_id FROM composants WHERE composants.id = historique.composant_id),
            1
        )
        WHERE velo_id IS NULL
    """)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS sorties (
            id {db.AUTO_ID},
            velo_id INTEGER DEFAULT 1,
            nom TEXT,
            debut TEXT NOT NULL UNIQUE,
            sport TEXT,
            distance_km DOUBLE PRECISION,
            denivele_pos INTEGER,
            denivele_neg INTEGER,
            duree_s INTEGER,
            vitesse_moy DOUBLE PRECISION,
            alt_max INTEGER,
            pente_moy_montee DOUBLE PRECISION,
            terrain TEXT DEFAULT 'mixte',
            notes TEXT,
            km_compte INTEGER DEFAULT 0,
            trace TEXT
        )
    """)

    ajouter_colonne(cursor, "sorties", "photos", "TEXT")
    ajouter_colonne(cursor, "sorties", "videos", "TEXT")
    ajouter_colonne(cursor, "sorties", "surfaces", "TEXT")
    # 1 = sortie reçue automatiquement, vélo à confirmer par Math.
    ajouter_colonne(cursor, "sorties", "a_verifier", "INTEGER DEFAULT 0")

    # Les liens de partage secrets ont été abandonnés : on nettoie leur table.
    cursor.execute("DROP TABLE IF EXISTS partages")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reglages (
            cle TEXT PRIMARY KEY,
            valeur DOUBLE PRECISION NOT NULL
        )
    """)

    for cle, valeur in REGLAGES_PAR_DEFAUT.items():
        cursor.execute("""
            INSERT INTO reglages (cle, valeur)
            VALUES (?, ?)
            ON CONFLICT (cle) DO NOTHING
        """, (cle, valeur))

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mes_reglages (
            cle TEXT PRIMARY KEY,
            valeur TEXT NOT NULL,
            maj_le TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mes_reglages_velo (
            velo_id INTEGER NOT NULL,
            cle TEXT NOT NULL,
            valeur TEXT NOT NULL,
            maj_le TEXT,
            PRIMARY KEY (velo_id, cle)
        )
    """)
    # Copie idempotente des anciens réglages personnels vers le CUBE.
    cursor.execute("""
        INSERT INTO mes_reglages_velo (velo_id, cle, valeur, maj_le)
        SELECT 1, cle, valeur, maj_le
        FROM mes_reglages
        WHERE TRUE
        ON CONFLICT (velo_id, cle) DO NOTHING
    """)

    # Colonnes ajoutées aussi aux bases existantes, sans perdre les données.
    ajouter_colonne(cursor, "composants", "velo_id", "INTEGER DEFAULT 1")
    ajouter_colonne(cursor, "sorties", "velo_id", "INTEGER DEFAULT 1")
    cursor.execute("UPDATE composants SET velo_id = 1 WHERE velo_id IS NULL")
    cursor.execute("UPDATE sorties SET velo_id = 1 WHERE velo_id IS NULL")

    ajouter_colonne(cursor, "composants", "seuil_jours", "INTEGER DEFAULT 0")
    ajouter_colonne(cursor, "composants", "verification", "INTEGER DEFAULT 0")
    ajouter_colonne(cursor, "composants", "coef_usure", "DOUBLE PRECISION DEFAULT 1")
    ajouter_colonne(cursor, "velo", "date_achat", "TEXT")
    cursor.execute(
        "UPDATE velo SET date_achat = ? WHERE id = 1 AND date_achat IS NULL",
        (DATE_ACHAT_PAR_DEFAUT,)
    )

    ajouter_colonne(cursor, "velo", "km_maj_le", "TEXT")
    ajouter_colonne(cursor, "velo", "km_frac", "DOUBLE PRECISION DEFAULT 0")

    cursor.execute(
        "UPDATE velo SET km_maj_le = ? WHERE id = 1 AND km_maj_le IS NULL",
        (maintenant_utc(),)
    )

    # Copie du kilométrage et des dates vers le registre, après migration des
    # colonnes. UNE SEULE FOIS : ensuite les kilomètres vivants sont écrits dans
    # `velos` (ancienne table `velo` figée, gardée en secours). Sans ce marqueur,
    # chaque redémarrage remettait le compteur du CUBE à l'ancienne valeur.
    cursor.execute(
        "SELECT valeur FROM reglages WHERE cle = ?",
        ("migration_velos_copie_v1",)
    )

    if cursor.fetchone() is None:
        cursor.execute("""
            UPDATE velos
            SET km_total = (SELECT km_total FROM velo WHERE id = 1),
                km_frac = (SELECT km_frac FROM velo WHERE id = 1),
                km_maj_le = (SELECT km_maj_le FROM velo WHERE id = 1),
                date_achat = (SELECT date_achat FROM velo WHERE id = 1)
            WHERE legacy_velo_id = 1
        """)
        cursor.execute("""
            INSERT INTO reglages (cle, valeur)
            VALUES (?, 1)
            ON CONFLICT (cle) DO NOTHING
        """, ("migration_velos_copie_v1",))

    cursor.execute("SELECT COUNT(*) FROM composants")

    if cursor.fetchone()[0] == 0:
        composants_initiaux = [
            (
                "Chaîne Shimano XT (Remplacement)",
                1236, 2000, "piece", 27, 76
            ),
            (
                "Nettoyage & Lubrification chaîne",
                2445, 300, "maintenance", 35, 76
            ),
            (
                "Plaquettes de frein avant (N04C)",
                0, 1500, "piece", 70, 62
            ),
            (
                "Plaquettes de frein arrière",
                0, 1500, "piece", 22, 47
            ),
            (
                "Fourche Fox 36 - Entretien 50h",
                0, 800, "maintenance", 75, 44
            ),
            (
                "Fourche Fox 36 - Révision lourde 200h",
                0, 3200, "maintenance", 71, 34
            ),
            (
                "Cassette Shimano XT 12-speed",
                0, 4000, "piece", 20, 63
            ),
            (
                "Pneu arrière / tubeless",
                0, 2500, "piece", 7, 58
            )
        ]

        cursor.executemany("""
            INSERT INTO composants (
                nom,
                km_depart,
                seuil_alerte,
                categorie,
                pos_x,
                pos_y
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, composants_initiaux)

        entretiens_connus = [
            ("Chaîne Shimano XT (Remplacement)",
             "2026-03-05", 1236, "Remplacement chaîne"),
            ("Nettoyage & Lubrification chaîne",
             "2026-10-01", 2445, "Nettoyage et lubrification"),
        ]

        for nom, jour, km, note in entretiens_connus:
            cursor.execute(
                "SELECT id FROM composants WHERE nom = ?", (nom,)
            )
            ligne = cursor.fetchone()

            if ligne is not None:
                cursor.execute("""
                    INSERT INTO historique (
                        composant_id,
                        date_remplacement,
                        cout,
                        notes,
                        km_intervention
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (ligne[0], jour, 0.0, note, km))

    migrer_composants_v2(cursor)
    mettre_a_niveau_velos(cursor)

    conn.commit()
    conn.close()


init_db()


# ============================================================
# MODÈLES DES REQUÊTES
# ============================================================

class SynchroRequest(BaseModel):
    km_total: int = Field(ge=0)


class RemplacementRequest(BaseModel):
    notes: Optional[str] = "Intervention standard"
    cout: Optional[float] = Field(default=0.0, ge=0)
    date_remplacement: Optional[str] = None


class ReglagesRequest(BaseModel):
    terrain_route: float = Field(ge=0.5, le=5)
    terrain_mixte: float = Field(ge=0.5, le=5)
    terrain_chemin: float = Field(ge=0.5, le=5)
    terrain_sentier: float = Field(ge=0.5, le=5)
    poids_denivele: float = Field(ge=0, le=5)


MES_REGLAGES_CLES = {
    "taille_cadre", "pneu_av", "pneu_ar",
    "fourche_psi", "fourche_sag", "fourche_detente", "fourche_bagues",
    "amorto_psi", "amorto_sag", "amorto_detente", "amorto_bagues",
    "notes",
}


class MesReglagesRequest(BaseModel):
    valeurs: Dict[str, str]


class SeuilsRequest(BaseModel):
    seuil_alerte: int = Field(ge=0, le=200000)
    seuil_jours: int = Field(ge=0, le=3650)


class CoefsPiecesRequest(BaseModel):
    # Clé : numéro de la pièce (en texte). Valeur : coefficient d'usure.
    valeurs: Dict[str, float]


class PositionRequest(BaseModel):
    pos_x: int = Field(ge=0, le=100)
    pos_y: int = Field(ge=0, le=100)


class PhotoIn(BaseModel):
    mini: str = Field(max_length=200_000)
    photo: str = Field(max_length=1_500_000)
    # Lieu et heure de prise de vue (lus dans la photo, facultatifs).
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    heure: Optional[str] = Field(default=None, max_length=30)


def photo_a_stocker(p):
    """Photo + lieu/heure de prise de vue, si on les connait."""
    stockee = {"mini": p.mini, "photo": p.photo}

    if p.lat is not None and p.lon is not None:
        stockee["lat"] = round(p.lat, 6)
        stockee["lon"] = round(p.lon, 6)

    if p.heure:
        stockee["heure"] = p.heure

    return stockee


def position_de(photo):
    """Lieu et heure d'une photo stockée (vide pour les anciennes photos)."""
    if not isinstance(photo, dict):
        return None

    position = {}

    for cle in ("lat", "lon", "heure"):
        if photo.get(cle) is not None:
            position[cle] = photo[cle]

    return position or None


class TerrainRequest(BaseModel):
    terrain: str = Field(max_length=20)


MAX_VIDEOS_PAR_SORTIE = 10


class VideoRequest(BaseModel):
    url: str = Field(max_length=500)
    titre: Optional[str] = Field(default="", max_length=80)


class AjoutPhotosRequest(BaseModel):
    photos: List[PhotoIn] = Field(max_length=8)


class ImportGpxRequest(BaseModel):
    # Un fichier GPX ou TCX (texte), ou un fichier FIT (binaire en base64).
    gpx: Optional[str] = Field(default=None, min_length=50, max_length=3_000_000)
    fit: Optional[str] = Field(default=None, min_length=20, max_length=8_500_000)
    terrain: Optional[str] = "mixte"
    notes: Optional[str] = None
    nom: Optional[str] = None
    photos: Optional[List[PhotoIn]] = None


class HistoriqueRequest(BaseModel):
    composant_id: int
    date_remplacement: str
    km_intervention: int = Field(ge=0)
    cout: Optional[float] = Field(default=0.0, ge=0)
    notes: Optional[str] = "Ancien entretien"


class HistoriqueModifRequest(BaseModel):
    date_remplacement: str
    km_intervention: int = Field(ge=0)
    cout: Optional[float] = Field(default=0.0, ge=0)
    notes: Optional[str] = "Ancien entretien"


class SupprimerPlusieursRequest(BaseModel):
    ids: List[int] = Field(min_length=1, max_length=500)


# ============================================================
# PAGE PRINCIPALE : HTML, CSS ET JAVASCRIPT
# ============================================================

@app.get("/", response_class=HTMLResponse)
def accueil():
    html_content = """
    <!DOCTYPE html>
    <html lang="fr">

    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">

        <title>Maillons - Entretien VTT</title>
        <meta name="theme-color" content="#0b171c">
        <script>
            (function () {
                try {
                    var t = localStorage.getItem('maillon_theme');
                    if (t === 'light' || t === 'dark') {
                        document.documentElement.setAttribute('data-theme', t);
                    }
                    var pal = localStorage.getItem('maillon_palette');
                    if (pal && pal !== 'turquoise' && /^[a-z]+$/.test(pal)) {
                        document.documentElement.setAttribute('data-palette', pal);
                    }
                } catch (e) {}
            })();
        </script>
        <meta name="apple-mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-title" content="Maillons">
        <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 96 96'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%2335e0c8'/><stop offset='1' stop-color='%230a8f9c'/></linearGradient></defs><rect width='96' height='96' rx='24' fill='url(%23g)'/><g fill='none' stroke-linecap='round'><rect x='14' y='34' width='42' height='22' rx='11' transform='rotate(-32 35 45)' stroke='%2306262b' stroke-width='9'/><rect x='40' y='42' width='42' height='22' rx='11' transform='rotate(-32 61 53)' stroke='%2306262b' stroke-width='9'/><rect x='14' y='34' width='42' height='22' rx='11' transform='rotate(-32 35 45)' stroke='white' stroke-width='5'/><rect x='40' y='42' width='42' height='22' rx='11' transform='rotate(-32 61 53)' stroke='white' stroke-width='5'/></g></svg>">

        <style>
            :root {
                --bg-color: #0b171c;
                --card-bg: #13252c;
                --text-color: #eef6f4;
                --text-muted: #8fa9b0;
                --ac1: #35e0c8;
                --ac2: #0a8f9c;
                --ac3: #1bb5ac;
                --ac1-hover: #6ff0dc;
                --ac2-hover: #0a7d8a;
                --ac-rgb: 53,224,200;
                --ac2-rgb: 10,143,156;
                --on-grad: #04231f;
                --accent: var(--ac1);
                --accent-hover: var(--ac1-hover);
                --on-accent: #04231f;
                --nav-h: 68px;
                --success: var(--ac1);
                --danger: #ef4444;
                --line: rgba(255,255,255,0.05);
                --line2: rgba(255,255,255,0.1);
                --line3: rgba(255,255,255,0.07);
                --fill: rgba(255,255,255,0.05);
                --fill2: rgba(255,255,255,0.1);
                --tick: rgba(255,255,255,0.22);
                --sh: 0 4px 12px rgba(0,0,0,0.3);
                --field: #0a1317;
                --field-line: #23393f;
                --inset: rgba(0,0,0,0.6);
                --nav-bg: rgba(14, 29, 35, 0.94);
                --bg-fade: rgba(11,23,28,0);
                --strong: #ffffff;
                color-scheme: dark;
                --warning: #f59e0b;
            }

            * {
                box-sizing: border-box;
            }

            body {
                font-family: ui-rounded, "SF Pro Rounded", "Segoe UI Variable",
                    "Segoe UI", system-ui, Roboto, Helvetica, Arial, sans-serif;
                background-color: var(--bg-color);
                color: var(--text-color);
                margin: 0;
                padding: 0 16px calc(var(--nav-h) + 28px + env(safe-area-inset-bottom));
                -webkit-font-smoothing: antialiased;
            }

            #authScreen {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: var(--bg-color);
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                z-index: 9999;
                padding: 20px;
            }

            .auth-card {
                background: var(--card-bg);
                padding: 24px;
                border-radius: 14px;
                box-shadow: var(--sh);
                border: 1px solid var(--line);
                width: 100%;
                max-width: 400px;
                text-align: center;
            }

            .auth-card h2 {
                margin-top: 0;
                font-size: 1.2rem;
                color: var(--strong);
            }

            .header {
                position: sticky;
                top: 0;
                z-index: 40;
                max-width: 600px;
                margin: 0 auto 14px auto;
                padding: 12px 0;
                display: flex;
                align-items: center;
                gap: 12px;
                background: linear-gradient(var(--bg-color) 75%, var(--bg-fade));
            }

            .logo {
                width: 40px;
                height: 40px;
                flex-shrink: 0;
                border-radius: 11px;
                box-shadow: 0 4px 14px rgba(var(--ac-rgb),0.25);
            }

            .header-titres {
                flex: 1;
                min-width: 0;
            }

            .header h1 {
                font-size: 1.3rem;
                font-weight: 800;
                letter-spacing: -0.03em;
                line-height: 1.1;
                margin: 0;
                color: var(--strong);
            }

            .header-velo {
                font-size: 0.78rem;
                color: var(--text-muted);
                margin: 2px 0 0 0;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            .select-velo-zone {
                position: relative;
                display: inline-block;
                max-width: 100%;
                margin-top: 6px;
            }

            .select-velo-zone::after {
                content: "";
                position: absolute;
                right: 14px;
                top: 50%;
                width: 7px;
                height: 7px;
                margin-top: -6px;
                border-right: 2px solid var(--accent);
                border-bottom: 2px solid var(--accent);
                transform: rotate(45deg);
                pointer-events: none;
            }

            #selectVelo {
                -webkit-appearance: none;
                appearance: none;
                display: block;
                width: auto;
                max-width: 100%;
                min-width: 0;
                margin: 0;
                padding: 6px 34px 6px 14px;
                font-family: inherit;
                font-size: 0.85rem;
                font-weight: 700;
                color: var(--accent);
                background: rgba(var(--ac-rgb),0.12);
                border: 1px solid rgba(var(--ac-rgb),0.3);
                border-radius: 999px;
                cursor: pointer;
                text-overflow: ellipsis;
                transition: border-color 0.15s, box-shadow 0.15s;
            }

            #selectVelo:focus {
                border-color: var(--ac2);
                box-shadow: 0 0 0 3px rgba(var(--ac-rgb),0.22);
            }

            #selectVelo option {
                background: var(--card-bg);
                color: var(--strong);
                font-weight: 600;
            }

            .km-pill {
                flex-shrink: 0;
                font-size: 0.85rem;
                font-weight: 700;
                color: var(--accent);
                background: rgba(var(--ac-rgb),0.12);
                border: 1px solid rgba(var(--ac-rgb),0.3);
                border-radius: 999px;
                padding: 6px 12px;
            }

            .sync-card {
                background: var(--card-bg);
                padding: 14px;
                border-radius: 14px;
                margin: 0 auto 20px auto;
                box-shadow: var(--sh);
                border: 1px solid var(--line);
                max-width: 600px;
            }

            .sync-card label {
                display: block;
                font-size: 0.75rem;
                color: var(--text-muted);
                margin-bottom: 6px;
            }

            .sync-row {
                display: flex;
                gap: 10px;
            }

            input[type="number"],
            input[type="text"],
            input[type="date"],
            input[type="password"],
            select {
                flex: 1;
                min-width: 0;
                background: var(--field);
                border: 1px solid var(--field-line);
                border-radius: 12px;
                padding: 10px;
                color: var(--strong);
                font-size: 1rem;
                outline: none;
                width: 100%;
                margin-bottom: 10px;
            }

            input:focus,
            select:focus {
                border-color: var(--accent);
            }

            button.btn-primary {
                background: var(--accent);
                color: var(--on-accent);
                border: none;
                border-radius: 12px;
                padding: 10px 16px;
                font-weight: 600;
                cursor: pointer;
            }

            button.btn-primary:hover {
                background: var(--accent-hover);
            }

            .bike-container {
                position: relative;
                width: 100%;
                max-width: 600px;
                aspect-ratio: 16 / 9;
                margin: 0 auto 20px auto;
                background: #ffffff;
                border-radius: 14px;
                overflow: hidden;
                border: 1px solid var(--line);
                box-shadow: var(--sh);
            }

            .bike-img {
                width: 100%;
                height: 100%;
                object-fit: contain;
                display: block;
            }

            .mes-reglages {
                max-width: 600px;
                margin: 0 auto 20px auto;
                background: var(--card-bg);
                border: 1px solid var(--line);
                border-radius: 14px;
                padding: 14px;
                box-shadow: var(--sh);
            }

            .mr-titre {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 8px;
                margin-bottom: 10px;
            }

            .mr-titre strong { font-size: 1rem; }

            .mr-titre button, .mr-actions button {
                background: transparent;
                color: var(--text-muted);
                border: 1px solid rgba(148,163,184,0.4);
                border-radius: 8px;
                padding: 6px 10px;
                font-size: 0.8rem;
                cursor: pointer;
            }

            .mr-actions { display: flex; gap: 8px; margin-top: 12px; }
            .mr-actions button { scroll-margin-bottom: 110px; }
            .mr-actions button.mr-ok {
                background: var(--accent);
                color: var(--on-accent);
                border-color: var(--accent);
                font-weight: 700;
            }

            .mr-groupe {
                padding: 10px 0;
                border-top: 1px solid var(--line3);
            }

            .mr-groupe h3 {
                margin: 0 0 6px;
                font-size: 0.9rem;
                color: var(--accent);
            }

            .mr-ligne {
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 10px;
                font-size: 0.88rem;
                padding: 3px 0;
            }

            .mr-ligne span { color: var(--text-muted); }
            .mr-ligne b { color: var(--strong); font-weight: 700; }
            .mr-ligne input[type="text"] { width: 120px; flex: none; margin: 0; padding: 8px 10px; text-align: right; }

            .placement-bar {
                max-width: 600px;
                margin: -8px auto 16px auto;
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                align-items: center;
                justify-content: center;
            }

            .placement-bar button, .card-tools button, .btn-voir-plus {
                background: transparent;
                color: var(--text-muted, #94a3b8);
                border: 1px solid rgba(148,163,184,0.4);
                border-radius: 8px;
                padding: 6px 10px;
                font-size: 0.8rem;
                cursor: pointer;
            }

            .card-tools {
                display: flex;
                gap: 8px;
                margin-top: 8px;
            }

            .btn-voir-plus {
                width: 100%;
                padding: 12px;
                margin: 4px 0 16px;
                font-size: 0.95rem;
            }

            .bike-container.placement { outline: 3px dashed var(--accent); cursor: crosshair; }
            .hotspot.choisi { outline: 3px solid #fff; transform: translate(-50%, -50%) scale(1.35); z-index: 5; }

            .hotspot {
                position: absolute;
                width: 24px;
                height: 24px;
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                border: 2px solid #fff;
                border-radius: 50%;
                transform: translate(-50%, -50%);
                cursor: pointer;
                box-shadow: 0 2px 6px rgba(0,0,0,0.4);
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 0.7rem;
                font-weight: 800;
                color: #fff;
                text-shadow: 0 1px 2px rgba(0,0,0,0.45);
                transition: transform 0.2s;
            }

            .hotspot:hover {
                transform: translate(-50%, -50%) scale(1.2);
            }

            .hotspot.warning {
                background: var(--danger);
                animation: pulse-red 1.5s infinite;
            }

            .hotspot.near-warning {
                background: var(--warning);
                animation: pulse-orange 1.5s infinite;
            }

            @keyframes pulse-red {
                0% {
                    box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7);
                }
                70% {
                    box-shadow: 0 0 0 10px rgba(239, 68, 68, 0);
                }
                100% {
                    box-shadow: 0 0 0 0 rgba(239, 68, 68, 0);
                }
            }

            @keyframes pulse-orange {
                0% {
                    box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.7);
                }
                70% {
                    box-shadow: 0 0 0 10px rgba(245, 158, 11, 0);
                }
                100% {
                    box-shadow: 0 0 0 0 rgba(245, 158, 11, 0);
                }
            }

            .grid {
                display: flex;
                flex-direction: column;
                gap: 12px;
                max-width: 600px;
                margin: 0 auto 20px auto;
            }

            .card {
                background: var(--card-bg);
                border-radius: 20px;
                padding: 16px;
                box-shadow: var(--sh);
                border: 1px solid var(--line);
            }

            .card-top {
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                gap: 10px;
                margin-bottom: 8px;
            }

            .component-name {
                font-weight: 600;
                font-size: 1rem;
                color: var(--strong);
            }

            .badge {
                flex-shrink: 0;
                font-size: 0.7rem;
                padding: 3px 8px;
                border-radius: 20px;
                font-weight: 600;
            }

            .badge.ok {
                background: rgba(var(--ac-rgb),0.15);
                color: var(--success);
            }

            .badge.near-warning {
                background: rgba(245, 158, 11, 0.15);
                color: var(--warning);
            }

            .badge.warning {
                background: rgba(239, 68, 68, 0.15);
                color: var(--danger);
            }

            .stats-row {
                display: flex;
                justify-content: space-between;
                gap: 10px;
                font-size: 0.8rem;
                color: var(--text-muted);
                margin-bottom: 6px;
            }

            .bar-bg {
                background: var(--field);
                border-radius: 6px;
                height: 8px;
                width: 100%;
                overflow: hidden;
                margin-bottom: 12px;
            }

            .bar-fill {
                height: 100%;
                background: var(--success);
                border-radius: 6px;
                transition: width 0.3s ease;
            }

            .bar-fill.near-warning {
                background: var(--warning);
            }

            .bar-fill.warning {
                background: var(--danger);
            }

            button.btn-action {
                background: var(--fill);
                color: var(--text-color);
                border: 1px solid var(--line2);
                width: 100%;
                padding: 8px;
                border-radius: 12px;
                font-size: 0.85rem;
                cursor: pointer;
            }

            button.btn-action:hover {
                background: var(--fill2);
            }

            .history-section {
                max-width: 600px;
                margin: 0 auto;
                background: var(--card-bg);
                border-radius: 14px;
                padding: 16px;
                border: 1px solid var(--line);
                box-shadow: var(--sh);
            }

            .history-section h2 {
                font-size: 1.1rem;
                margin-top: 0;
                color: var(--strong);
            }

            .history-item {
                position: relative;
                font-size: 0.85rem;
                padding: 10px 0;
                border-bottom: 1px solid var(--line);
                display: flex;
                justify-content: space-between;
                gap: 12px;
                align-items: flex-start;
            }

            .history-item:last-child {
                border-bottom: none;
            }

            .history-details {
                overflow-wrap: anywhere;
                flex: 1;
            }

            .history-cost {
                text-align: right;
                white-space: nowrap;
                font-weight: 600;
                color: var(--accent);
            }

            .loading {
                text-align: center;
                color: var(--text-muted);
                padding: 30px;
            }

            .form-help {
                color: var(--text-muted);
                font-size: 0.8rem;
                margin: -4px 0 10px;
            }

            input[type="file"] {
                width: 100%;
                margin-bottom: 10px;
                color: var(--text-muted);
            }

            .btn-survol {
                display: inline-flex;
                align-items: center;
                gap: 8px;
                margin-top: 10px;
                padding: 9px 16px;
                font: inherit;
                font-size: 0.9rem;
                font-weight: 800;
                letter-spacing: 0.01em;
                color: #ffffff;
                -webkit-text-stroke: 0.5px #000000;
                paint-order: stroke fill;
                text-shadow: 0 0 1px #000000, 0 1px 1px rgba(0,0,0,0.35);
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                border: none;
                border-radius: 999px;
                box-shadow: 0 6px 16px rgba(var(--ac2-rgb),0.35), inset 0 1px 0 rgba(255,255,255,0.35);
                cursor: pointer;
                transition: transform 0.15s, box-shadow 0.15s;
            }

            .btn-survol:hover { transform: translateY(-1px); box-shadow: 0 8px 20px rgba(var(--ac2-rgb),0.45), inset 0 1px 0 rgba(255,255,255,0.35); }
            .btn-survol:active { transform: translateY(1px); }
            .btn-survol:disabled { opacity: 0.45; cursor: default; box-shadow: none; transform: none; }
            .btn-survol .ico { width: 20px; height: 20px; stroke-width: 2.2; filter: drop-shadow(0 0 0.7px #000000) drop-shadow(0 0 0.7px #000000); }

            .sorties-filtres input[type="search"],
            .sorties-filtres select {
                border-color: rgba(var(--ac2-rgb),0.55);
                box-shadow: inset 0 0 0 1px rgba(var(--ac-rgb),0.08);
                transition: border-color 0.15s, box-shadow 0.15s;
            }

            .sorties-filtres input[type="search"]:focus,
            .sorties-filtres select:focus {
                border-color: var(--ac2);
                box-shadow: 0 0 0 3px rgba(var(--ac-rgb),0.22);
            }

            .btn-filtres-survol {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                padding: 10px 16px;
                font: inherit;
                font-weight: 800;
                color: #ffffff;
                -webkit-text-stroke: 0.35px #000000;
                paint-order: stroke fill;
                text-shadow: 0 0 1px #000000, 0 1px 1px rgba(0,0,0,0.35);
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                border: none;
                border-radius: 999px;
                box-shadow: 0 6px 16px rgba(var(--ac2-rgb),0.35), inset 0 1px 0 rgba(255,255,255,0.35);
                cursor: pointer;
                transition: transform 0.15s, box-shadow 0.15s;
            }

            .btn-filtres-survol:hover {
                transform: translateY(-1px);
                box-shadow: 0 8px 20px rgba(var(--ac2-rgb),0.45), inset 0 1px 0 rgba(255,255,255,0.35);
            }

            .btn-filtres-survol:active { transform: translateY(1px); }

            .mode-switch {
                display: flex;
                gap: 4px;
                padding: 4px;
                max-width: 600px;
                margin: 0 auto 12px auto;
                border-radius: 999px;
                background: var(--fill);
                border: 1px solid var(--line2);
            }

            .mode-switch button {
                flex: 1;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 8px;
                padding: 9px 10px;
                font: inherit;
                font-size: 0.88rem;
                font-weight: 700;
                color: var(--text-muted);
                background: transparent;
                border: none;
                border-radius: 999px;
                cursor: pointer;
                transition: background 0.2s, color 0.2s;
            }

            .mode-switch button .ico { width: 17px; height: 17px; }
            .mode-switch button small { font-size: 0.75rem; font-weight: 700; opacity: 0.85; }

            /* Pastille posée à cheval sur le bord haut droit du bouton. */
            .mode-switch button { position: relative; }

            .mode-pastille {
                position: absolute;
                top: -7px;
                right: 14px;
                width: 15px;
                height: 15px;
                border-radius: 50%;
                border: 2px solid rgba(255,255,255,0.9);
                pointer-events: none;
            }

            .mode-pastille.warning {
                background: var(--danger);
                animation: pulse-red 1.5s infinite;
            }

            .mode-pastille.near-warning {
                background: #fcd34d;
                animation: pulse-orange 1.5s infinite;
            }

            /* ---- Blocs repliables de l'onglet Réglages ---- */
            .mes-reglages.mr-repliable { margin-bottom: 10px; padding: 6px 14px; }
            .apparence { margin-bottom: 10px; }
            .apparence.mr-repliable { padding: 6px 14px; }
            .apparence.mr-repliable .mr-entete strong { margin-bottom: 0; }

            .mr-repliable > .mr-titre { margin-bottom: 0; }

            .mr-titre button.mr-entete,
            .mr-titre button.mr-entete:hover {
                flex: 1;
                justify-content: space-between;
                padding: 10px 0;
                font-size: 1rem;
                color: var(--text-color);
                background: transparent;
                border: none;
                border-radius: 0;
            }

            .mr-chevron { display: inline-flex; color: var(--text-muted); transition: transform 0.2s; }
            .mr-repliable.ouverte .mr-chevron { transform: rotate(180deg); }

            .mr-repliable .mr-corps { display: none; padding: 4px 0 10px; }
            .mr-repliable.ouverte .mr-corps { display: block; }
            .mr-repliable.ouverte > .mr-titre { border-bottom: 1px solid var(--line3); }

            .mr-corps .mr-ligne label { color: var(--text-muted); }
            .mr-corps .guide { margin: 0 0 8px; }
            .mr-corps .guide-coefs { margin: 14px 0 0; }
            .guide-coefs[open] > summary { color: var(--accent); }
            .mr-corps .mr-groupe:first-child { border-top: none; }

            .mode-switch button[aria-selected="true"] {
                color: #ffffff;
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                -webkit-text-stroke: 0.4px #000;
                paint-order: stroke fill;
                box-shadow: 0 4px 12px rgba(var(--ac2-rgb),0.3);
            }

            .bike-container[data-mode="entretien"] .hotspot.verif,
            .bike-container[data-mode="verifs"] .hotspot.entretien { display: none; }

            .chip .chip-num {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 18px;
                height: 18px;
                margin-right: 6px;
                font-size: 0.7rem;
                font-weight: 800;
                color: #fff;
                border-radius: 50%;
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
            }

            .menu-sortie-btn {
                position: absolute;
                top: 6px;
                right: 0;
                z-index: 5;
                width: 36px;
                height: 36px;
                padding: 0;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                color: var(--text-muted);
                background: transparent;
                border: none;
                border-radius: 50%;
                cursor: pointer;
            }

            .menu-sortie-btn:hover, .menu-sortie-btn[aria-expanded="true"] { background: var(--fill2); color: var(--strong); }

            /* Partage et favori : à gauche du menu trois points de chaque sortie */
            .menu-sortie-btn.bouton-partage { right: 38px; }
            .menu-sortie-btn.bouton-favori { right: 76px; }
            .menu-sortie-btn.occupe { opacity: 0.6; }
            .menu-sortie-btn.occupe svg { animation: v3d-tour 0.9s linear infinite; }
            .menu-sortie-btn.bouton-favori.actif { color: var(--danger); }
            .menu-sortie-btn.bouton-favori.actif svg { fill: currentColor; }
            .history-item.sortie-item .titre-sortie,
            .history-item.sortie-item .history-details > div:nth-child(2) { padding-right: 78px; }
            .menu-sortie button.hist-periode-btn.actif { border-color: var(--danger); }
            .menu-sortie button.hist-periode-btn.actif .ico { stroke: var(--danger); fill: var(--danger); }
            .menu-sortie-btn svg { width: 22px; height: 22px; fill: none; stroke: currentColor; }

            .menu-sortie {
                position: absolute;
                top: 44px;
                right: 0;
                z-index: 30;
                min-width: 210px;
                padding: 6px;
                display: none;
                flex-direction: column;
                gap: 2px;
                border-radius: 14px;
                background: var(--card-bg);
                border: 1px solid var(--line2);
                box-shadow: 0 10px 30px rgba(0,0,0,0.35);
            }

            .menu-sortie.ouvert { display: flex; }
            .menu-sortie.vers-haut { top: auto; bottom: 44px; }

            .menu-sortie button {
                display: flex;
                align-items: center;
                gap: 10px;
                width: 100%;
                margin: 0;
                padding: 11px 12px;
                font: inherit;
                font-size: 0.9rem;
                font-weight: 600;
                text-align: left;
                color: var(--text-color);
                background: transparent;
                border: none;
                border-radius: 9px;
                cursor: pointer;
            }

            .menu-sortie button:hover { background: var(--fill); }
            .menu-sortie button:disabled { opacity: 0.4; cursor: default; }
            .menu-sortie button.danger { color: var(--danger); }
            .menu-sortie button .ico { width: 18px; height: 18px; }

            .ligne-usure { margin-top: 4px; font-size: 0.78rem; font-weight: 600; color: var(--accent); }

            .sortie-statut { margin-top: 8px; font-weight: 700; color: var(--accent); }

            .history-details .titre-sortie { padding-right: 38px; }

            .hist-entete { position: relative; display: flex; align-items: center; min-height: 44px; padding-right: 84px; }
            .hist-entete h2 { margin: 0; }
            .photo-velo-bouton { border: 1px solid var(--accent); background: var(--card-bg); color: var(--accent); font-weight: 700; padding: 8px 16px; border-radius: 999px; cursor: pointer; font-size: 14px; }
            #blocAjouterVelo { position: relative; }
            #blocAjouterVelo .mr-titre { padding-right: 44px; min-height: 36px; }
            #blocAjouterVelo .menu-sortie-btn { top: 8px; right: 8px; }
            #blocAjouterVelo .menu-sortie { top: 48px; right: 8px; }
            .hist-total { margin: 2px 0 8px; font-size: 0.82rem; color: var(--text-muted); }
            .hist-filtre-btn { right: 40px; }
            .hist-filtre-btn.actif { color: var(--accent); background: rgba(var(--ac-rgb),0.16); }
            .hist-filtre-btn.actif:hover { background: rgba(var(--ac-rgb),0.24); }
            .hist-filtre-btn.actif::after {
                content: "";
                position: absolute;
                top: 1px;
                right: 1px;
                width: 11px;
                height: 11px;
                border-radius: 50%;
                background: var(--danger);
                border: 2px solid rgba(255,255,255,0.9);
                pointer-events: none;
            }
            .menu-filtre { min-width: 250px; max-width: calc(100vw - 56px); max-height: 60vh; overflow-y: auto; }
            .menu-filtre button { white-space: normal; }
            .menu-filtres-sorties { top: 44px; right: 40px; width: min(340px, calc(100vw - 56px)); max-width: calc(100vw - 56px); box-sizing: border-box; }
            .sorties-filtres-contenu { display: grid; grid-template-columns: 1fr; gap: 10px; }
            .sorties-filtres-contenu input[type="search"],
            .sorties-filtres-contenu input.champ-km,
            .sorties-filtres-contenu select { width: 100%; min-width: 0; margin: 0; box-sizing: border-box; }
            .sorties-filtres-contenu label { margin: 2px 0 -6px; font-size: 0.78rem; font-weight: 700; color: var(--text-muted); }
            .sorties-filtres-contenu .form-help { margin: 0; }

            .hist-selection {
                display: flex;
                flex-wrap: wrap;
                align-items: center;
                gap: 8px;
                margin: 6px 0 8px;
                padding: 10px 12px;
                border-radius: 12px;
                background: var(--fill);
                border: 1px solid var(--line2);
            }

            .hist-selection span { flex: 1 1 100%; font-weight: 700; font-size: 0.85rem; }

            .hist-selection button {
                margin: 0;
                padding: 8px 12px;
                font: inherit;
                font-size: 0.82rem;
                font-weight: 700;
                color: var(--text-color);
                background: var(--card-bg);
                border: 1px solid var(--line2);
                border-radius: 999px;
                cursor: pointer;
            }

            .hist-selection button.danger { color: var(--danger); }
            .hist-selection button:disabled { opacity: 0.4; cursor: default; }

            .history-item.avec-menu { padding-right: 40px; }
            .history-item.choisie { background: var(--fill); }
            .history-item.en-selection { cursor: pointer; padding-right: 4px; }
            .hist-case { flex: none; width: 22px; height: 22px; margin: 1px 0 0; accent-color: var(--accent); }

            .fi-boite { width: min(100%, 460px); }
            .fi-boite h2 { font-size: 1.1rem; }
            #histFermer { width: 38px; height: 38px; padding: 0; display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; border: 1px solid var(--line); background: var(--fill); color: var(--strong); cursor: pointer; }
            #histFermer .ico { width: 18px; height: 18px; }

            .sortie-media {
                display: flex;
                flex-direction: column;
                gap: 6px;
                align-items: center;
                flex-shrink: 0;
            }

            .photos-gallery {
                display: flex;
                gap: 4px;
                flex-wrap: wrap;
                max-width: 100px;
                justify-content: flex-end;
            }

            .sortie-img {
                width: 43px;
                height: 43px;
                object-fit: cover;
                border-radius: 4px;
                border: 1px solid var(--line2);
            }

            #lightbox {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.92);
                z-index: 9000;
                align-items: center;
                justify-content: center;
                cursor: pointer;
            }

            #lightboxRetirer {
                position: absolute;
                bottom: 18px;
                left: 50%;
                transform: translateX(-50%);
                background: rgba(180, 30, 30, 0.9);
                color: #fff;
                border: none;
                border-radius: 8px;
                padding: 10px 18px;
                font-size: 0.95rem;
                cursor: pointer;
            }

            .lb-nav {
                position: absolute;
                top: 50%;
                transform: translateY(-50%);
                width: 46px;
                height: 46px;
                display: none;
                align-items: center;
                justify-content: center;
                color: #fff;
                background: rgba(255,255,255,0.16);
                border: 1px solid rgba(255,255,255,0.25);
                border-radius: 50%;
                cursor: pointer;
                z-index: 2;
                -webkit-backdrop-filter: blur(6px);
                backdrop-filter: blur(6px);
            }

            .lb-nav:hover { background: rgba(255,255,255,0.28); }
            .lb-nav .ico { width: 24px; height: 24px; stroke-width: 2.4; }
            .lb-prec { left: 10px; }
            .lb-suiv { right: 10px; }

            #lightboxCompteur {
                position: absolute;
                top: 16px;
                left: 50%;
                transform: translateX(-50%);
                display: none;
                color: #fff;
                font-size: 0.85rem;
                font-weight: 700;
                background: rgba(0,0,0,0.5);
                border-radius: 999px;
                padding: 5px 14px;
            }

            #lightbox img {
                max-width: 96%;
                max-height: 96%;
                object-fit: contain;
                border-radius: 6px;
            }

            @media (max-width: 480px) {
                body {
                    padding: 0 12px calc(var(--nav-h) + 28px + env(safe-area-inset-bottom));
                }

                .sync-row {
                    flex-direction: column;
                }

                .card-top {
                    flex-direction: column;
                }

                .stats-row {
                    flex-direction: column;
                    gap: 4px;
                }
            }

            .onglets {
                position: fixed;
                left: 0;
                right: 0;
                bottom: 0;
                z-index: 50;
                display: flex;
                justify-content: center;
                gap: 0;
                height: calc(var(--nav-h) + env(safe-area-inset-bottom));
                padding: 6px 8px env(safe-area-inset-bottom);
                background: var(--nav-bg);
                -webkit-backdrop-filter: blur(14px);
                backdrop-filter: blur(14px);
                border-top: 1px solid var(--line3);
            }

            .onglet {
                flex: 1;
                max-width: 200px;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                gap: 3px;
                padding: 0;
                font: inherit;
                font-size: 0.72rem;
                font-weight: 600;
                color: var(--text-muted);
                background: none;
                border: none;
                cursor: pointer;
            }

            .onglet svg {
                width: 24px;
                height: 24px;
                fill: none;
                stroke: currentColor;
                stroke-width: 1.9;
                stroke-linecap: round;
                stroke-linejoin: round;
                box-sizing: content-box;
                padding: 4px 18px;
                border-radius: 999px;
                transition: background 0.2s;
            }

            .onglet.actif {
                color: var(--accent);
            }

            .onglet.actif svg {
                background: rgba(var(--ac-rgb),0.16);
            }

            .onglet:focus-visible {
                outline: 2px solid var(--accent);
                outline-offset: -4px;
                border-radius: 12px;
            }

            .page {
                max-width: 600px;
                margin: 0 auto;
            }

            /* ---- Habillage des cartes, barres et boutons ---- */
            .ico {
                width: 16px;
                height: 16px;
                flex-shrink: 0;
                fill: none;
                stroke: currentColor;
                stroke-width: 2;
                stroke-linecap: round;
                stroke-linejoin: round;
            }

            .card {
                position: relative;
                padding: 16px 16px 14px 20px;
                overflow: hidden;
            }

            .card::before {
                content: "";
                position: absolute;
                left: 0;
                top: 0;
                bottom: 0;
                width: 5px;
                background: linear-gradient(180deg, var(--ac1), var(--ac2));
            }

            .card.near-warning::before { background: linear-gradient(180deg, #fbbf24, #d97706); }
            .card.warning::before { background: linear-gradient(180deg, #f87171, #dc2626); }

            .component-name { line-height: 1.3; }

            .badge {
                display: inline-flex;
                align-items: center;
                gap: 6px;
                font-size: 0.74rem;
                padding: 4px 10px;
                white-space: nowrap;
            }

            .badge::before {
                content: "";
                width: 7px;
                height: 7px;
                border-radius: 50%;
                background: currentColor;
            }

            .bar-bg {
                position: relative;
                height: 12px;
                border-radius: 999px;
                background: var(--field);
                box-shadow: inset 0 1px 3px var(--inset);
                margin: 8px 0 14px;
            }

            .bar-bg::after {
                content: "";
                position: absolute;
                top: 2px;
                bottom: 2px;
                left: 85%;
                width: 2px;
                border-radius: 2px;
                background: var(--tick);
            }

            .bar-fill {
                border-radius: 999px;
                background: linear-gradient(90deg, var(--ac2), var(--ac1));
                box-shadow: 0 0 10px rgba(var(--ac-rgb),0.45);
            }

            .bar-fill.near-warning {
                background: linear-gradient(90deg, #d97706, #fbbf24);
                box-shadow: 0 0 10px rgba(251,191,36,0.4);
            }

            .bar-fill.warning {
                background: linear-gradient(90deg, #dc2626, #f87171);
                box-shadow: 0 0 10px rgba(248,113,113,0.45);
            }

            button.btn-action {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 8px;
                padding: 11px;
                font: inherit;
                font-size: 0.9rem;
                font-weight: 700;
                color: var(--accent);
                background: rgba(var(--ac-rgb),0.1);
                border: 1px solid rgba(var(--ac-rgb),0.35);
                border-radius: 12px;
                transition: background 0.15s, transform 0.1s;
            }

            button.btn-action:hover { background: rgba(var(--ac-rgb),0.2); }
            button.btn-action:active { transform: scale(0.98); }

            .card.warning button.btn-action,
            .card.near-warning button.btn-action {
                color: var(--on-grad);
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                border-color: transparent;
            }

            .card-tools { margin-top: 10px; gap: 8px; }

            .card-tools button,
            .placement-bar button,
            .mr-titre button,
            .mr-actions button,
            .btn-voir-plus {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 6px;
                font: inherit;
                font-size: 0.82rem;
                font-weight: 600;
                color: var(--text-muted);
                background: var(--fill);
                border: 1px solid var(--line2);
                border-radius: 999px;
                padding: 7px 14px;
                cursor: pointer;
                transition: background 0.15s, color 0.15s;
            }

            .card-tools button:hover,
            .placement-bar button:hover,
            .mr-titre button:hover,
            .mr-actions button:hover,
            .btn-voir-plus:hover {
                background: var(--fill2);
                color: var(--strong);
            }

            .apparence {
                max-width: 600px;
                margin: 0 auto 16px auto;
                background: var(--card-bg);
                border: 1px solid var(--line);
                border-radius: 14px;
                padding: 14px;
                box-shadow: var(--sh);
            }

            .apparence strong { display: block; margin-bottom: 10px; }

            .segments {
                display: flex;
                gap: 4px;
                padding: 4px;
                background: var(--fill);
                border-radius: 999px;
            }

            .segments button {
                flex: 1;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 6px;
                font: inherit;
                font-size: 0.85rem;
                font-weight: 600;
                color: var(--text-muted);
                background: transparent;
                border: none;
                border-radius: 999px;
                padding: 9px 6px;
                cursor: pointer;
            }

            .segments button.actif {
                color: #ffffff;
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                -webkit-text-stroke: 0.4px #000;
                paint-order: stroke fill;
            }

            .card-tools button.danger:hover {
                background: rgba(239,68,68,0.15);
                color: #f87171;
                border-color: rgba(239,68,68,0.4);
            }

            .placement-bar button.actif {
                color: var(--on-grad);
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                border-color: transparent;
            }

            .mr-actions button.mr-ok {
                color: var(--on-grad);
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                border-color: transparent;
            }

            .liste-entete {
                grid-column: 1 / -1;
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 10px;
                padding: 0 2px;
            }

            .liste-entete strong { font-size: 0.95rem; }

            .liste-entete button {
                display: inline-flex;
                align-items: center;
                gap: 6px;
                font: inherit;
                font-size: 0.82rem;
                font-weight: 600;
                color: var(--text-muted);
                background: var(--fill);
                border: 1px solid var(--line2);
                border-radius: 999px;
                padding: 7px 14px;
                cursor: pointer;
            }

            /* Cartes repliables : une ligne + barre, le détail s'ouvre au toucher */
            .card { padding: 12px 14px 12px 18px; }

            .card .card-top {
                align-items: center;
                margin-bottom: 0;
                cursor: pointer;
                -webkit-tap-highlight-color: transparent;
            }

            .card .card-top:focus-visible {
                outline: 2px solid var(--accent);
                outline-offset: 3px;
                border-radius: 8px;
            }

            .component-name {
                flex: 1;
                min-width: 0;
                font-size: 0.95rem;
            }

            .component-name {
                display: flex;
                align-items: center;
                gap: 10px;
            }

            .component-name .nom { min-width: 0; }

            .num {
                flex-shrink: 0;
                min-width: 30px;
                height: 30px;
                padding: 0 6px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border-radius: 10px;
                font-size: 0.82rem;
                font-weight: 800;
                color: #fff;
                text-shadow: 0 1px 2px rgba(0,0,0,0.35);
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                box-shadow: 0 2px 6px rgba(var(--ac2-rgb),0.35);
            }

            .num.near-warning {
                background: linear-gradient(135deg, #fbbf24, #d97706);
                box-shadow: 0 2px 6px rgba(217,119,6,0.35);
            }

            .num.warning {
                background: linear-gradient(135deg, #f87171, #dc2626);
                box-shadow: 0 2px 6px rgba(220,38,38,0.35);
            }

            .chevron { display: inline-flex; color: var(--text-muted); transition: transform 0.2s; }
            .card.ouverte .chevron { transform: rotate(180deg); }

            .card .bar-bg { height: 8px; margin: 10px 0 0; }
            .card.ouverte .bar-bg { margin-bottom: 12px; }

            .card.masquee { display: none; }

            .vue3d {
                position: fixed;
                inset: 0;
                z-index: 9500;
                background: #0b171c;
                color: #fff;
            }

            #carte3d { position: absolute; inset: 0; }

            .v3d-haut {
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                z-index: 3;
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 10px;
                padding: calc(10px + env(safe-area-inset-top)) 12px 10px;
                background: linear-gradient(rgba(0,0,0,0.6), rgba(0,0,0,0));
            }

            .v3d-haut strong { font-size: 0.95rem; text-shadow: 0 1px 3px rgba(0,0,0,0.6); }

            .v3d-bas {
                position: absolute;
                left: 0;
                right: 0;
                bottom: 0;
                z-index: 3;
                padding: 28px 12px calc(12px + env(safe-area-inset-bottom));
                background: linear-gradient(rgba(0,0,0,0), rgba(0,0,0,0.7));
            }

            .v3d-bas input[type="range"] {
                width: 100%;
                margin: 0 0 10px;
                accent-color: var(--ac1);
            }

            .v3d-boutons { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }

            .vue3d button {
                display: inline-flex;
                align-items: center;
                gap: 6px;
                font: inherit;
                font-size: 0.82rem;
                font-weight: 700;
                color: #fff;
                background: rgba(20,40,48,0.78);
                border: 1px solid rgba(255,255,255,0.25);
                border-radius: 999px;
                padding: 8px 14px;
                cursor: pointer;
                -webkit-backdrop-filter: blur(6px);
                backdrop-filter: blur(6px);
            }

            .vue3d button.actif { background: linear-gradient(135deg, var(--ac1), var(--ac2)); color: var(--on-grad); border-color: transparent; }
            .vue3d button.enreg { background: #dc2626; border-color: transparent; }
            #v3dFermer { width: 38px; height: 38px; padding: 0; justify-content: center; }

            .v3d-info {
                margin-top: 8px;
                text-align: center;
                font-size: 0.78rem;
                color: rgba(255,255,255,0.85);
                text-shadow: 0 1px 3px rgba(0,0,0,0.7);
            }

            .v3d-photo {
                position: absolute;
                z-index: 4;
                top: calc(64px + env(safe-area-inset-top));
                left: 50%;
                transform: translateX(-50%);
                padding: 6px;
                border-radius: 14px;
                background: rgba(255,255,255,0.95);
                box-shadow: 0 8px 30px rgba(0,0,0,0.5);
                cursor: pointer;
            }

            .v3d-photo img {
                display: block;
                max-width: min(78vw, 420px);
                max-height: 38vh;
                border-radius: 9px;
            }

            .v3d-marqueur {
                width: 30px;
                height: 30px;
                padding: 0;
                justify-content: center;
                font-size: 15px;
                border-radius: 50%;
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                border: 2px solid #fff;
                box-shadow: 0 2px 8px rgba(0,0,0,0.5);
            }

            .v3d-panneau { width: 48px; height: 44px; filter: drop-shadow(0 2px 3px rgba(0,0,0,.65)); }
            .v3d-panneau svg { width: 48px; height: 44px; display: block; }
            .v3d-lieu { padding: 4px 7px; border-radius: 6px; color: #fff; background: rgba(15,23,42,.88); border: 1px solid rgba(255,255,255,.8); font-size: 12px; font-weight: 700; white-space: nowrap; box-shadow: 0 2px 6px rgba(0,0,0,.45); }
            .v3d-lieu-haut { background: rgba(20,83,45,.92); }
            .v3d-cache { display: none !important; }
            .v3d-tourne { display: inline-block; width: 14px; height: 14px; margin-right: 8px; vertical-align: -2px; border-radius: 50%; border: 2px solid rgba(255,255,255,.35); border-top-color: #fff; animation: v3d-tour .8s linear infinite; }
            @keyframes v3d-tour { to { transform: rotate(360deg); } }

            .v3d-legende {
                position: absolute;
                z-index: 3;
                left: 12px;
                top: calc(58px + env(safe-area-inset-top));
                display: flex;
                flex-direction: column;
                gap: 3px;
                padding: 8px 10px;
                border-radius: 10px;
                font-size: 0.72rem;
                font-weight: 700;
                background: rgba(10,25,31,0.72);
            }

            .v3d-legende span i {
                display: inline-block;
                width: 14px;
                height: 5px;
                border-radius: 3px;
                margin-right: 6px;
                vertical-align: middle;
            }

            .fenetre-profil {
                position: fixed;
                inset: 0;
                z-index: 9600;
                align-items: center;
                justify-content: center;
                padding: 12px;
                background: rgba(0,0,0,0.6);
            }

            .fp-boite {
                width: min(100%, 940px);
                max-height: 94vh;
                overflow: auto;
                padding: 14px;
                border-radius: 16px;
                background: var(--card-bg);
                border: 1px solid var(--line);
            }

            .fp-haut { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
            #fpFermer { width: 38px; height: 38px; padding: 0; display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; border: 1px solid var(--line); background: var(--fill); color: var(--strong); cursor: pointer; }
            #fpGraph svg { width: 100%; height: auto; display: block; border-radius: 10px; background: var(--fill); }
            .fp-info { margin: 10px 0 4px; font-weight: 700; font-size: 0.9rem; min-height: 1.3em; }

            .profil-sortie {
                flex: 0 0 100%;
                margin-top: 10px;
            }

            .profil-sortie svg { width: 100%; height: auto; display: block; border-radius: 8px; background: var(--fill); }
            .profil-legende { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 6px; font-size: 0.74rem; color: var(--text-muted); }
            .profil-legende i { display: inline-block; width: 12px; height: 6px; border-radius: 3px; margin-right: 4px; vertical-align: middle; }

            .v3d-message {
                position: absolute;
                z-index: 4;
                left: calc(150px + env(safe-area-inset-left));
                right: calc(12px + env(safe-area-inset-right));
                top: calc(58px + env(safe-area-inset-top));
                text-align: center;
                font-size: 0.84rem;
                line-height: 1.35;
                padding: 10px 12px;
                border-radius: 12px;
                background: rgba(10,25,31,0.92);
                border: 1px solid rgba(255,255,255,0.2);
            }

            .v3d-recherche { position: absolute; z-index: 5; left: 50%; top: 64%; transform: translateX(-50%); display: flex; align-items: center; padding: 8px 14px; border-radius: 999px; font-size: 0.85rem; font-weight: 600; white-space: nowrap; color: #fff; background: rgba(10,25,31,0.92); border: 1px solid rgba(255,255,255,0.25); box-shadow: 0 2px 8px rgba(0,0,0,.4); }

            .videos-liste {
                flex: 0 0 100%;
                min-width: 0;
                display: flex;
                flex-direction: column;
                gap: 8px;
                margin: 10px 0;
            }

            .video-carte {
                position: relative;
                background: var(--fill);
                border: 1px solid var(--line);
                border-radius: 12px;
                overflow: hidden;
            }

            .video-lien {
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 8px 44px 8px 8px;
                color: var(--text-color);
                text-decoration: none;
            }

            .video-vignette {
                position: relative;
                flex-shrink: 0;
                width: 112px;
                aspect-ratio: 16 / 9;
                border-radius: 8px;
                overflow: hidden;
                background: linear-gradient(135deg, #123a44, var(--ac2));
                display: flex;
                align-items: center;
                justify-content: center;
            }

            .video-vignette img {
                position: absolute;
                inset: 0;
                width: 100%;
                height: 100%;
                object-fit: cover;
            }

            .video-play {
                position: relative;
                width: 30px;
                height: 30px;
                border-radius: 50%;
                background: rgba(0,0,0,0.6);
                color: #fff;
                display: flex;
                align-items: center;
                justify-content: center;
            }

            .video-play svg { width: 16px; height: 16px; margin-left: 2px; }

            .video-texte {
                display: flex;
                flex-direction: column;
                min-width: 0;
                gap: 2px;
            }

            .video-texte strong { font-size: 0.9rem; overflow-wrap: anywhere; }
            .video-texte small { color: var(--text-muted); font-size: 0.76rem; }

            .video-retirer {
                position: absolute;
                top: 6px;
                right: 6px;
                width: 32px;
                height: 32px;
                display: flex;
                align-items: center;
                justify-content: center;
                color: var(--text-muted);
                background: transparent;
                border: none;
                border-radius: 50%;
                cursor: pointer;
            }

            .video-retirer:hover { background: var(--fill2); color: var(--strong); }

            .guide {
                margin: 4px 0 12px;
                background: var(--fill);
                border: 1px solid var(--line);
                border-radius: 12px;
                padding: 0 12px;
            }

            .guide summary {
                cursor: pointer;
                padding: 10px 0;
                font-size: 0.86rem;
                font-weight: 700;
                color: var(--accent);
            }

            .guide[open] summary { border-bottom: 1px solid var(--line3); margin-bottom: 8px; }

            .guide p, .guide ol {
                font-size: 0.85rem;
                line-height: 1.45;
                margin: 0 0 8px;
            }

            .guide ol { padding-left: 20px; }
            .guide li { margin-bottom: 4px; }

            .guide-niveau {
                display: inline-block;
                font-size: 0.74rem;
                font-weight: 700;
                color: var(--text-muted);
                background: var(--fill2);
                border-radius: 999px;
                padding: 2px 10px;
                margin-bottom: 8px;
            }

            .guide .guide-outils { color: var(--text-muted); font-size: 0.8rem; }

            .guide-video {
                display: inline-flex;
                align-items: center;
                gap: 8px;
                margin: 2px 0 12px;
                padding: 8px 14px;
                font-size: 0.84rem;
                font-weight: 700;
                color: #fff;
                background: #d62b2b;
                border-radius: 999px;
                text-decoration: none;
            }

            .verifs {
                max-width: 600px;
                margin: 0 auto 16px auto;
                background: var(--card-bg);
                border: 1px solid var(--line);
                border-radius: 18px;
                padding: 12px 14px;
                box-shadow: var(--sh);
            }

            .verifs-titre {
                display: flex;
                align-items: baseline;
                justify-content: space-between;
                gap: 8px;
                margin-bottom: 10px;
            }

            .verifs-titre strong { font-size: 0.95rem; }
            .verifs-titre .form-help { margin: 0; }

            .chips { display: flex; flex-wrap: wrap; gap: 8px; }

            .chip {
                display: inline-flex;
                align-items: center;
                gap: 7px;
                font: inherit;
                font-size: 0.82rem;
                font-weight: 600;
                color: var(--text-color);
                background: var(--fill);
                border: 1px solid var(--line2);
                border-radius: 999px;
                padding: 7px 12px 7px 10px;
                cursor: pointer;
            }

            .chip::before {
                content: "";
                width: 9px;
                height: 9px;
                border-radius: 50%;
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
            }

            .chip.near-warning::before { background: linear-gradient(135deg, #fbbf24, #d97706); }
            .chip.warning::before { background: linear-gradient(135deg, #f87171, #dc2626); }
            .chip small { font-size: 0.74rem; font-weight: 600; color: var(--text-muted); }
            .chip.warning small { color: var(--danger); }
            .chip.near-warning small { color: var(--warning); }

            .chip.choisie {
                border-color: var(--accent);
                background: rgba(var(--ac-rgb),0.12);
            }

            .verif-detail {
                margin-top: 12px;
                padding-top: 10px;
                border-top: 1px solid var(--line3);
            }

            .verif-detail .vd-nom { font-weight: 700; font-size: 0.95rem; margin-bottom: 4px; }
            .verif-detail .vd-info { font-size: 0.82rem; color: var(--text-muted); margin-bottom: 8px; }

            .btn-voir-plus {
                width: 100%;
                padding: 12px;
                font-size: 0.9rem;
                justify-content: center;
            }

            .card-detail { display: none; padding-top: 4px; }
            .card.ouverte .card-detail { display: block; }

            .card .card-top { flex-direction: row; }
            .card .badge { padding: 3px 9px; }
        
            /* ---- Palettes de couleurs (choix dans Réglages > Apparence) ---- */
            :root[data-palette="bleu"] { --ac1: #5ab4ff; --ac2: #2a82e6; --ac3: #3d9bf2; --ac1-hover: #8ccaff; --ac2-hover: #1a5fb4; --ac-rgb: 90,180,255; --ac2-rgb: 42,130,230; }
            :root[data-palette="vert"] { --ac1: #6fdc6f; --ac2: #2a8a3c; --ac3: #4cb85a; --ac1-hover: #96e896; --ac2-hover: #1f7030; --ac-rgb: 111,220,111; --ac2-rgb: 42,138,60; }
            :root[data-palette="orange"] { --ac1: #ffae42; --ac2: #c75a08; --ac3: #e8801f; --ac1-hover: #ffc275; --ac2-hover: #a84a05; --ac-rgb: 255,174,66; --ac2-rgb: 199,90,8; }
            :root[data-palette="rose"] { --ac1: #ff7ab8; --ac2: #d6337f; --ac3: #ea5599; --ac1-hover: #ff9fcc; --ac2-hover: #b82a6c; --ac-rgb: 255,122,184; --ac2-rgb: 214,51,127; }
            :root[data-palette="violet"] { --ac1: #b49bff; --ac2: #7a5ae0; --ac3: #9678f0; --ac1-hover: #cbb9ff; --ac2-hover: #5d3fba; --ac-rgb: 180,155,255; --ac2-rgb: 122,90,224; }

            .palettes-titre { margin: 14px 0 8px; font-size: 0.85rem; font-weight: 600; color: var(--text-muted); }
            .palettes { display: flex; flex-wrap: wrap; gap: 12px; }
            .palettes button {
                width: 40px;
                height: 40px;
                padding: 0;
                border-radius: 50%;
                border: 3px solid transparent;
                background-clip: padding-box;
                cursor: pointer;
                box-shadow: 0 0 0 2px var(--line2);
            }
            .palettes button.actif { box-shadow: 0 0 0 3px var(--card-bg), 0 0 0 5px var(--strong); }

            /* ---- Mode jour : suit le réglage du téléphone, ou choix manuel ---- */
            @media (prefers-color-scheme: light) {
            :root:not([data-theme="dark"]):not([data-theme="light"]) {
                --bg-color: #eef4f3;
                --card-bg: #ffffff;
                --text-color: #16282e;
                --text-muted: #5b727a;
                --accent: var(--ac2);
                --accent-hover: var(--ac2-hover);
                --on-accent: #ffffff;
                --success: var(--ac2);
                --danger: #dc2626;
                --warning: #b45309;
                --line: rgba(20,60,70,0.10);
                --line2: rgba(20,60,70,0.16);
                --line3: rgba(20,60,70,0.12);
                --fill: rgba(20,60,70,0.05);
                --fill2: rgba(20,60,70,0.10);
                --tick: rgba(20,60,70,0.28);
                --sh: 0 4px 14px rgba(20,60,70,0.12);
                --field: #e3ecea;
                --field-line: #c3d3d0;
                --inset: rgba(20,60,70,0.18);
                --nav-bg: rgba(255,255,255,0.94);
                --bg-fade: rgba(238,244,243,0);
                --strong: #0b171c;
                color-scheme: light;
            }
            }

            :root[data-theme="light"] {
                --bg-color: #eef4f3;
                --card-bg: #ffffff;
                --text-color: #16282e;
                --text-muted: #5b727a;
                --accent: var(--ac2);
                --accent-hover: var(--ac2-hover);
                --on-accent: #ffffff;
                --success: var(--ac2);
                --danger: #dc2626;
                --warning: #b45309;
                --line: rgba(20,60,70,0.10);
                --line2: rgba(20,60,70,0.16);
                --line3: rgba(20,60,70,0.12);
                --fill: rgba(20,60,70,0.05);
                --fill2: rgba(20,60,70,0.10);
                --tick: rgba(20,60,70,0.28);
                --sh: 0 4px 14px rgba(20,60,70,0.12);
                --field: #e3ecea;
                --field-line: #c3d3d0;
                --inset: rgba(20,60,70,0.18);
                --nav-bg: rgba(255,255,255,0.94);
                --bg-fade: rgba(238,244,243,0);
                --strong: #0b171c;
                color-scheme: light;
            }
            /* ---- Téléphone à l'horizontale : barre de navigation sur le côté ---- */
            /* appliqué par le script (classe « paysage ») : suit aussi la rotation de l'écran */
            :root.paysage { --nav-w: 78px; }

            :root.paysage body {
                padding: 0 calc(16px + env(safe-area-inset-right)) 24px
                    calc(var(--nav-w) + 14px + env(safe-area-inset-left));
            }

            :root.paysage .header {
                position: static;
                max-width: 900px;
                margin-bottom: 8px;
                padding: 8px 0;
                background: none;
            }

            :root.paysage .page { max-width: 900px; }

            :root.paysage .onglets {
                top: 0;
                right: auto;
                bottom: 0;
                left: 0;
                width: calc(var(--nav-w) + env(safe-area-inset-left));
                height: auto;
                flex-direction: column;
                justify-content: center;
                gap: 4px;
                padding: 6px 0 6px env(safe-area-inset-left);
                border-top: none;
                border-right: 1px solid var(--line3);
            }

            :root.paysage .onglet {
                flex: 0 1 auto;
                max-width: none;
                padding: 4px 0;
            }

            :root.paysage .onglet svg { padding: 3px 14px; }

            :root.paysage .bike-container {
                max-width: none;
                width: min(100%, 900px, calc((100vh - 24px) * 16 / 9));
                margin-bottom: 14px;
            }

            :root.paysage .fenetre-profil { padding: 6px; }
            :root.paysage .fp-boite { padding: 10px; max-height: 98vh; }
            :root.paysage .fp-haut { margin-bottom: 6px; }

            :root.paysage .v3d-haut { padding-top: calc(6px + env(safe-area-inset-top)); padding-bottom: 6px; }
            :root.paysage .v3d-bas { padding: 14px calc(12px + env(safe-area-inset-right)) calc(6px + env(safe-area-inset-bottom)) calc(12px + env(safe-area-inset-left)); }
            :root.paysage .v3d-bas input[type="range"] { margin-bottom: 6px; }
            :root.paysage .vue3d button { padding: 5px 11px; font-size: 0.78rem; }
            :root.paysage .v3d-info { margin-top: 4px; }
            :root.paysage .v3d-legende { top: calc(46px + env(safe-area-inset-top)); left: calc(12px + env(safe-area-inset-left)); padding: 5px 8px; font-size: 0.68rem; }
            :root.paysage .v3d-photo { top: calc(46px + env(safe-area-inset-top)); left: auto; right: calc(12px + env(safe-area-inset-right)); transform: none; }
            :root.paysage .v3d-photo img { max-width: 40vw; max-height: 52vh; }
            :root.paysage .v3d-message { top: calc(46px + env(safe-area-inset-top)); left: calc(170px + env(safe-area-inset-left)); max-width: 420px; }

            :root.paysage #lightbox img { max-height: 92%; }


            /* ---- Habillage aux couleurs de l'appli, commun à tous les onglets ---- */
            .history-section, .mes-reglages, .apparence, .sync-card, .verifs {
                background:
                    linear-gradient(180deg, rgba(40,200,185,0.11), rgba(40,200,185,0) 170px),
                    var(--card-bg);
            }

            input[type="file"]::file-selector-button {
                margin-right: 10px;
                padding: 8px 14px;
                font: inherit;
                font-size: 0.85rem;
                font-weight: 700;
                color: var(--on-accent);
                background: var(--accent);
                border: none;
                border-radius: 999px;
                cursor: pointer;
            }

            #page-sorties details:not(.guide) > summary { color: var(--accent); }

            .onglets::before {
                content: "";
                position: absolute;
                top: -1px;
                left: 0;
                right: 0;
                height: 2px;
                pointer-events: none;
                background: linear-gradient(90deg, transparent, var(--ac1) 30%, var(--ac2) 70%, transparent);
            }

            :root.paysage .onglets::before {
                top: 0;
                bottom: 0;
                left: auto;
                right: -1px;
                width: 2px;
                height: auto;
                background: linear-gradient(180deg, transparent, var(--ac1) 30%, var(--ac2) 70%, transparent);
            }

            .onglet.actif svg {
                background: linear-gradient(135deg, var(--ac1), var(--ac3));
                stroke: var(--on-accent);
            }

            /* ---- Titres de blocs : même design que le bouton « Entretien » ---- */
            .titre-pastille.titre-pastille {
                display: flex;
                width: fit-content;
                align-items: center;
                gap: 8px;
                margin: 0 0 12px;
                padding: 9px 16px;
                font: inherit;
                font-size: 0.9rem;
                font-weight: 700;
                line-height: 1.2;
                color: #ffffff;
                background: linear-gradient(135deg, var(--ac1), var(--ac2));
                -webkit-text-stroke: 0.4px #000;
                paint-order: stroke fill;
                border-radius: 999px;
                box-shadow: 0 4px 12px rgba(var(--ac2-rgb),0.3);
            }

            .titre-pastille .ico { width: 17px; height: 17px; flex: none; }
            .titre-pastille small { font-size: 0.75rem; font-weight: 700; opacity: 0.85; }

            .hist-entete .titre-pastille.titre-pastille,
            .mr-titre .titre-pastille.titre-pastille,
            .verifs-titre .titre-pastille.titre-pastille { margin: 0; }

            /* ---- Page Sorties : formulaire d'import plus net (apparence seulement) ---- */
            #page-sorties .import-gpx > label,
            #page-sorties #blocStrava label {
                display: block;
                margin: 16px 2px 6px;
                font-size: 0.74rem;
                font-weight: 700;
                letter-spacing: 0.05em;
                text-transform: uppercase;
                color: var(--text-muted);
            }

            #page-sorties input[type="file"] {
                padding: 10px;
                margin-bottom: 4px;
                font-size: 0.85rem;
                background: var(--field);
                border: 1.5px dashed var(--field-line);
                border-radius: 14px;
            }

            #page-sorties .import-gpx > .form-help {
                margin: 8px 2px 4px;
                line-height: 1.45;
            }

            #page-sorties #blocStrava {
                margin-top: 18px;
                padding: 12px 14px;
                background: var(--fill);
                border: 1px solid var(--line2);
                border-radius: 14px;
            }

            #page-sorties #blocStrava[open] summary { margin-bottom: 4px; }

            /* Import : caché par défaut, ouvert depuis le menu trois points de « Mes sorties » */
            #blocImport { margin-bottom: 20px; padding: 14px; background: var(--fill); border: 1px solid var(--line2); border-radius: 18px; }
            #blocImport[data-mode="gpx"] #blocStrava { display: none; }
            #blocImport[data-mode="strava"] .import-gpx { display: none; }
            #blocImport #blocStrava { margin-top: 0; }
            .import-entete { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-weight: 800; color: var(--strong); }
            .import-fermer { width: 36px; height: 36px; padding: 0; display: inline-flex; align-items: center; justify-content: center; flex: none; color: var(--strong); background: var(--fill); border: 1px solid var(--line); border-radius: 50%; cursor: pointer; }
            .import-fermer svg { width: 18px; height: 18px; fill: none; stroke: currentColor; }

            /* ---- Pastilles rouges de nouveauté ---- */
            .onglet { position: relative; }

            .onglet.avec-pastille::after {
                content: "";
                position: absolute;
                top: 6px;
                left: calc(50% + 14px);
                width: 9px;
                height: 9px;
                border-radius: 50%;
                background: var(--danger);
                border: 2px solid var(--bg-color);
                pointer-events: none;
            }

            .pastille-nouvelle,
            .pastille-verifier {
                display: inline-block;
                margin-right: 8px;
                padding: 2px 8px;
                font-size: 0.66rem;
                font-weight: 800;
                letter-spacing: 0.04em;
                text-transform: uppercase;
                vertical-align: middle;
                color: #ffffff;
                background: var(--danger);
                border-radius: 999px;
            }

            /* ---- Fiche d'une sortie ---- */
            .sortie-cliquable { cursor: pointer; }
            .fiche-boite { width: min(100%, 560px); }
            .fiche-titre { font-size: 1.15rem; line-height: 1.25; }
            .fiche-bloc { margin-top: 16px; }

            .fiche-bloc h3 {
                margin: 0 0 8px;
                font-size: 0.74rem;
                font-weight: 700;
                letter-spacing: 0.05em;
                text-transform: uppercase;
                color: var(--text-muted);
            }

            .fiche-carte {
                position: relative;
                height: 250px;
                overflow: hidden;
                border-radius: 14px;
                background: var(--fill);
            }

            .fiche-carte svg { width: 100%; height: 100%; }

            .fiche-fond {
                position: absolute;
                top: 8px;
                left: 8px;
                z-index: 2;
                padding: 6px 12px;
                font: inherit;
                font-size: 0.78rem;
                font-weight: 700;
                color: #ffffff;
                background: rgba(0,0,0,0.55);
                border: 1px solid rgba(255,255,255,0.35);
                border-radius: 999px;
                cursor: pointer;
            }

            .fiche-grille { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }

            .fiche-tuile {
                display: flex;
                flex-direction: column;
                gap: 2px;
                padding: 10px 12px;
                background: var(--fill);
                border: 1px solid var(--line);
                border-radius: 12px;
            }

            .fiche-tuile span { font-size: 0.72rem; color: var(--text-muted); }
            .fiche-tuile strong { font-size: 1.05rem; font-weight: 800; color: var(--strong); }
            .fiche-meteo-titre { margin: 0 0 8px; font-size: 1.05rem; font-weight: 800; }
            .fiche-bloc svg { width: 100%; height: auto; display: block; border-radius: 10px; background: var(--fill); }
            .fiche-barre { display: flex; height: 14px; overflow: hidden; border-radius: 7px; background: var(--fill); }
            .fiche-legende { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; font-size: 0.8rem; color: var(--text-muted); }
            .fiche-legende i { display: inline-block; width: 12px; height: 8px; margin-right: 8px; border-radius: 3px; }
            .fiche-boite > .btn-survol { margin-top: 18px; }

            /* ---- Historique : bouton « Période » du filtre et fenêtre calendrier ---- */
            .menu-sortie button.hist-periode-btn {
                padding: 10px;
                font-size: 1rem;
                font-weight: 400;
                color: var(--strong);
                background: var(--field);
                border: 1px solid var(--field-line);
                border-radius: 12px;
            }

            .menu-sortie button.hist-periode-btn:hover { background: var(--field); border-color: var(--accent); }
            .hist-periode-btn .ico { width: 20px; height: 20px; flex: none; fill: none; stroke: var(--accent); }

            /* Champ de recherche des filtres : même look que les autres champs */
            .sorties-filtres-contenu input[type="search"] {
                -webkit-appearance: none;
                appearance: none;
                padding: 10px;
                font-size: 1rem;
                color: var(--strong);
                background: var(--field);
                border: 1px solid var(--field-line);
                border-radius: 12px;
                outline: none;
            }

            .sorties-filtres-contenu input.champ-km {
                -webkit-appearance: none;
                appearance: none;
                padding: 10px;
                font-size: 1rem;
                color: var(--strong);
                background: var(--field);
                border: 1px solid var(--field-line);
                border-radius: 12px;
                outline: none;
            }

            .sorties-filtres-contenu input[type="search"]:focus,
            .sorties-filtres-contenu input.champ-km:focus { border-color: var(--accent); }

            .sorties-km { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }

            .cal-boite { width: min(100%, 380px); }

            .cal-rond {
                width: 38px;
                height: 38px;
                padding: 0;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                flex: none;
                color: var(--strong);
                background: var(--fill);
                border: 1px solid var(--line);
                border-radius: 50%;
                cursor: pointer;
            }

            .cal-rond svg { width: 20px; height: 20px; fill: none; stroke: currentColor; }

            .cal-raccourcis { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }

            .cal-puce {
                padding: 7px 14px;
                font: inherit;
                font-size: 0.82rem;
                font-weight: 700;
                color: var(--accent);
                background: var(--fill);
                border: 1px solid var(--line2);
                border-radius: 999px;
                cursor: pointer;
            }

            .cal-navigation { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
            .cal-navigation strong { font-size: 1rem; }

            .cal-semaine, .cal-grille { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
            .cal-semaine { margin-bottom: 4px; }
            .cal-semaine span { text-align: center; font-size: 0.72rem; font-weight: 700; color: var(--text-muted); }

            .cal-jour {
                position: relative;
                aspect-ratio: 1 / 1;
                padding: 0;
                font: inherit;
                font-size: 0.92rem;
                font-weight: 600;
                color: var(--text-color);
                background: transparent;
                border: 1.5px solid transparent;
                border-radius: 12px;
                cursor: pointer;
            }

            .cal-jour.auj { border-color: var(--accent); }
            .cal-jour.dans-periode { background: rgba(var(--ac-rgb),0.2); }
            .cal-jour.borne { background: var(--accent); color: var(--on-accent); font-weight: 800; }

            .cal-jour.avec-entretien::after {
                content: "";
                position: absolute;
                left: 50%;
                bottom: 5px;
                width: 5px;
                height: 5px;
                margin-left: -2.5px;
                border-radius: 50%;
                background: var(--accent);
            }

            .cal-jour.borne.avec-entretien::after { background: var(--on-accent); }

            .cal-resume { margin: 12px 0; min-height: 2.6em; }
            .cal-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
            .cal-actions button { margin: 0; padding: 12px; font: inherit; font-weight: 700; border-radius: 999px; cursor: pointer; }

            .cal-secondaire {
                color: var(--text-color);
                background: var(--fill);
                border: 1px solid var(--line2);
            }

            /* ---- Fiche technique du vélo ---- */
            .fiche-technique-velo {
                max-width: 600px;
                margin: 0 auto 10px;
                padding: 6px 14px;
                background:
                    linear-gradient(180deg, rgba(40,200,185,0.11), rgba(40,200,185,0) 170px),
                    var(--card-bg);
                border: 1px solid var(--line);
                border-radius: 14px;
                box-shadow: var(--sh);
            }

            .ftv-resume {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 12px;
                padding: 10px 0;
                cursor: pointer;
                list-style: none;
            }

            .ftv-resume::-webkit-details-marker { display: none; }

            .ftv-entete {
                display: flex;
                flex-direction: column;
                align-items: flex-start;
                gap: 5px;
                min-width: 0;
            }

            .ftv-titre-pastille {
                margin: 0 !important;
            }

            .ftv-titre-pastille .ico {
                width: 17px;
                height: 17px;
                flex: none;
            }

            .ftv-sous-titre {
                display: block;
                color: var(--text-muted);
                font-size: 0.78rem;
                line-height: 1.35;
            }

            .ftv-chevron {
                width: 9px;
                height: 9px;
                flex: none;
                margin-right: 4px;
                border-right: 2px solid var(--text-muted);
                border-bottom: 2px solid var(--text-muted);
                transform: rotate(45deg);
                transition: transform 0.18s ease;
            }

            .fiche-technique-velo[open] .ftv-chevron {
                transform: rotate(225deg);
            }

            .ftv-taille-ligne {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 12px;
                margin: 2px 0 10px;
                padding: 9px 11px;
                background: var(--fill);
                border: 1px solid var(--line);
                border-radius: 10px;
            }
            .ftv-taille-ligne label { color: var(--text-muted); font-size: .8rem; font-weight: 650; }
            .ftv-taille-ligne select {
                min-width: 76px;
                padding: 7px 28px 7px 10px;
                color: var(--text-color);
                background: var(--card-bg);
                border: 1px solid var(--line2);
                border-radius: 8px;
                font: inherit;
                font-weight: 750;
            }

            .ftv-grille {
                display: grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 10px;
                padding: 4px 0 10px;
            }

            .ftv-item {
                display: flex;
                align-items: flex-start;
                gap: 10px;
                min-width: 0;
                padding: 12px 10px;
                background: var(--fill);
                border: 1px solid var(--line);
                border-radius: 12px;
            }

            .ftv-icone {
                display: grid;
                place-items: center;
                width: 38px;
                height: 38px;
                flex: none;
                color: var(--accent);
                background: rgba(40, 200, 185, 0.12);
                border: 1px solid rgba(40, 200, 185, 0.22);
                border-radius: 10px;
            }

            .ftv-icone svg {
                display: block;
                width: 27px;
                height: 27px;
                fill: none;
                stroke: currentColor;
                stroke-width: 1.8;
                stroke-linecap: round;
                stroke-linejoin: round;
            }

            .ftv-item > span:last-child { min-width: 0; }

            .ftv-libelle {
                display: block;
                margin: 1px 0 4px;
                color: var(--text-muted);
                font-size: 0.72rem;
                line-height: 1.25;
            }

            .ftv-valeur {
                display: block;
                color: var(--text-color);
                font-size: 0.85rem;
                font-weight: 750;
                line-height: 1.35;
                overflow-wrap: anywhere;
            }

            .ftv-note {
                margin: 2px 0 4px;
                color: var(--text-muted);
                font-size: 0.72rem;
                line-height: 1.45;
            }

            @media (max-width: 380px) {
                .ftv-grille { gap: 6px; }
                .ftv-item { gap: 7px; padding: 9px 7px; }
                .ftv-icone { width: 32px; height: 32px; border-radius: 8px; }
                .ftv-icone svg { width: 24px; height: 24px; }
                .ftv-valeur { font-size: 0.79rem; }
            }

        </style>
    </head>

    <body>
        <div id="authScreen">
            <div class="auth-card">
                <svg class="logo" style="width:64px;height:64px;border-radius:18px;margin-bottom:12px" role="img" aria-label="Maillons"><use href="#logo-maillon"/></svg>
                <h2>Maillons</h2>
                <p class="form-help">Entrez votre code d'accès</p>
                <input type="password" id="authInput" placeholder="Code secret" style="margin-bottom: 12px;">
                <button class="btn-primary" style="width: 100%;" onclick="tenterConnexion()">Valider</button>
                <p id="authError" style="color: var(--danger); font-size: 0.8rem; margin-top: 10px;"></p>
            </div>
        </div>

        <div id="fenetreProfil" class="fenetre-profil" style="display:none;" role="dialog" aria-label="Profil de la sortie">
            <div class="fp-boite">
                <div class="fp-haut">
                    <strong id="fpTitre"></strong>
                    <button type="button" id="fpFermer" aria-label="Fermer"></button>
                </div>
                <div id="fpGraph"></div>
                <div class="fp-info" id="fpInfo"></div>
                <div id="fpLegendes"></div>
            </div>
        </div>

        <div id="vue3d" class="vue3d" style="display:none;" role="dialog" aria-label="Survol 3D du parcours">
            <div id="carte3d"></div>
            <div class="v3d-haut">
                <strong id="v3dTitre"></strong>
                <button type="button" id="v3dFermer" aria-label="Fermer"></button>
            </div>
            <div class="v3d-legende" id="v3dLegende"></div>
            <div class="v3d-photo" id="v3dPhoto" style="display:none;">
                <img id="v3dPhotoImg" alt="Photo de la sortie">
            </div>
            <div class="v3d-message" id="v3dMessage" style="display:none;"></div>
            <div class="v3d-recherche" id="v3dRecherche" style="display:none;"><span class="v3d-tourne"></span>Recherche des noms de lieux…</div>
            <div class="v3d-bas">
                <input type="range" id="v3dProgres" min="0" max="1000" value="0" aria-label="Avancement du parcours">
                <div class="v3d-boutons">
                    <button type="button" id="v3dLecture"></button>
                    <button type="button" id="v3dVitesse"></button>
                    <button type="button" id="v3dInclinaison"></button>
                    <button type="button" id="v3dFond"></button>
                    <button type="button" id="v3dAutres"></button>
                    <button type="button" id="v3dReperes"></button>
                    <button type="button" id="v3dCouleur"></button>
                    <button type="button" id="v3dEnreg"></button>
                </div>
                <div class="v3d-info" id="v3dInfo"></div>
            </div>
        </div>

        <div id="lightbox" onclick="if (Date.now() - Number(this.dataset.balayage || 0) > 400) fermerPhoto()">
            <button id="lightboxPrec" class="lb-nav lb-prec" type="button" aria-label="Photo précédente"></button>
            <img id="lightboxImg" alt="Photo de la sortie">
            <button id="lightboxSuiv" class="lb-nav lb-suiv" type="button" aria-label="Photo suivante"></button>
            <div id="lightboxCompteur" aria-live="polite"></div>
            <button id="lightboxRetirer" type="button">🗑️ Retirer cette photo</button>
        </div>

        <svg width="0" height="0" style="position:absolute" aria-hidden="true">
            <defs>
                <linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0" style="stop-color:var(--ac1)"/>
                    <stop offset="1" style="stop-color:var(--ac2)"/>
                </linearGradient>
            </defs>
            <symbol id="logo-maillon" viewBox="0 0 96 96">
                <rect width="96" height="96" rx="24" fill="url(#lg)"/>
                <g fill="none" stroke-linecap="round">
                    <rect x="14" y="34" width="42" height="22" rx="11" transform="rotate(-32 35 45)" stroke="#06262b" stroke-width="9"/>
                    <rect x="40" y="42" width="42" height="22" rx="11" transform="rotate(-32 61 53)" stroke="#06262b" stroke-width="9"/>
                    <rect x="14" y="34" width="42" height="22" rx="11" transform="rotate(-32 35 45)" stroke="#fff" stroke-width="5"/>
                    <rect x="40" y="42" width="42" height="22" rx="11" transform="rotate(-32 61 53)" stroke="#fff" stroke-width="5"/>
                </g>
            </symbol>
        </svg>

        <header class="header">
            <svg class="logo" role="img" aria-label="Maillons"><use href="#logo-maillon"/></svg>
            <div class="header-titres">
                <h1>Maillons</h1>
                <p class="header-velo" id="headerVeloNom">Mon CUBE</p>
                <div class="select-velo-zone"><select id="selectVelo" aria-label="Choisir mon vélo"></select></div>
            </div>
            <span class="km-pill" id="veloInfo">… km</span>
        </header>

        <nav class="onglets" id="onglets" aria-label="Pages de l'application">
            <button type="button" class="onglet" id="onglet-entretien" onclick="location.hash='entretien'"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>Entretien</button>
            <button type="button" class="onglet" id="onglet-historique" onclick="location.hash='historique'"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Historique</button>
            <button type="button" class="onglet" id="onglet-reglages" onclick="location.hash='reglages'"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg>Réglages</button>
            <button type="button" class="onglet" id="onglet-sorties" onclick="location.hash='sorties'"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8 18h8a3 3 0 0 0 0-6H8a3 3 0 0 1 0-6h8"/></svg>Sorties</button>
        </nav>

        <div class="page" id="page-entretien">
        <div class="mode-switch" id="modeSwitch" role="tablist" aria-label="Afficher">
            <button type="button" role="tab" id="modeEntretien" aria-selected="true"></button>
            <button type="button" role="tab" id="modeVerifs" aria-selected="false"></button>
        </div>

        <div class="bike-container">
            <img
                src="https://shop.roulezjeunesse.com/cdn/shop/files/CubeStereoHybridONE44EXC8001.jpg?v=1736521382&width=1220"
                alt="Mon Cube Stereo Hybrid"
                class="bike-img"
            >
            <div id="photoVeloACompleter" style="display:none; position:absolute; inset:0; flex-direction:column; gap:12px; align-items:center; justify-content:center; text-align:center; padding:24px; color:#64748b; font-weight:700;">
                <span>Pas encore de photo pour ce vélo.</span>
                <button type="button" class="photo-velo-bouton" id="btnAjouterPhotoVelo" onclick="choisirPhotoVelo()">Ajouter une photo</button>
                <button type="button" class="photo-velo-bouton" id="btnPhotoVeloLien" onclick="photoDepuisLien()">Photo depuis un lien</button>
            </div>
            <input type="file" id="veloPhotoInput" accept="image/*" style="display:none">
            <div id="hotspotsContainer"></div>
        </div>

        <div class="placement-bar">
            <button type="button" id="btnPlacer" onclick="basculerPlacement()"></button>
            <span class="form-help" id="placementAide" style="display:none; margin:0;">
                Touchez un numéro, puis touchez son emplacement exact sur la photo.
            </span>
        </div>

        <section class="verifs" id="verifs" style="display:none;" aria-label="Vérifications">
            <div class="verifs-titre"><strong class="titre-pastille"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg><span>Vérifications</span></strong><span class="form-help" id="verifsInfo"></span></div>
            <div class="chips" id="verifsChips"></div>
            <div class="verif-detail" id="verifDetail" style="display:none;"></div>
        </section>

        <div id="composantsList" class="grid">
            <div class="loading">Chargement des composants...</div>
        </div>

        <div class="sync-card">
            <label for="kmInput">Kilométrage total du VTTAE (saisie en km)</label>

            <div class="sync-row">
                <input
                    type="number"
                    id="kmInput"
                    min="0"
                    step="1"
                    placeholder="Ex. : 2700"
                >

                <button class="btn-primary" onclick="syncKm()">
                    Mettre à jour
                </button>
            </div>
        </div>

        <div style="max-width: 600px; margin: 0 auto 20px auto; text-align: right;">
            <button
                class="btn-action"
                style="width: auto; padding: 6px 12px;"
                onclick="telechargerSauvegarde()"
            >
                💾 Télécharger ma sauvegarde
            </button>
        </div>

        </div>

        <div class="page" id="page-reglages" style="display:none;">
        <section class="mes-reglages" id="blocAjouterVelo">
            <div class="mr-titre"><strong id="titreFormVelo">Ajouter un vélo</strong><div id="veloMenuZone"></div></div>
            <p class="form-help">Chaque vélo aura ses sorties, ses kilomètres et son carnet d'entretien séparés. La photo personnalisée sera ajoutée lors d'une étape ultérieure.</p>
            <form id="formAjouterVelo">
                <label for="nouveauVeloNom">Nom du vélo</label>
                <input id="nouveauVeloNom" name="nom" type="text" maxlength="80" placeholder="Ex. : Mon gravel" required>
                <label for="nouveauVeloType">Type de vélo</label>
                <select id="nouveauVeloType" name="type" required>
                    <option value="vtt_electrique">VTT électrique</option>
                    <option value="vtt">VTT sans moteur</option>
                    <option value="enduro">Enduro</option>
                    <option value="descente">Descente</option>
                    <option value="gravel">Gravel</option>
                    <option value="gravel_electrique">Gravel électrique</option>
                    <option value="route">Route</option>
                    <option value="route_electrique">Route électrique</option>
                </select>
                <div class="form-help">Recherche automatique des caractéristiques</div>
                <label for="veloRechercheFiche">Modèle du vélo ou lien constructeur</label>
                <input id="veloRechercheFiche" type="text" maxlength="2000" placeholder="Ex. : EDR Easy Route ou lien de la fiche">
                <button type="button" id="boutonRechercheFiche" style="width:100%; margin:6px 0;">Rechercher les caractéristiques</button>
                <div id="resultatsRechercheFiche" class="form-help" aria-live="polite"></div>
                <label for="nouveauVeloMarque">Marque (facultatif)</label>
                <input id="nouveauVeloMarque" name="marque" type="text" maxlength="80">
                <label for="nouveauVeloModele">Modèle (facultatif)</label>
                <input id="nouveauVeloModele" name="modele" type="text" maxlength="120">
                <label for="nouveauVeloAnnee">Année (facultatif)</label>
                <input id="nouveauVeloAnnee" name="annee" type="number" min="1950" max="2100">
                <label for="nouveauVeloTaille">Taille du cadre (facultatif)</label>
                <input id="nouveauVeloTaille" name="taille" type="text" maxlength="30">
                <label for="nouveauVeloDate">Date d'achat (facultatif)</label>
                <input id="nouveauVeloDate" name="date_achat" type="date">
                <label for="nouveauVeloFreins">Freins</label>
                <select id="nouveauVeloFreins" name="freins">
                    <option value="inconnu">À confirmer</option>
                    <option value="disque">Freins à disque (type à confirmer)</option>
                    <option value="disque_hydraulique">Freins à disque hydrauliques</option>
                    <option value="disque_mecanique">Freins à disque mécaniques</option>
                    <option value="patins">Freins à patins</option>
                </select>
                <label for="nouveauVeloSuspension">Suspension</label>
                <select id="nouveauVeloSuspension" name="suspension">
                    <option value="inconnu">À confirmer</option>
                    <option value="rigide">Sans suspension</option>
                    <option value="avant">Fourche suspendue</option>
                    <option value="double">Fourche et amortisseur arrière</option>
                </select>
                <label for="nouveauVeloTubeless">Montage des pneus</label>
                <select id="nouveauVeloTubeless" name="tubeless">
                    <option value="inconnu">À confirmer</option>
                    <option value="chambre">Avec chambre à air</option>
                    <option value="tubeless">Tubeless</option>
                </select>
                <label for="nouveauVeloTransmission">Transmission</label>
                <select id="nouveauVeloTransmission" name="transmission">
                    <option value="inconnu">À confirmer</option>
                    <option value="chaine">Chaîne et dérailleur</option>
                    <option value="courroie">Courroie</option>
                </select>
                <div class="form-help">Les caractéristiques trouvées sont proposées, jamais appliquées sans vérification. Les entretiens dépendant d'un équipement non confirmé seront adaptés après validation.</div>
                <div id="champsElectriques">
                    <label for="nouveauVeloMoteur">Moteur (vélo électrique uniquement)</label>
                    <input id="nouveauVeloMoteur" name="moteur" type="text" maxlength="120">
                    <label for="nouveauVeloBatterie">Batterie (vélo électrique uniquement)</label>
                    <input id="nouveauVeloBatterie" name="batterie" type="text" maxlength="120">
                </div>
                <button class="btn-primary" id="boutonCreerVelo" type="submit" style="width:100%; margin-top:8px;">Créer le vélo</button>
                <button type="button" id="btnAnnulerEditionFiche" style="display:none; width:100%; margin-top:8px;" onclick="annulerEditionFiche()">Annuler la modification</button>
                <p class="form-help" id="nouveauVeloMessage"></p>
            </form>
        </section>
        <section class="apparence mr-repliable" id="blocApparence" aria-label="Apparence">
            <div class="mr-titre">
                <button type="button" class="mr-entete" aria-expanded="false" aria-controls="blocApparenceCorps">
                    <strong class="titre-pastille"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 3v18"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/></svg><span>Apparence</span></strong>
                    <span class="mr-chevron"></span>
                </button>
            </div>
            <div class="mr-corps" id="blocApparenceCorps">
            <div class="segments" id="segmentsTheme">
                <button type="button" data-theme-choix="auto"></button>
                <button type="button" data-theme-choix="light"></button>
                <button type="button" data-theme-choix="dark"></button>
            </div>
            <div class="palettes-titre">Couleurs de l'appli</div>
            <div class="palettes" id="palettesChoix">
                <button type="button" data-palette-choix="turquoise" title="Turquoise" aria-label="Turquoise" style="background-image: linear-gradient(135deg, #35e0c8, #0a8f9c)"></button>
                <button type="button" data-palette-choix="bleu" title="Bleu" aria-label="Bleu" style="background-image: linear-gradient(135deg, #5ab4ff, #2a82e6)"></button>
                <button type="button" data-palette-choix="vert" title="Vert" aria-label="Vert" style="background-image: linear-gradient(135deg, #6fdc6f, #2a8a3c)"></button>
                <button type="button" data-palette-choix="orange" title="Orange" aria-label="Orange" style="background-image: linear-gradient(135deg, #ffae42, #c75a08)"></button>
                <button type="button" data-palette-choix="rose" title="Rose" aria-label="Rose" style="background-image: linear-gradient(135deg, #ff7ab8, #d6337f)"></button>
                <button type="button" data-palette-choix="violet" title="Violet" aria-label="Violet" style="background-image: linear-gradient(135deg, #b49bff, #7a5ae0)"></button>
            </div>
            </div>
        </section>

        <section class="apparence mr-repliable" id="blocUnites" aria-label="Unités">
            <div class="mr-titre">
                <button type="button" class="mr-entete" aria-expanded="false" aria-controls="blocUnitesCorps">
                    <strong class="titre-pastille"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 17h16"/><circle cx="8" cy="7" r="2"/><circle cx="15" cy="17" r="2"/></svg><span>Unités</span></strong>
                    <span class="mr-chevron"></span>
                </button>
            </div>
            <div class="mr-corps" id="blocUnitesCorps">
            <p class="form-help" style="margin:0 0 12px;">Choisis les unités utilisées pour afficher les sorties. Les données enregistrées restent inchangées.</p>
            <div class="mr-ligne">
                <label for="unitesDistance">Distance</label>
                <select id="unitesDistance">
                    <option value="km">Kilomètres (km)</option>
                    <option value="mi">Miles (mi)</option>
                </select>
            </div>
            <div class="mr-ligne">
                <label for="unitesAltitude">Altitude et dénivelé</label>
                <select id="unitesAltitude">
                    <option value="m">Mètres (m)</option>
                    <option value="ft">Pieds (ft)</option>
                </select>
            </div>
            <div class="mr-ligne">
                <label for="unitesVitesse">Vitesse</label>
                <select id="unitesVitesse">
                    <option value="kmh">Kilomètres par heure (km/h)</option>
                    <option value="mph">Miles par heure (mph)</option>
                </select>
            </div>
            <div class="mr-ligne">
                <label for="unitesTemperature">Température</label>
                <select id="unitesTemperature">
                    <option value="c">Celsius (°C)</option>
                    <option value="f">Fahrenheit (°F)</option>
                </select>
            </div>
            <div class="mr-ligne">
                <label for="unitesHeure">Heure</label>
                <select id="unitesHeure">
                    <option value="24">24 heures</option>
                    <option value="12">12 heures (AM/PM)</option>
                </select>
            </div>
            </div>
        </section>

        <details class="fiche-technique-velo" id="ficheTechniqueCube">
            <summary class="ftv-resume">
                <span class="ftv-entete">
                    <strong class="titre-pastille ftv-titre-pastille">
                        
                        <span>Fiche technique du vélo</span>
                    </strong>
                    <span class="ftv-sous-titre">CUBE Stereo Hybrid ONE44 EXC 800 · 2025</span>
                </span>
                <span class="ftv-chevron" aria-hidden="true"></span>
            </summary>

            <div class="ftv-taille-ligne">
                <label for="ftvTailleCadre">Taille du cadre</label>
                <select id="ftvTailleCadre" aria-label="Taille du cadre">
                    <option value="S">S</option>
                    <option value="M">M</option>
                    <option value="L">L</option>
                    <option value="XL">XL</option>
                </select>
            </div>
            <div class="ftv-grille">
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M13 10h15l7 7v14l-7 7H13l-5-7V17z"/><path d="M17 16h9v16h-9z"/><path d="M20 12v4m4-4v4M20 32v4m4-4v4M8 21H4m4 8H4m31-8h5m-5 8h5"/></svg></span>
                    <span><span class="ftv-libelle">Moteur</span><span class="ftv-valeur">Bosch Performance Line CX</span><span class="ftv-libelle">Couple maximal 85 Nm</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><rect x="10" y="10" width="27" height="29" rx="4"/><path d="M18 6h11v4M37 19h4v11h-4"/><path d="M24 15l-6 11h6l-2 8 9-13h-6l2-6"/></svg></span>
                    <span><span class="ftv-libelle">Batterie</span><span class="ftv-valeur">Bosch PowerTube</span><span class="ftv-libelle">Capacité 800 Wh</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M16 7h9l4 5-3 5-5 20-5 4-4-2 3-6 4-17-3-5z"/><path d="M29 12l6 5-4 5M13 39l-3 3m9-3 4 2"/></svg></span>
                    <span><span class="ftv-libelle">Fourche avant</span><span class="ftv-valeur">Fox 36 Float AWL</span><span class="ftv-libelle">Débattement 140 mm</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="11" cy="10" r="4"/><circle cx="37" cy="38" r="4"/><path d="M14 13l20 22"/><path d="M16 14l-4 5 5 4-4 5 5 4-3 4"/><path d="M24 9l7 7m-2-11 7 7"/></svg></span>
                    <span><span class="ftv-libelle">Amortisseur arrière</span><span class="ftv-valeur">Fox Float Rhythm RC</span><span class="ftv-libelle">Débattement arrière 140 mm</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="12" cy="34" r="8"/><circle cx="35" cy="13" r="6"/><circle cx="12" cy="34" r="2"/><circle cx="35" cy="13" r="1.5"/><path d="M17 29l13-12M12 26V15h9m5 19h9v-8"/></svg></span>
                    <span><span class="ftv-libelle">Transmission</span><span class="ftv-valeur">Shimano XT · 12 vitesses</span><span class="ftv-libelle">Cassette Deore 10–51 dents</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="24" cy="24" r="16"/><circle cx="24" cy="24" r="4"/><path d="M24 8v5m0 22v5M8 24h5m22 0h5M13 13l4 4m14 14 4 4M35 13l-4 4M17 31l-4 4"/><path d="M24 20v-8"/></svg></span>
                    <span><span class="ftv-libelle">Freinage</span><span class="ftv-valeur">Shimano XT hydrauliques</span><span class="ftv-libelle">Disques 203 mm avant et arrière</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="24" cy="24" r="17"/><circle cx="24" cy="24" r="3"/><path d="M24 7v14m0 6v14M7 24h14m6 0h14M12 12l9 9m6 6 9 9M36 12l-9 9m-6 6-9 9"/></svg></span>
                    <span><span class="ftv-libelle">Roues et pneus</span><span class="ftv-valeur">Schwalbe Nobby Nic</span><span class="ftv-libelle">Section 2,6 pouces · roues <strong id="ftvTailleRoues">29 pouces</strong></span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><rect x="7" y="7" width="34" height="27" rx="5"/><path d="M17 41h14m-7-7v7M12 13h24M12 18h11"/><circle cx="32" cy="25" r="4"/></svg></span>
                    <span><span class="ftv-libelle">Commande et écran</span><span class="ftv-valeur">Bosch Purion 200</span><span class="ftv-libelle">Écran intégré</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M8 11h22l8 8v18H8z"/><path d="M13 16h15l5 5v11H13z"/><path d="M17 20h12m-12 5h12m-12 5h7"/></svg></span>
                    <span><span class="ftv-libelle">Cadre</span><span class="ftv-valeur">Aluminium Superlite</span><span class="ftv-libelle">Batterie intégrée · patte UDH</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="12" cy="35" r="7"/><circle cx="35" cy="13" r="5"/><path d="M17 30l13-12M12 28V15h10m4 19h9v-8M30 18l5 7"/></svg></span>
                    <span><span class="ftv-libelle">Dérailleur arrière</span><span class="ftv-valeur">Shimano XT RD-M8100-SGS</span><span class="ftv-libelle">12 vitesses · ShadowPlus</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M10 10h15v28H10z"/><path d="M25 15h9a5 5 0 0 1 5 5v8a5 5 0 0 1-5 5h-9"/><path d="M14 15h7m-7 6h7m-7 6h7m-7 6h7"/></svg></span>
                    <span><span class="ftv-libelle">Commandes de vitesses</span><span class="ftv-valeur">Shimano XT SL-M8100-IR</span><span class="ftv-libelle">Commande au guidon</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="24" cy="24" r="15"/><circle cx="24" cy="24" r="5"/><path d="M24 9v10m0 10v10M9 24h10m10 0h10"/></svg></span>
                    <span><span class="ftv-libelle">Pédalier et plateau</span><span class="ftv-valeur">ACID MTB Hybrid Pro</span><span class="ftv-libelle">36 dents en 27,5″ · 34 dents en 29″</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="24" cy="24" r="16"/><circle cx="24" cy="24" r="12"/><circle cx="24" cy="24" r="8"/><circle cx="24" cy="24" r="4"/></svg></span>
                    <span><span class="ftv-libelle">Cassette</span><span class="ftv-valeur">Shimano Deore CS-M6100</span><span class="ftv-libelle">12 pignons · 10–51 dents</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M8 15h8v8h-8zM20 15h8v8h-8zM32 15h8v8h-8zM14 27h8v8h-8zM26 27h8v8h-8z"/><path d="M16 19h4m8 0h4m-12 12h6"/></svg></span>
                    <span><span class="ftv-libelle">Chaîne</span><span class="ftv-valeur">KMC e12</span><span class="ftv-libelle">Chaîne 12 vitesses pour VAE</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M8 12h32v24H8z"/><path d="M13 17h22v14H13zM18 21v6m6-6v6m6-6v6"/></svg></span>
                    <span><span class="ftv-libelle">Guide-chaîne</span><span class="ftv-valeur">Reverse Flip-Guide E-Chain Guide</span><span class="ftv-libelle">Montage direct sur le cadre</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="24" cy="24" r="17"/><circle cx="24" cy="24" r="3"/><path d="M24 7v14m0 6v14M7 24h14m6 0h14M12 12l9 9m6 6 9 9M36 12l-9 9m-6 6-9 9"/></svg></span>
                    <span><span class="ftv-libelle">Jantes</span><span class="ftv-valeur">CUBE EX30</span><span class="ftv-libelle">Tubeless Ready · 32 trous</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="24" cy="24" r="15"/><circle cx="24" cy="24" r="5"/><path d="M24 9v10m0 10v10M9 24h10m10 0h10"/></svg></span>
                    <span><span class="ftv-libelle">Moyeux</span><span class="ftv-valeur">CUBE by Newmen</span><span class="ftv-libelle">Avant 15 mm · arrière 12 mm Boost</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M18 7h12l-2 22h-8z"/><path d="M17 29h14l5 5v6H12v-6z"/><path d="M19 13h10m-10 5h10"/></svg></span>
                    <span><span class="ftv-libelle">Tige de selle télescopique</span><span class="ftv-valeur">CUBE Dropper Post</span><span class="ftv-libelle">Diamètre 31,6 mm · commande au guidon</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M8 29c5-9 10-12 18-12h8l6 7-4 7H18l-6 4z"/><path d="M17 29h18"/></svg></span>
                    <span><span class="ftv-libelle">Selle</span><span class="ftv-valeur">ACID Venec EMTB Plus 145</span><span class="ftv-libelle">Largeur annoncée 145 mm</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M8 14h13l5 8-4 5H9zM27 25h12l3 8-4 4H25z"/><path d="M13 18h5m12 11h5"/></svg></span>
                    <span><span class="ftv-libelle">Pédales</span><span class="ftv-valeur">ACID PP MTB</span><span class="ftv-libelle">Pédales plates d’origine</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><rect x="10" y="8" width="22" height="32" rx="3"/><path d="M32 17h6v14h-6M16 14h10m-10 6h10m-10 6h7"/><path d="M20 34l3-5 3 5"/></svg></span>
                    <span><span class="ftv-libelle">Chargeur</span><span class="ftv-valeur">Bosch 2 A</span><span class="ftv-libelle">Pour batterie Bosch PowerTube 800</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M9 11h30v26H9z"/><path d="M15 17h18v14H15z"/><path d="M19 20l4 8 6-8"/></svg></span>
                    <span><span class="ftv-libelle">Plaquettes de frein</span><span class="ftv-valeur">Shimano compatibles BR-M8120</span><span class="ftv-libelle">Référence exacte à confirmer avant achat</span></span>
                </div>
            </div>
            <p class="ftv-note">Caractéristiques du modèle 2025. Les équipements peuvent varier selon la version et la taille du vélo.</p>
        </details>
        <details class="fiche-technique-velo" id="ficheTechniqueAutre" style="display:none;">
            <summary class="ftv-resume">
                <span class="ftv-entete">
                    <strong class="titre-pastille ftv-titre-pastille">
                        
                        <span>Fiche technique du vélo</span>
                    </strong>
                    <span class="ftv-sous-titre" id="ficheAutreNom">—</span>
                </span>
                <span class="ftv-chevron" aria-hidden="true"></span>
            </summary>
            <div class="ftv-grille">
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="12" cy="32" r="8"/><circle cx="36" cy="32" r="8"/><path d="M12 32l7-14h12l5 14M19 18l7 14M26 32h10M17 13h6M29 18l-2-5h-4"/></svg></span>
                    <span><span class="ftv-libelle">Type</span><span class="ftv-valeur" id="ficheAutreType">—</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M8 8h16l16 16-16 16L8 24z"/><circle cx="17" cy="17" r="3"/></svg></span>
                    <span><span class="ftv-libelle">Marque</span><span class="ftv-valeur" id="ficheAutreMarque">—</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><rect x="9" y="8" width="30" height="32" rx="4"/><path d="M15 17h18M15 24h18M15 31h10"/></svg></span>
                    <span><span class="ftv-libelle">Modèle</span><span class="ftv-valeur" id="ficheAutreModele">—</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="13" cy="24" r="9"/><circle cx="35" cy="24" r="9"/><path d="M13 24h22M24 13v22"/></svg></span>
                    <span><span class="ftv-libelle">Freins</span><span class="ftv-valeur" id="ficheAutreFreins">À confirmer</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M14 6l-4 28m4-28l10 28m-10-28h7M10 34l-3 8m13-8l3 8M27 12h12m-12 7h12m-12 7h12"/></svg></span>
                    <span><span class="ftv-libelle">Suspension</span><span class="ftv-valeur" id="ficheAutreSuspension">À confirmer</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="24" cy="24" r="16"/><circle cx="24" cy="24" r="7"/><path d="M24 8v9m16 7h-9M24 40v-9M8 24h9"/></svg></span>
                    <span><span class="ftv-libelle">Pneus</span><span class="ftv-valeur" id="ficheAutreTubeless">À confirmer</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><circle cx="12" cy="32" r="7"/><circle cx="36" cy="32" r="7"/><path d="M12 32l8-17h9l7 17M20 15h9M24 15l-4-6"/></svg></span>
                    <span><span class="ftv-libelle">Transmission</span><span class="ftv-valeur" id="ficheAutreTransmission">À confirmer</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><rect x="7" y="10" width="34" height="30" rx="4"/><path d="M7 19h34M16 6v8m16-8v8"/></svg></span>
                    <span><span class="ftv-libelle">Année</span><span class="ftv-valeur" id="ficheAutreAnnee">—</span></span>
                </div>
                <div class="ftv-item">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M8 34L34 8l6 6L14 40z"/><path d="M16 26l4 4m2-10l4 4m2-10l4 4"/></svg></span>
                    <span><span class="ftv-libelle">Taille du cadre</span><span class="ftv-valeur" id="ficheAutreTaille">—</span></span>
                </div>
                <div class="ftv-item" id="ficheAutreMoteurLigne">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M13 10h15l7 7v14l-7 7H13l-5-7V17z"/><path d="M17 16h9v16h-9z"/><path d="M20 12v4m4-4v4M20 32v4m4-4v4M8 21H4m4 8H4m31-8h5m-5 8h5"/></svg></span>
                    <span><span class="ftv-libelle">Moteur</span><span class="ftv-valeur" id="ficheAutreMoteur">—</span></span>
                </div>
                <div class="ftv-item" id="ficheAutreBatterieLigne">
                    <span class="ftv-icone" aria-hidden="true"><svg viewBox="0 0 48 48"><rect x="10" y="10" width="27" height="29" rx="4"/><path d="M18 6h11v4M37 19h4v11h-4"/><path d="M24 15l-6 11h6l-2 8 9-13h-6l2-6"/></svg></span>
                    <span><span class="ftv-libelle">Batterie</span><span class="ftv-valeur" id="ficheAutreBatterie">—</span></span>
                </div>
            </div>
            <button type="button" id="btnModifierFicheVelo" onclick="modifierFicheVelo()" style="width:100%; margin: 4px 0 8px;">Modifier cette fiche</button>
        </details>

        <section class="mes-reglages mr-repliable" id="mesReglages" aria-label="Mes réglages vélo">
            <div class="mr-titre">
                <button type="button" class="mr-entete" aria-expanded="false" aria-controls="mesReglagesCorps">
                    <strong class="titre-pastille"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg><span>Mes réglages vélo</span></strong>
                    <span class="mr-chevron"></span>
                </button>
            </div>
            <div class="mr-corps" id="mesReglagesCorps">
                <div class="mr-titre">
                    <div class="form-help" id="mrDate" style="margin:0;"></div>
                    <button type="button" id="mrModifier" onclick="modifierReglagesVelo()"></button>
                </div>
                <div id="mrContenu"></div>
                <div class="mr-actions" id="mrActions" style="display:none;">
                    <button type="button" class="mr-ok" id="mrOk" onclick="enregistrerReglagesVelo()"></button>
                    <button type="button" id="mrAnnuler" onclick="annulerReglagesVelo()"></button>
                </div>
            </div>
        </section>
        <section class="mes-reglages mr-repliable" id="reglagesUsure" aria-label="Réglages de l'usure">
            <div class="mr-titre">
                <button type="button" class="mr-entete" aria-expanded="false" aria-controls="reglagesUsureCorps">
                    <strong class="titre-pastille"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 17a8 8 0 1 1 16 0"/><path d="M12 17l4-5"/></svg><span>Réglages de l'usure</span></strong>
                    <span class="mr-chevron"></span>
                </button>
            </div>
            <div class="mr-corps" id="reglagesUsureCorps">
                <details class="guide">
                    <summary>Comment ça marche</summary>
                    <p>
                        Chaque sortie fait vieillir tes pièces plus ou moins vite
                        selon le terrain. 1 = usure normale, 1,6 = usure 60 % plus
                        rapide. Le dénivelé compte aussi : 0 = on l'ignore,
                        1 = poids normal. Le type de chemin de chaque sortie est
                        analysé automatiquement : ce sont alors les pourcentages réels
                        de route, chemin et sentier qui comptent. Le coefficient
                        « Mixte » sert pour les parties que l'analyse ne reconnaît pas.
                    </p>
                </details>

                <div class="mr-ligne">
                    <label for="regRoute">Route / bitume</label>
                    <input type="text" inputmode="decimal" id="regRoute">
                </div>

                <div class="mr-ligne">
                    <label for="regMixte">Mixte (par défaut)</label>
                    <input type="text" inputmode="decimal" id="regMixte">
                </div>

                <div class="mr-ligne">
                    <label for="regChemin">Chemin / piste</label>
                    <input type="text" inputmode="decimal" id="regChemin">
                </div>

                <div class="mr-ligne">
                    <label for="regSentier">Sentier technique</label>
                    <input type="text" inputmode="decimal" id="regSentier">
                </div>

                <div class="mr-ligne">
                    <label for="regDenivele">Poids du dénivelé</label>
                    <input type="text" inputmode="decimal" id="regDenivele">
                </div>

                <button
                    class="btn-primary"
                    style="width:100%; margin-top: 10px;"
                    onclick="enregistrerReglages()"
                >
                    Enregistrer les réglages
                </button>

                <p class="form-help" id="reglagesMessage" style="margin-top: 10px;"></p>

                <details class="guide guide-coefs" id="coefsPiecesDetails">
                    <summary>Coefficient de chaque élément</summary>
                    <p>
                        Un coefficient pour chaque élément d'entretien, en plus de
                        ceux du terrain. 1 = usure normale, 1,5 = cet élément
                        s'use 50 % plus vite, 0,8 = 20 % plus lentement. Il joue
                        sur le seuil en km et sur le seuil en jours.
                    </p>

                    <div id="coefsPiecesListe"></div>

                    <button
                        class="btn-primary"
                        style="width:100%; margin-top: 10px;"
                        onclick="enregistrerCoefsPieces()"
                    >
                        Enregistrer les coefficients
                    </button>

                    <p class="form-help" id="coefsPiecesMessage" style="margin-top: 10px;"></p>
                </details>
            </div>
        </section>
        </div>

        <div class="page" id="page-historique" style="display:none;">
        <div class="history-section">
            <div class="hist-entete">
                <h2 class="titre-pastille"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h11"/></svg><span>Carnet d'entretien</span><small id="histCompteur"></small></h2>
                <div id="histMenuZone"></div>
            </div>

            <div class="hist-total" id="histTotal"></div>
            <div class="hist-selection" id="histSelection" style="display:none;"></div>

            <div id="historyList">
                <div class="loading">Chargement de l'historique...</div>
            </div>
        </div>

        </div>

        <div id="fenetreIntervention" class="fenetre-profil" style="display:none;" role="dialog" aria-modal="true" aria-labelledby="histTitre">
            <div class="fp-boite fi-boite">
                <div class="fp-haut">
                    <h2 id="histTitre" style="margin:0;"></h2>
                    <button type="button" id="histFermer" aria-label="Fermer"></button>
                </div>

                <label for="histComposant">Composant concerné</label>
                <select id="histComposant"></select>

                <label for="histDate">Date de l'intervention</label>
                <input type="date" id="histDate" required>

                <label for="histKm">Kilométrage au moment de l'intervention (saisie en km)</label>
                <input
                    type="number"
                    id="histKm"
                    min="0"
                    step="1"
                    placeholder="Ex. : 2445"
                    required
                >

                <p class="form-help" id="histAide"></p>

                <label for="histCout">Coût en euros</label>
                <input
                    type="number"
                    step="0.01"
                    min="0"
                    id="histCout"
                    placeholder="Coût en € (optionnel)"
                >

                <label for="histNotes">Notes</label>
                <input
                    type="text"
                    id="histNotes"
                    placeholder="Ex. : entretien réalisé chez le bouclard"
                >

                <button
                    class="btn-primary"
                    style="width:100%;"
                    id="histEnregistrer"
                    onclick="enregistrerIntervention()"
                >
                    Enregistrer l'intervention
                </button>
            </div>
        </div>

        <div class="page" id="page-sorties" style="display:none;">
        <div class="history-section" style="margin-bottom: 20px;">
            <div id="blocImport" data-mode="gpx" style="display:none;">
            <div class="import-entete">
                <span>Importer des sorties</span>
                <button type="button" id="importFermer" class="import-fermer" aria-label="Fermer l'import" onclick="fermerImport()"></button>
            </div>
            <div class="import-gpx">
            <label for="gpxFile">Importer une ou plusieurs sorties (fichiers GPX de Komoot)</label>
            <input
                type="file"
                id="gpxFile"
                accept=".gpx,application/gpx+xml,text/xml"
                multiple
            >

            <p class="form-help">
                Plusieurs fichiers d'un coup sont possibles (30 maximum).
                Le terrain et la note choisis ci-dessous s'appliquent à toutes
                les sorties ; tu pourras corriger le terrain de chacune ensuite.
                Les photos ne se joignent qu'à une seule sortie à la fois.
            </p>

            <label for="gpxPhotos">Photos souvenirs (plusieurs possibles)</label>
            <input
                type="file"
                id="gpxPhotos"
                accept="image/*"
                multiple
            >

            <label for="gpxTerrain">Terrain</label>
            <select id="gpxTerrain">
                <option value="mixte">Mixte</option>
                <option value="route">Route / bitume</option>
                <option value="chemin">Chemin / piste</option>
                <option value="sentier">Sentier technique</option>
            </select>

            <label for="gpxNotes">Notes (optionnel)</label>
            <input
                type="text"
                id="gpxNotes"
                placeholder="Ex. : sortie boueuse, pluie"
            >

            <button
                class="btn-primary"
                id="gpxBouton"
                style="width:100%;"
                onclick="importerGpx()"
            >
                Importer
            </button>

            <p class="form-help" id="gpxMessage" style="margin-top: 10px;"></p>
            </div>

            <details id="blocStrava" style="margin-top: 20px;">
                <summary style="cursor: pointer; font-weight: 600;">
                    📥 Importer depuis Strava (archive)
                </summary>

                <p class="form-help" style="margin-top: 10px;">
                    Sur le site strava.com : Réglages → Mon compte →
                    « Télécharger ou supprimer votre compte » →
                    « Demander votre archive » (les libellés peuvent varier).
                    Strava t'envoie un e-mail avec un lien : télécharge le
                    fichier .zip, puis choisis-le ici. Le fichier est lu dans
                    ton navigateur : seules les sorties choisies sont envoyées
                    à l'application. Les photos ne sont pas reprises.
                </p>

                <label for="stravaZip">Archive Strava (.zip)</label>
                <input type="file" id="stravaZip" accept=".zip,application/zip">

                <div id="stravaChoix" style="display: none;">
                    <label for="stravaVelo">Vélo (équipement Strava)</label>
                    <select id="stravaVelo"></select>

                    <label for="stravaType">Type d'activité</label>
                    <select id="stravaType"></select>

                    <label for="stravaTerrain">Terrain</label>
                    <select id="stravaTerrain">
                        <option value="mixte">Mixte</option>
                        <option value="route">Route / bitume</option>
                        <option value="chemin">Chemin / piste</option>
                        <option value="sentier">Sentier technique</option>
                    </select>

                    <p class="form-help" id="stravaCompte"></p>

                    <button
                        class="btn-primary"
                        id="stravaBouton"
                        style="width:100%;"
                        onclick="importerStrava()"
                    >
                        Importer ces sorties
                    </button>
                </div>

                <p class="form-help" id="stravaMessage" style="margin-top: 10px;"></p>
            </details>
            </div>

            <div class="hist-entete sorties-entete" style="margin:0 0 12px;">
                <h2 class="titre-pastille"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8 18h8a3 3 0 0 0 0-6H8a3 3 0 0 1 0-6h8"/></svg><span>Mes sorties</span></h2>
                <div id="sortiesMenuZone"></div>
            </div>

            <div id="sortiesFiltres" class="menu-sortie menu-filtre menu-filtres-sorties">
                <div class="sorties-filtres-contenu">
                    <label for="rechercheSorties">Rechercher une sortie</label>
                    <input id="rechercheSorties" type="search" placeholder="Nom, note, activité..." aria-label="Rechercher une sortie" oninput="appliquerFiltresSorties()">
                    <label for="periodeSortiesBtn">Période</label>
                    <button type="button" id="periodeSortiesBtn" class="hist-periode-btn" aria-label="Filtrer par période" onclick="ouvrirCalendrierSorties()"></button>
                    <label for="kmMinSorties">Distance (<span id="uniteFiltreDistance">km</span>)</label>
                    <div class="sorties-km">
                        <input id="kmMinSorties" class="champ-km" type="text" inputmode="decimal" placeholder="Min" aria-label="Distance minimale" oninput="appliquerFiltresSorties()">
                        <input id="kmMaxSorties" class="champ-km" type="text" inputmode="decimal" placeholder="Max" aria-label="Distance maximale" oninput="appliquerFiltresSorties()">
                    </div>
                    <label for="favorisSortiesBtn">Favoris</label>
                    <button type="button" id="favorisSortiesBtn" class="hist-periode-btn" aria-pressed="false" onclick="basculerFiltreFavoris()"></button>
                    <label for="zoneSorties">Zone géographique</label>
                    <select id="zoneSorties" aria-label="Filtrer par zone géographique" onchange="appliquerFiltresSorties()">
                        <option value="">Toutes les zones</option>
                    </select>
                    <label for="triSorties">Trier les sorties</label>
                    <select id="triSorties" aria-label="Trier les sorties" onchange="appliquerFiltresSorties()">
                        <option value="date-desc">Plus récentes</option>
                        <option value="date-asc">Plus anciennes</option>
                        <option value="distance-desc">Distance décroissante</option>
                        <option value="distance-asc">Distance croissante</option>
                        <option value="denivele-desc">Dénivelé décroissant</option>
                        <option value="denivele-asc">Dénivelé croissant</option>
                    </select>
                </div>
                <button type="button" id="reinitialiserFiltresSorties" role="button">
                    <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 1 0 2.64-6.36L3 8"/><path d="M3 3v5h5"/></svg>
                    <span>Réinitialiser les filtres</span>
                </button>
            </div>
            <p id="compteurSorties" class="form-help" style="margin:0 0 10px;"></p>
            <div id="sortiesList">
                <div class="loading">Chargement des sorties...</div>
            </div>

        </div>

        </div>

        <script>
            const PAGES = ['entretien', 'historique', 'reglages', 'sorties'];

            function afficherPage() {
                let nom = (location.hash || '').replace('#', '');

                if (!PAGES.includes(nom)) {
                    nom = 'entretien';
                }

                PAGES.forEach(p => {
                    document.getElementById('page-' + p).style.display =
                        p === nom ? 'block' : 'none';

                    const bouton = document.getElementById('onglet-' + p);
                    bouton.classList.toggle('actif', p === nom);

                    if (p === nom) {
                        bouton.setAttribute('aria-current', 'page');
                    } else {
                        bouton.removeAttribute('aria-current');
                    }
                });

                window.scrollTo(0, 0);
                viderPastilleOnglet(nom);
            }

            // ---- Pastilles rouges : nouveautés depuis la dernière visite (mémorisées sur cet appareil) ----
            var etatComposants = null;

            function lireVus() {
                try {
                    const texte = localStorage.getItem('maillons_vus_' + veloActifId);
                    return texte ? JSON.parse(texte) : null;
                } catch (e) {
                    return null;
                }
            }

            function ecrireVus(v) {
                try {
                    localStorage.setItem('maillons_vus_' + veloActifId, JSON.stringify(v));
                } catch (e) {}
            }

            function pageCourante() {
                const nom = (location.hash || '').replace('#', '');
                return PAGES.includes(nom) ? nom : 'entretien';
            }

            function dessinerPastilles() {
                const v = lireVus();
                PAGES.forEach(p => {
                    const bouton = document.getElementById('onglet-' + p);
                    if (!bouton) return;
                    const actif = Boolean(v && v.badges && v.badges[p]);
                    bouton.classList.toggle('avec-pastille', actif);
                    if (!bouton.dataset.libelle) bouton.dataset.libelle = bouton.textContent.trim();
                    bouton.setAttribute('aria-label', actif ? bouton.dataset.libelle + ' (nouveauté)' : bouton.dataset.libelle);
                });
            }

            function viderPastilleOnglet(nom) {
                const v = lireVus();
                if (v && v.badges && v.badges[nom]) {
                    v.badges[nom] = false;
                    ecrireVus(v);
                }
                dessinerPastilles();
            }

            function sortieEstNouvelle(id) {
                const v = lireVus();
                return Boolean(v && Array.isArray(v.nouvelles) && v.nouvelles.includes(String(id)));
            }

            function effacerNouvelleSortie(id) {
                const v = lireVus();
                if (!v || !Array.isArray(v.nouvelles)) return;
                v.nouvelles = v.nouvelles.filter(x => x !== String(id));
                ecrireVus(v);
            }

            // Compare ce qui existe maintenant avec ce qu'on connaissait : le nouveau déclenche la pastille.
            function actualiserPastilles() {
                let v = lireVus();
                if (!v || typeof v !== 'object') v = {};
                if (!v.base) v.base = {};
                if (!v.badges) v.badges = {};
                if (!Array.isArray(v.nouvelles)) v.nouvelles = [];

                const page = pageCourante();
                const ids = (liste) => liste.map(x => String(x.id));
                const traiter = (nom, courants, charge) => {
                    const ancien = v.base[nom];
                    if (!Array.isArray(ancien)) {
                        // Première fois : on retient l'existant sans rien signaler.
                        if (charge) v.base[nom] = courants;
                        return [];
                    }
                    const nouveaux = courants.filter(id => !ancien.includes(id));
                    v.base[nom] = courants;
                    if (nouveaux.length && page !== nom) v.badges[nom] = true;
                    return nouveaux;
                };

                const enAlerte = Array.isArray(etatComposants)
                    ? ids(etatComposants.filter(c => c.alerte_entretien))
                    : [];
                traiter('entretien', enAlerte, Array.isArray(etatComposants));
                traiter('historique', ids(historiqueDonnees), historiqueDonnees.length > 0);

                const idsSorties = ids(sortiesConnues);
                const nouvellesSorties = traiter('sorties', idsSorties, idsSorties.length > 0);
                nouvellesSorties.forEach(id => {
                    if (!v.nouvelles.includes(id)) v.nouvelles.push(id);
                });
                if (idsSorties.length > 0) {
                    v.nouvelles = v.nouvelles.filter(id => idsSorties.includes(id));
                }

                ecrireVus(v);
                dessinerPastilles();

                // Réaffiche la liste des sorties pour y poser la pastille « Nouveau ».
                if (nouvellesSorties.length) chargerSorties(true);
            }

            let veloActifId = Number(sessionStorage.getItem('maillons_velo_id')) || 1;
            let velosDisponibles = [];
            const fetchOriginal = window.fetch.bind(window);

            // Affectation (et non « function fetch ») : une déclaration de fonction
            // serait remontée avant la ligne fetchOriginal ci-dessus et se rappellerait
            // elle-même sans fin (page bloquée, "Maximum call stack size exceeded").
            window.fetch = function (url, options) {
                let cible = url;
                if (typeof url === 'string' && url.startsWith('/api/')) {
                    const globales = ['/api/velos', '/api/sauvegarde', '/api/reglages'];
                    if (!globales.some(p => url === p || url.startsWith(p + '?'))) {
                        const separateur = url.includes('?') ? '&' : '?';
                        cible = url + separateur + 'velo_id=' + encodeURIComponent(veloActifId);
                    }
                }
                return fetchOriginal(cible, options);
            };

            window.addEventListener('hashchange', afficherPage);
            afficherPage();

            function getHeaders() {
                const code = sessionStorage.getItem('vtt_code') || '';
                return {
                    'Content-Type': 'application/json',
                    'x-code-acces': code
                };
            }

            const TYPES_VELO = {
                vtt_electrique: 'VTT électrique',
                vtt: 'VTT sans moteur',
                enduro: 'Enduro',
                descente: 'Descente',
                gravel: 'Gravel',
                gravel_electrique: 'Gravel électrique',
                route: 'Route',
                route_electrique: 'Route électrique',
                electrique: 'VTT électrique'
            };

            async function chargerVelos() {
                try {
                    const res = await fetch('/api/velos', { headers: getHeaders() });
                    if (!res.ok) throw new Error();
                    velosDisponibles = await res.json();
                    if (!velosDisponibles.length) throw new Error();
                    if (!velosDisponibles.some(v => Number(v.id) === veloActifId)) {
                        veloActifId = Number(velosDisponibles[0].id);
                    }
                    sessionStorage.setItem('maillons_velo_id', String(veloActifId));
                    const select = document.getElementById('selectVelo');
                    select.replaceChildren();
                    velosDisponibles.forEach(v => {
                        const option = document.createElement('option');
                        option.value = String(v.id);
                        const nbAVerifier = Number(v.sorties_a_verifier) || 0;
                        option.textContent = v.nom + ' · ' + (TYPES_VELO[v.type] || v.type)
                            + (nbAVerifier > 0 ? ' · ' + nbAVerifier + ' à vérifier' : '');
                        select.appendChild(option);
                    });
                    select.value = String(veloActifId);
                    afficherNomVeloActif();
                    monterMenuVelo();
                } catch (e) {
                    console.error('Impossible de charger les vélos', e);
                }
            }

            // Photo du vélo affiché : sa photo enregistrée, sinon (CUBE) la photo d'origine, sinon un espace vide.
            const photoCubeOrigine = (document.querySelector('.bike-img') || {}).src || '';
            const photosVelos = {};

            async function afficherPhotoVelo() {
                const photo = document.querySelector('.bike-img');
                const vide = document.getElementById('photoVeloACompleter');
                const velo = velosDisponibles.find(v => Number(v.id) === veloActifId);
                const id = veloActifId;
                const estCube = !velo || Number(velo.id) === 1;
                let source = '';
                if (velo && Number(velo.a_photo) === 1) {
                    if (photosVelos[id] === undefined) {
                        try {
                            const res = await fetch('/api/velos/' + id + '/photo', { headers: getHeaders() });
                            const data = await res.json();
                            photosVelos[id] = res.ok ? (data.photo || '') : '';
                        } catch (e) {
                            photosVelos[id] = '';
                        }
                        if (id !== veloActifId) return;
                    }
                    source = photosVelos[id];
                }
                if (!source && estCube) source = photoCubeOrigine;
                if (photo) {
                    if (source) photo.src = source;
                    photo.style.visibility = source ? 'visible' : 'hidden';
                }
                if (vide) vide.style.display = source ? 'none' : 'flex';
            }

            function choisirPhotoVelo() {
                const champ = document.getElementById('veloPhotoInput');
                if (champ) champ.click();
            }

            // Réduit la photo sur le téléphone (1200 px maxi, JPEG) avant de l'envoyer.
            function reduirePhoto(fichier) {
                return new Promise((resolve, reject) => {
                    const url = URL.createObjectURL(fichier);
                    const img = new Image();
                    img.onload = () => {
                        const maxi = 1200;
                        const echelle = Math.min(1, maxi / Math.max(img.naturalWidth, img.naturalHeight));
                        const canvas = document.createElement('canvas');
                        canvas.width = Math.max(1, Math.round(img.naturalWidth * echelle));
                        canvas.height = Math.max(1, Math.round(img.naturalHeight * echelle));
                        const ctx = canvas.getContext('2d');
                        ctx.fillStyle = '#ffffff';
                        ctx.fillRect(0, 0, canvas.width, canvas.height);
                        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                        URL.revokeObjectURL(url);
                        resolve(canvas.toDataURL('image/jpeg', 0.82));
                    };
                    img.onerror = () => {
                        URL.revokeObjectURL(url);
                        reject(new Error('Cette image ne peut pas être lue.'));
                    };
                    img.src = url;
                });
            }

            function dataUrlEnFichier(dataUrl) {
                const morceaux = dataUrl.split(',');
                const type = morceaux[0].slice(5, morceaux[0].indexOf(';'));
                const binaire = atob(morceaux[1]);
                const octets = new Uint8Array(binaire.length);
                for (let i = 0; i < binaire.length; i++) octets[i] = binaire.charCodeAt(i);
                return new Blob([octets], { type: type });
            }

            // Photo récupérée sur Internet : le serveur va la chercher, puis la page la réduit et l'enregistre.
            async function photoDepuisLien() {
                const message = document.getElementById('nouveauVeloMessage');
                const lien = prompt("Colle ici le lien de la page du vélo (Décathlon, Opticycles...) ou d'une image :");
                if (!lien || !lien.trim()) return;
                try {
                    if (message) message.textContent = 'Recherche de la photo…';
                    const res = await fetch('/api/photo-depuis-lien', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ url: lien.trim() })
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Photo introuvable.');
                    await enregistrerPhotoVelo(dataUrlEnFichier(data.photo));
                } catch (e) {
                    const texte = e.message || 'Photo introuvable.';
                    if (message) message.textContent = texte;
                    alert(texte);
                }
            }

            // ---- Placement automatique des repères sur la photo (vélo de profil) ----
            // Positions types, en fraction du vélo (0 = bord gauche ou haut, 1 = bord droit ou bas),
            // pour un vélo dont le guidon est à droite de la photo.
            const ANCRES_REPERES = {
                pneu_av: [0.97, 0.68], pneu_ar: [0.03, 0.68],
                frein_av: [0.84, 0.76], frein_ar: [0.16, 0.76], levier_frein: [0.76, 0.09],
                moyeu_av: [0.81, 0.70], moyeu_ar: [0.19, 0.70], roue_av: [0.88, 0.48],
                derailleur: [0.22, 0.85], cassette: [0.19, 0.64],
                fourche: [0.80, 0.40], direction: [0.70, 0.22], cintre: [0.75, 0.07],
                selle: [0.32, 0.08], tige: [0.35, 0.22],
                amortisseur: [0.40, 0.44], bielle: [0.38, 0.54],
                pedalier: [0.43, 0.72], pedale: [0.46, 0.86], chaine: [0.31, 0.77],
                moteur: [0.47, 0.62], batterie: [0.58, 0.46]
            };
            // Mots à reconnaître dans le nom de la pièce (sans accents) ; la première règle qui correspond gagne.
            const REGLES_REPERES = [
                [['purge'], 'levier_frein'], [['liquide de frein'], 'levier_frein'],
                [['tubeless', 'liquide'], 'pneu_ar'],
                [['pneu', 'arriere'], 'pneu_ar'], [['pneu'], 'pneu_av'],
                [['frein', 'arriere'], 'frein_ar'], [['disque', 'arriere'], 'frein_ar'], [['plaquette', 'arriere'], 'frein_ar'],
                [['frein'], 'frein_av'], [['disque'], 'frein_av'], [['plaquette'], 'frein_av'],
                [['moyeu'], 'moyeu_av'],
                [['biellette'], 'bielle'], [['roulements de suspension'], 'bielle'],
                [['amortisseur'], 'amortisseur'], [['suspension'], 'amortisseur'],
                [['fourche'], 'fourche'], [['direction'], 'direction'],
                [['serrage'], 'cintre'], [['potence'], 'cintre'], [['cintre'], 'cintre'],
                [['tige de selle'], 'tige'], [['selle'], 'selle'],
                [['cable'], 'chaine'], [['gaine'], 'chaine'],
                [['galet'], 'derailleur'], [['derailleur'], 'derailleur'],
                [['cassette'], 'cassette'], [['pignon'], 'cassette'],
                [['chaine'], 'chaine'], [['transmission'], 'chaine'],
                [['plateau'], 'pedalier'], [['pedalier'], 'pedalier'], [['pedale'], 'pedale'],
                [['moteur'], 'moteur'], [['bosch'], 'moteur'], [['batterie'], 'batterie'],
                [['rayon'], 'roue_av'], [['roue'], 'roue_av']
            ];
            // Petits décalages pour que plusieurs pièces au même endroit ne se superposent pas.
            const DECALAGES_REPERES = [[0, 0], [0.035, -0.04], [-0.035, 0.04], [0.035, 0.04], [-0.035, -0.04], [0.07, 0], [-0.07, 0]];

            function sansAccents(texte) {
                const de = 'àâäéèêëîïôöùûüç';
                const vers = 'aaaeeeeiioouuuc';
                let resultat = '';
                for (const c of String(texte).toLowerCase()) {
                    const i = de.indexOf(c);
                    resultat += i >= 0 ? vers[i] : c;
                }
                return resultat;
            }

            function clePourPiece(nom) {
                const n = sansAccents(nom);
                for (const regle of REGLES_REPERES) {
                    if (regle[0].every(mot => n.includes(mot))) return regle[1];
                }
                return null;
            }

            function chargerImageDepuis(source) {
                return new Promise((ok, ko) => {
                    const img = new Image();
                    img.onload = () => ok(img);
                    img.onerror = () => ko(new Error('Photo illisible.'));
                    img.src = source;
                });
            }

            // Cherche le vélo sur la photo : tout ce qui n'a pas la couleur du fond (photo de boutique, fond clair).
            function boiteDuVelo(img) {
                const largeurImg = img.naturalWidth, hauteurImg = img.naturalHeight;
                const echelle = Math.min(1, 400 / Math.max(largeurImg, hauteurImg));
                const w = Math.max(1, Math.round(largeurImg * echelle));
                const h = Math.max(1, Math.round(hauteurImg * echelle));
                const canvas = document.createElement('canvas');
                canvas.width = w;
                canvas.height = h;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(img, 0, 0, w, h);
                const px = ctx.getImageData(0, 0, w, h).data;
                let r = 0, g = 0, b = 0, n = 0;
                const lire = (x, y) => {
                    const k = (y * w + x) * 4;
                    r += px[k]; g += px[k + 1]; b += px[k + 2]; n++;
                };
                for (let x = 0; x < w; x++) { lire(x, 0); lire(x, h - 1); }
                for (let y = 0; y < h; y++) { lire(0, y); lire(w - 1, y); }
                r /= n; g /= n; b /= n;
                const colonnes = new Array(w).fill(0);
                const lignes = new Array(h).fill(0);
                for (let y = 0; y < h; y++) {
                    for (let x = 0; x < w; x++) {
                        const k = (y * w + x) * 4;
                        const ecart = Math.abs(px[k] - r) + Math.abs(px[k + 1] - g) + Math.abs(px[k + 2] - b);
                        if (ecart > 200) { colonnes[x]++; lignes[y]++; }
                    }
                }
                const seuilC = Math.max(2, Math.round(h * 0.012));
                const seuilL = Math.max(2, Math.round(w * 0.012));
                const x0 = colonnes.findIndex(c => c >= seuilC);
                const y0 = lignes.findIndex(c => c >= seuilL);
                const x1 = w - 1 - colonnes.slice().reverse().findIndex(c => c >= seuilC);
                const y1 = h - 1 - lignes.slice().reverse().findIndex(c => c >= seuilL);
                const complet = { x: 0, y: 0, w: largeurImg, h: hauteurImg, trouve: false };
                if (x0 < 0 || y0 < 0 || x1 <= x0 || y1 <= y0) return complet;
                const boite = {
                    x: x0 / w * largeurImg,
                    y: y0 / h * hauteurImg,
                    w: (x1 - x0 + 1) / w * largeurImg,
                    h: (y1 - y0 + 1) / h * hauteurImg,
                    trouve: true
                };
                if (boite.w < largeurImg * 0.25 || boite.h < hauteurImg * 0.25) return complet;
                return boite;
            }

            // Calcule la place de chaque repère (en % de la zone photo) pour un vélo dont le guidon est à droite ou à gauche.
            function calculerPositionsRepers(liste, boite, largeurImg, hauteurImg, cw, ch, guidonADroite) {
                const echelle = Math.min(cw / largeurImg, ch / hauteurImg);
                const dx = (cw - largeurImg * echelle) / 2;
                const dy = (ch - hauteurImg * echelle) / 2;
                const compte = {};
                return liste.map(c => {
                    const cle = clePourPiece(c.nom);
                    let u, v;
                    if (cle) {
                        const rang = compte[cle] || 0;
                        compte[cle] = rang + 1;
                        const decalage = DECALAGES_REPERES[rang % DECALAGES_REPERES.length];
                        u = ANCRES_REPERES[cle][0] + decalage[0];
                        v = ANCRES_REPERES[cle][1] + decalage[1];
                    } else {
                        // Pièce inconnue : on garde sa place relative sur la photo d'origine du CUBE.
                        u = (Number(c.pos_x) - 6) / 90;
                        v = (Number(c.pos_y) - 8) / 84;
                    }
                    u = Math.max(0, Math.min(1, u));
                    v = Math.max(0, Math.min(1, v));
                    if (!guidonADroite) u = 1 - u;
                    const x = (dx + (boite.x + u * boite.w) * echelle) / cw * 100;
                    const y = (dy + (boite.y + v * boite.h) * echelle) / ch * 100;
                    return {
                        composant_id: c.id,
                        pos_x: Math.max(0, Math.min(100, Math.round(x))),
                        pos_y: Math.max(0, Math.min(100, Math.round(y)))
                    };
                });
            }

            async function placerRepresAuto(guidonADroite, silencieux) {
                const message = document.getElementById('nouveauVeloMessage');
                const id = veloActifId;
                const velo = velosDisponibles.find(v => Number(v.id) === id);
                try {
                    if (!velo || Number(velo.a_photo) !== 1) {
                        if (!silencieux) alert("Ajoute d'abord une photo à ce vélo.");
                        return false;
                    }
                    if (photosVelos[id] === undefined) await afficherPhotoVelo();
                    const source = photosVelos[id];
                    const liste = Array.isArray(etatComposants) ? etatComposants : [];
                    if (!source || !liste.length || id !== veloActifId) return false;
                    const img = await chargerImageDepuis(source);
                    const boite = boiteDuVelo(img);
                    const cadre = document.querySelector('.bike-container');
                    let cw = cadre ? cadre.clientWidth : 0;
                    let ch = cadre ? cadre.clientHeight : 0;
                    if (!cw || !ch) { cw = 16; ch = 9; }
                    const positions = calculerPositionsRepers(liste, boite, img.naturalWidth, img.naturalHeight, cw, ch, guidonADroite);
                    const res = await fetch('/api/velos/' + id + '/positions', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ positions: positions })
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Placement impossible.');
                    await chargerEtat();
                    if (message) {
                        message.textContent = boite.trouve
                            ? 'Repères placés automatiquement. Ajuste-les avec « Placer les repères » si besoin.'
                            : 'Repères placés au mieux (le vélo n’a pas pu être isolé du fond). Ajuste-les avec « Placer les repères ».';
                    }
                    return true;
                } catch (e) {
                    if (!silencieux) alert(e.message || 'Placement impossible.');
                    return false;
                }
            }

            async function enregistrerPhotoVelo(fichier) {
                const message = document.getElementById('nouveauVeloMessage');
                const id = veloActifId;
                try {
                    message.textContent = 'Enregistrement de la photo…';
                    const photo = await reduirePhoto(fichier);
                    const res = await fetch('/api/velos/' + id + '/photo', {
                        method: 'PUT',
                        headers: getHeaders(),
                        body: JSON.stringify({ photo: photo })
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Enregistrement impossible.');
                    photosVelos[id] = photo;
                    const velo = velosDisponibles.find(v => Number(v.id) === id);
                    if (velo) velo.a_photo = 1;
                    message.textContent = 'Photo enregistrée.';
                    await afficherPhotoVelo();
                    monterMenuVelo();
                    await placerRepresAuto(true, true);
                } catch (e) {
                    message.textContent = e.message || 'Enregistrement impossible.';
                    alert(message.textContent);
                }
            }

            async function supprimerPhotoVelo() {
                const message = document.getElementById('nouveauVeloMessage');
                const id = veloActifId;
                const velo = velosDisponibles.find(v => Number(v.id) === id);
                if (!velo || Number(velo.a_photo) !== 1) return;
                if (!confirm('Supprimer la photo de « ' + velo.nom + ' » ?')) return;
                try {
                    const res = await fetch('/api/velos/' + id + '/photo', {
                        method: 'DELETE',
                        headers: getHeaders()
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Suppression impossible.');
                    delete photosVelos[id];
                    velo.a_photo = 0;
                    message.textContent = 'Photo supprimée.';
                    afficherPhotoVelo();
                    monterMenuVelo();
                } catch (e) {
                    message.textContent = e.message || 'Suppression impossible.';
                }
            }

            document.addEventListener('DOMContentLoaded', () => {
                const champ = document.getElementById('veloPhotoInput');
                if (!champ) return;
                champ.addEventListener('change', () => {
                    const fichier = champ.files && champ.files[0];
                    champ.value = '';
                    if (fichier) enregistrerPhotoVelo(fichier);
                });
            });

            // Menu « trois points » de la carte « Ajouter un vélo » : actions du vélo affiché.
            // Le CUBE (vélo n° 1) est protégé : il ne peut ni être supprimé ni passer par cette fiche.
            function monterMenuVelo() {
                const zone = document.getElementById('veloMenuZone');
                if (!zone) return;
                zone.replaceChildren();
                const velo = velosDisponibles.find(v => Number(v.id) === veloActifId);
                const estCube = !velo || Number(velo.id) === 1;
                const nom = velo ? String(velo.nom) : 'ce vélo';
                const court = nom.length > 22 ? nom.slice(0, 21) + '…' : nom;
                ajouterMenuPoints(zone, 'Actions du vélo', [
                    { icone: 'crayon', texte: 'Modifier la fiche de « ' + court + ' »', desactive: estCube, action: modifierFicheVelo },
                    { icone: 'appareil', texte: velo && Number(velo.a_photo) === 1 ? 'Changer la photo' : 'Ajouter une photo', action: choisirPhotoVelo },
                    { icone: 'lien', texte: 'Photo depuis un lien…', action: photoDepuisLien },
                    { icone: 'poubelle', texte: 'Supprimer la photo', danger: true, desactive: !velo || Number(velo.a_photo) !== 1, action: supprimerPhotoVelo },
                    { icone: 'repere', texte: 'Replacer les repères (guidon à droite)', desactive: !velo || Number(velo.a_photo) !== 1, action: () => placerRepresAuto(true, false) },
                    { icone: 'repere', texte: 'Replacer les repères (guidon à gauche)', desactive: !velo || Number(velo.a_photo) !== 1, action: () => placerRepresAuto(false, false) },
                    { icone: 'poubelle', texte: 'Supprimer « ' + court + ' »', danger: true, desactive: estCube, action: supprimerVelo }
                ]);
            }

            async function supprimerVelo() {
                const velo = velosDisponibles.find(v => Number(v.id) === veloActifId);
                if (!velo || Number(velo.id) === 1) return;
                const message = document.getElementById('nouveauVeloMessage');
                try {
                    const res = await fetch('/api/velos/' + velo.id + '/contenu', { headers: getHeaders() });
                    const contenu = await res.json();
                    if (!res.ok) throw new Error(contenu.detail || 'Lecture impossible.');
                    const saut = String.fromCharCode(10);
                    const texte = 'Supprimer pour de bon « ' + velo.nom + ' » ?' + saut + saut
                        + 'Seront effacés définitivement :' + saut
                        + '- ' + contenu.composants + ' pièce(s) et entretien(s)' + saut
                        + '- ' + contenu.historique + ' intervention(s) de l’historique' + saut
                        + '- ' + contenu.sorties + ' sortie(s)' + saut + saut
                        + 'Impossible de revenir en arrière.';
                    if (!confirm(texte)) return;
                    const sup = await fetch('/api/velos/' + velo.id + '/supprimer', {
                        method: 'POST',
                        headers: getHeaders()
                    });
                    const data = await sup.json();
                    if (!sup.ok) throw new Error(data.detail || 'Suppression impossible.');
                    try { localStorage.removeItem('maillons_vus_' + velo.id); } catch (e) {}
                    annulerEditionFiche();
                    veloActifId = 1;
                    sessionStorage.setItem('maillons_velo_id', '1');
                    await chargerVelos();
                    await rechargerDonneesVelo();
                    message.textContent = '« ' + velo.nom + ' » a été supprimé.';
                } catch (e) {
                    message.textContent = e.message || 'Suppression impossible.';
                }
            }

            function afficherNomVeloActif() {
                const velo = velosDisponibles.find(v => Number(v.id) === veloActifId);
                const titre = document.getElementById('headerVeloNom');
                if (titre) titre.textContent = velo ? velo.nom : 'Mon vélo';
                const estCube = !velo || Number(velo.id) === 1;
                afficherPhotoVelo();
                const ficheCube = document.getElementById('ficheTechniqueCube');
                const ficheGenerique = document.getElementById('ficheTechniqueAutre');
                if (ficheCube) ficheCube.style.display = estCube ? '' : 'none';
                if (ficheGenerique) ficheGenerique.style.display = estCube ? 'none' : 'block';
                const boutonModifierFiche = document.getElementById('btnModifierFicheVelo');
                if (boutonModifierFiche) boutonModifierFiche.style.display = estCube ? 'none' : 'block';
                const ficheNom = document.getElementById('ficheAutreNom');
                const ficheType = document.getElementById('ficheAutreType');
                const ficheMarque = document.getElementById('ficheAutreMarque');
                const ficheModele = document.getElementById('ficheAutreModele');
                const ficheAnnee = document.getElementById('ficheAutreAnnee');
                const ficheTaille = document.getElementById('ficheAutreTaille');
                const ficheMoteur = document.getElementById('ficheAutreMoteur');
                const ficheBatterie = document.getElementById('ficheAutreBatterie');
                if (velo) {
                    if (ficheNom) ficheNom.textContent = velo.nom || 'Mon vélo';
                    if (ficheType) ficheType.textContent = TYPES_VELO[velo.type] || velo.type || 'Non renseigné';
                    if (ficheMarque) ficheMarque.textContent = velo.marque || 'Non renseignée';
                    if (ficheModele) ficheModele.textContent = velo.modele || 'Non renseigné';
                    const ficheFreins = document.getElementById('ficheAutreFreins');
                    if (ficheFreins) ficheFreins.textContent = velo.freins === 'patins' ? 'Freins à patins' : velo.freins === 'disque_hydraulique' ? 'Freins à disque hydrauliques' : velo.freins === 'disque_mecanique' ? 'Freins à disque mécaniques' : velo.freins === 'disque' ? 'Freins à disque, type à confirmer' : 'À confirmer';
                    const ficheSuspension = document.getElementById('ficheAutreSuspension');
                    if (ficheSuspension) ficheSuspension.textContent = ({ rigide: 'Sans suspension', avant: 'Fourche suspendue', double: 'Fourche et amortisseur arrière' })[velo.suspension] || 'À confirmer';
                    const ficheTubeless = document.getElementById('ficheAutreTubeless');
                    if (ficheTubeless) ficheTubeless.textContent = velo.tubeless === 'tubeless' ? 'Tubeless' : velo.tubeless === 'chambre' ? 'Avec chambre à air' : 'À confirmer';
                    const ficheTransmission = document.getElementById('ficheAutreTransmission');
                    if (ficheTransmission) ficheTransmission.textContent = velo.transmission === 'courroie' ? 'Courroie' : velo.transmission === 'chaine' ? 'Chaîne et dérailleur' : 'À confirmer';
                    if (ficheAnnee) ficheAnnee.textContent = velo.annee || 'Non renseignée';
                    if (ficheTaille) ficheTaille.textContent = velo.taille || 'Non renseignée';
                    if (ficheMoteur) ficheMoteur.textContent = velo.moteur || 'Non concerné ou non renseigné';
                    if (ficheBatterie) ficheBatterie.textContent = velo.batterie || 'Non renseignée';
                    const estElectriqueVelo = ['vtt_electrique', 'gravel_electrique', 'route_electrique', 'electrique'].includes(velo.type);
                    const ligneMoteur = document.getElementById('ficheAutreMoteurLigne');
                    const ligneBatterie = document.getElementById('ficheAutreBatterieLigne');
                    if (ligneMoteur) ligneMoteur.style.display = estElectriqueVelo ? '' : 'none';
                    if (ligneBatterie) ligneBatterie.style.display = estElectriqueVelo ? '' : 'none';
                }
                const electriques = document.getElementById('champsElectriques');
                const type = document.getElementById('nouveauVeloType');
                if (electriques && type) {
                    electriques.style.display = ['vtt_electrique', 'gravel_electrique', 'route_electrique'].includes(type.value) ? 'block' : 'none';
                }
            }

            async function rechargerDonneesVelo() {
                sortiesConnues = [];
                historiqueDonnees = [];
                etatComposants = [];
                histSelection.clear();
                await chargerVelos();
                await chargerEtat();
                afficherNomVeloActif();
            }

            async function changerVeloActif() {
                const prochain = Number(document.getElementById('selectVelo').value);
                if (!prochain || prochain === veloActifId) return;
                veloActifId = prochain;
                sessionStorage.setItem('maillons_velo_id', String(veloActifId));
                await rechargerDonneesVelo();
            }

            document.getElementById('selectVelo').addEventListener('change', changerVeloActif);
            document.getElementById('nouveauVeloType').addEventListener('change', afficherNomVeloActif);
            let veloEditionId = null;

            function modifierFicheVelo() {
                const velo = velosDisponibles.find(v => Number(v.id) === veloActifId);
                if (!velo || Number(velo.id) === 1) return;
                const formulaire = document.getElementById('formAjouterVelo');
                formulaire.elements.nom.value = velo.nom || '';
                formulaire.elements.type.value = velo.type || 'vtt';
                formulaire.elements.type.disabled = true;
                formulaire.elements.marque.value = velo.marque || '';
                formulaire.elements.modele.value = velo.modele || '';
                formulaire.elements.annee.value = velo.annee || '';
                formulaire.elements.taille.value = velo.taille || '';
                formulaire.elements.date_achat.value = velo.date_achat || '';
                formulaire.elements.moteur.value = velo.moteur || '';
                formulaire.elements.batterie.value = velo.batterie || '';
                if (formulaire.elements.freins) formulaire.elements.freins.value = velo.freins || 'inconnu';
                if (formulaire.elements.suspension) formulaire.elements.suspension.value = velo.suspension || 'inconnu';
                if (formulaire.elements.tubeless) formulaire.elements.tubeless.value = velo.tubeless || 'inconnu';
                if (formulaire.elements.transmission) formulaire.elements.transmission.value = velo.transmission || 'inconnu';
                veloEditionId = Number(velo.id);
                document.getElementById('titreFormVelo').textContent = 'Modifier la fiche du vélo';
                document.getElementById('boutonCreerVelo').textContent = 'Enregistrer la fiche';
                document.getElementById('btnAnnulerEditionFiche').style.display = 'block';
                document.getElementById('nouveauVeloMessage').textContent = '';
                location.hash = 'reglages';
                afficherPage();
                document.getElementById('blocAjouterVelo').scrollIntoView({ behavior: 'smooth', block: 'start' });
            }

            function annulerEditionFiche() {
                veloEditionId = null;
                const formulaire = document.getElementById('formAjouterVelo');
                formulaire.reset();
                formulaire.elements.type.disabled = false;
                document.getElementById('titreFormVelo').textContent = 'Ajouter un vélo';
                document.getElementById('boutonCreerVelo').textContent = 'Créer le vélo';
                document.getElementById('btnAnnulerEditionFiche').style.display = 'none';
                document.getElementById('nouveauVeloMessage').textContent = '';
                afficherNomVeloActif();
            }

            async function rechercherFicheVelo(url = null) {
                const entree = document.getElementById('veloRechercheFiche');
                const zone = document.getElementById('resultatsRechercheFiche');
                const recherche = (entree.value || '').trim();
                if (!url && !recherche) {
                    zone.textContent = 'Saisis le modèle du vélo ou le lien de sa fiche constructeur.';
                    return;
                }
                zone.textContent = 'Recherche en cours…';
                try {
                    const res = await fetch('/api/recherche-fiche-velo', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ recherche: url ? '' : recherche, url: url || '' })
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Recherche impossible.');
                    if (data.candidats) {
                        zone.textContent = 'Choisis la fiche qui correspond exactement à ton vélo :';
                        data.candidats.forEach(candidat => {
                            const bouton = document.createElement('button');
                            bouton.type = 'button';
                            bouton.textContent = candidat.titre;
                            bouton.style.width = '100%';
                            bouton.style.margin = '4px 0';
                            bouton.addEventListener('click', () => rechercherFicheVelo(candidat.url));
                            zone.appendChild(bouton);
                        });
                        return;
                    }
                    if (data.marque) document.getElementById('nouveauVeloMarque').value = data.marque;
                    if (data.modele) document.getElementById('nouveauVeloModele').value = data.modele;
                    if (data.annee) document.getElementById('nouveauVeloAnnee').value = data.annee;
                    if (data.freins) document.getElementById('nouveauVeloFreins').value = data.freins;
                    if (data.suspension) document.getElementById('nouveauVeloSuspension').value = data.suspension;
                    if (data.tubeless) document.getElementById('nouveauVeloTubeless').value = data.tubeless;
                    if (data.transmission) document.getElementById('nouveauVeloTransmission').value = data.transmission;
                    if (data.type) document.getElementById('nouveauVeloType').value = data.type;
                    if (data.moteur) document.getElementById('nouveauVeloMoteur').value = data.moteur;
                    if (data.batterie) document.getElementById('nouveauVeloBatterie').value = data.batterie;
                    afficherNomVeloActif();
                    zone.textContent = 'Source : ' + data.source + '. Vérifie chaque champ avant d’enregistrer. Les caractéristiques non trouvées restent à confirmer.';
                    if (Array.isArray(data.composants_techniques)) {
                        const titre = document.createElement('h4');
                        titre.textContent = 'Composants techniques repérés';
                        zone.appendChild(titre);
                        const liste = document.createElement('ul');
                        liste.style.paddingLeft = '20px';
                        data.composants_techniques.forEach(piece => {
                            const ligne = document.createElement('li');
                            const nom = piece.categorie.replaceAll('_', ' ');
                            ligne.textContent = nom + ' : ' + (piece.nom || 'non trouvé sur cette page')
                                + (piece.statut === 'a_confirmer' ? ' · plusieurs variantes à confirmer' : '');
                            liste.appendChild(ligne);
                        });
                        zone.appendChild(liste);
                        const note = document.createElement('p');
                        note.textContent = 'Les variantes possibles sont affichées séparément. Une pièce non trouvée n’est jamais déduite automatiquement.';
                        zone.appendChild(note);
                    }
                } catch (e) {
                    zone.textContent = e.message || 'Recherche impossible. Tu peux compléter la fiche manuellement.';
                }
            }
            document.getElementById('boutonRechercheFiche').addEventListener('click', () => rechercherFicheVelo());

            document.getElementById('formAjouterVelo').addEventListener('submit', async event => {
                event.preventDefault();
                const formulaire = event.currentTarget;
                const message = document.getElementById('nouveauVeloMessage');
                const type = document.getElementById('nouveauVeloType').value;
                const electrique = ['vtt_electrique', 'gravel_electrique', 'route_electrique', 'electrique'].includes(type);
                const valeurs = Object.fromEntries(new FormData(formulaire).entries());
                if (!electrique) {
                    delete valeurs.moteur;
                    delete valeurs.batterie;
                }
                if (valeurs.annee === '') delete valeurs.annee;
                else if (valeurs.annee) valeurs.annee = Number(valeurs.annee);
                Object.keys(valeurs).forEach(cle => {
                    if (typeof valeurs[cle] === 'string' && valeurs[cle].trim() === '') delete valeurs[cle];
                });
                try {
                    const edition = veloEditionId !== null;
                    const adresse = edition ? '/api/velos/' + veloEditionId : '/api/velos';
                    const res = await fetch(adresse, {
                        method: edition ? 'PUT' : 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify(valeurs)
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Enregistrement impossible.');
                    if (data.velo_id) veloActifId = Number(data.velo_id);
                    sessionStorage.setItem('maillons_velo_id', String(veloActifId));
                    annulerEditionFiche();
                    message.textContent = edition ? 'Fiche mise à jour.' : 'Vélo créé. Chargement de ses données…';
                    await chargerVelos();
                    await rechargerDonneesVelo();
                } catch (e) {
                    message.textContent = e.message || 'Création impossible.';
                }
            });

            async function tenterConnexion() {
                const code = document.getElementById('authInput').value;
                sessionStorage.setItem('vtt_code', code);
                
                try {
                    const res = await fetch('/api/etat', { headers: getHeaders() });
                    if (res.ok) {
                        document.getElementById('authScreen').style.display = 'none';
                        await chargerVelos();
                        chargerEtat();
                    } else {
                        document.getElementById('authError').textContent =
                            res.status === 503
                                ? "Le code d'accès n'est pas encore réglé sur le serveur."
                                : "Code incorrect.";
                    }
                } catch (e) {
                    document.getElementById('authError').textContent = "Erreur de connexion.";
                }
            }

            document.getElementById('authInput').addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    tenterConnexion();
                }
            });

            // Vérifie si un code est déjà en mémoire
            if (sessionStorage.getItem('vtt_code')) {
                fetch('/api/etat', { headers: getHeaders() }).then(async res => {
                    if (res.ok) {
                        document.getElementById('authScreen').style.display = 'none';
                        await chargerVelos();
                        await chargerEtat();
                    }
                });
            }

            const MR_GROUPES = [
                { titre: '🚲 Mon vélo', champs: [
                    ['taille_cadre', 'Taille du cadre'] ] },
                { titre: '🛞 Pneus', champs: [
                    ['pneu_av', 'Avant (bar)'], ['pneu_ar', 'Arrière (bar)'] ] },
                { titre: '🔱 Fourche', champs: [
                    ['fourche_psi', 'Pression (psi)'], ['fourche_sag', 'SAG (%)'],
                    ['fourche_detente', 'Détente (clics)'], ['fourche_bagues', 'Bagues de volume'] ] },
                { titre: '🌀 Amortisseur', champs: [
                    ['amorto_psi', 'Pression (psi)'], ['amorto_sag', 'SAG (%)'],
                    ['amorto_detente', 'Détente (clics)'], ['amorto_bagues', 'Bagues de volume'] ] },
                { titre: '📝 Remarque', champs: [
                    ['notes', 'Note libre'] ] }
            ];
            const ICONES = {
                cle: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
                echange: '<path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/><path d="M3 21v-5h5"/>',
                crayon: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
                poubelle: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/>',
                repere: '<path d="M12 21s7-6.2 7-11.5A7 7 0 0 0 5 9.5C5 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
                valider: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
                fermer: '<path d="M6 6l12 12M18 6L6 18"/>',
                bas: '<path d="M6 9l6 6 6-6"/>',
                haut: '<path d="M6 15l6-6 6 6"/>'
            };

            function remplirBouton(el, icone, texte) {
                el.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">'
                    + ICONES[icone] + '</svg><span></span>';
                el.lastChild.textContent = texte;
            }

            ICONES.points = '<circle cx="12" cy="5" r="1.9" fill="currentColor"/><circle cx="12" cy="12" r="1.9" fill="currentColor"/><circle cx="12" cy="19" r="1.9" fill="currentColor"/>';
            ICONES.lien = '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>';
            ICONES.appareil = '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>';
            ICONES.video = '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3"/>';
            ICONES.montagne = '<path d="M2 20l6.5-11 4 6.5L15 12l7 8z"/><path d="M8.5 9l1.8 3"/><circle cx="18" cy="5.5" r="1.6" fill="currentColor"/>';
            ICONES.lecture = '<path d="M8 5v14l11-7z" fill="currentColor"/>';
            ICONES.pause = '<path d="M8 5v14M16 5v14"/>';
            ICONES.vitesse = '<path d="M5 6l6 6-6 6M13 6l6 6-6 6"/>';
            ICONES.incl = '<path d="M3 18l9-12 9 12z"/>';
            ICONES.fond = '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>';
            ICONES.enreg = '<circle cx="12" cy="12" r="6" fill="currentColor"/>';
            ICONES.soleil = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
            ICONES.lune = '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>';
            ICONES.auto = '<circle cx="12" cy="12" r="9"/><path d="M12 3v18"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>';
            ICONES.filtre = '<path d="M3.5 5h17l-6.5 8v6l-4-2v-4z"/>';
            ICONES.vide = '';
            ICONES.partage = '<circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="M8.2 10.8l7.6-4.4M8.2 13.2l7.6 4.4"/>';
            ICONES.coeur = '<path d="M12 20.5s-7.5-4.8-9.3-9.6C1.5 7.5 3.6 4.5 6.9 4.5c2.1 0 3.8 1 5.1 3 1.3-2 3-3 5.1-3 3.3 0 5.4 3 4.2 6.4-1.8 4.8-9.3 9.6-9.3 9.6z"/>';
            ICONES.importer = '<path d="M12 15V4"/><path d="M7 9l5-5 5 5"/><path d="M5 20h14"/>';
            ICONES.plus = '<path d="M12 5v14M5 12h14"/>';
            ICONES.telecharger = '<path d="M12 4v11"/><path d="M7 11l5 5 5-5"/><path d="M5 20h14"/>';
            ICONES.cases = '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>';

            function themeChoisi() {
                try {
                    const t = localStorage.getItem('maillon_theme');
                    if (t === 'light' || t === 'dark') return t;
                } catch (e) {}
                return 'auto';
            }

            function appliquerTheme(choix) {
                const racine = document.documentElement;
                if (choix === 'light' || choix === 'dark') {
                    racine.setAttribute('data-theme', choix);
                } else {
                    racine.removeAttribute('data-theme');
                }
                try {
                    if (choix === 'auto') {
                        localStorage.removeItem('maillon_theme');
                    } else {
                        localStorage.setItem('maillon_theme', choix);
                    }
                } catch (e) {}

                const clair = choix === 'light' ||
                    (choix === 'auto' && window.matchMedia &&
                     window.matchMedia('(prefers-color-scheme: light)').matches);
                const meta = document.querySelector('meta[name="theme-color"]');
                if (meta) meta.setAttribute('content', clair ? '#eef4f3' : '#0b171c');

                document.querySelectorAll('#segmentsTheme button').forEach(b => {
                    b.classList.toggle('actif', b.dataset.themeChoix === choix);
                });
            }

            document.querySelectorAll('#segmentsTheme button').forEach(b => {
                const choix = b.dataset.themeChoix;
                remplirBouton(b, choix === 'light' ? 'soleil' : choix === 'dark' ? 'lune' : 'auto',
                    choix === 'light' ? 'Jour' : choix === 'dark' ? 'Nuit' : 'Auto');
                b.addEventListener('click', () => appliquerTheme(choix));
            });
            appliquerTheme(themeChoisi());

            const PALETTES = ['turquoise', 'bleu', 'vert', 'orange', 'rose', 'violet'];

            function paletteChoisie() {
                try {
                    const p = localStorage.getItem('maillon_palette');
                    if (PALETTES.indexOf(p) >= 0) return p;
                } catch (e) {}
                return 'turquoise';
            }

            function appliquerPalette(nom) {
                const racine = document.documentElement;
                if (nom === 'turquoise') {
                    racine.removeAttribute('data-palette');
                } else {
                    racine.setAttribute('data-palette', nom);
                }
                try {
                    if (nom === 'turquoise') {
                        localStorage.removeItem('maillon_palette');
                    } else {
                        localStorage.setItem('maillon_palette', nom);
                    }
                } catch (e) {}
                document.querySelectorAll('#palettesChoix button').forEach(b => {
                    b.classList.toggle('actif', b.dataset.paletteChoix === nom);
                });
            }

            document.querySelectorAll('#palettesChoix button').forEach(b => {
                b.addEventListener('click', () => appliquerPalette(b.dataset.paletteChoix));
            });
            appliquerPalette(paletteChoisie());

            const UNITES_DEFAUT = { distance: 'km', altitude: 'm', vitesse: 'kmh', temperature: 'c', heure: '24' };
            const UNITES_VALIDES = {
                distance: ['km', 'mi'],
                altitude: ['m', 'ft'],
                vitesse: ['kmh', 'mph'],
                temperature: ['c', 'f'],
                heure: ['24', '12']
            };

            function lireUnites() {
                try {
                    const u = JSON.parse(localStorage.getItem('maillon_unites') || '{}');
                    const resultat = {};
                    Object.keys(UNITES_DEFAUT).forEach(cle => {
                        resultat[cle] = UNITES_VALIDES[cle].includes(u[cle]) ? u[cle] : UNITES_DEFAUT[cle];
                    });
                    return resultat;
                } catch (e) {
                    return { ...UNITES_DEFAUT };
                }
            }

            let unites = lireUnites();

            function appliquerUnites() {
                ['Distance', 'Altitude', 'Vitesse', 'Temperature', 'Heure'].forEach(nom => {
                    const cle = nom.toLowerCase();
                    const el = document.getElementById('unites' + nom);
                    if (el) el.value = unites[cle];
                });
                try { localStorage.setItem('maillon_unites', JSON.stringify(unites)); } catch (e) {}
            }

            ['Distance', 'Altitude', 'Vitesse', 'Temperature', 'Heure'].forEach(nom => {
                const el = document.getElementById('unites' + nom);
                if (el) el.addEventListener('change', () => {
                    const cle = nom.toLowerCase();
                    unites[cle] = UNITES_VALIDES[cle].includes(el.value) ? el.value : UNITES_DEFAUT[cle];
                    appliquerUnites();
                    if (sortiesConnues.length) chargerSorties(true);
                });
            });

            function fmtDistance(km, decimales) {
                const valeur = Number(km) * (unites.distance === 'mi' ? 0.621371 : 1);
                const unite = unites.distance === 'mi' ? 'mi' : 'km';
                let texte;
                if (decimales === undefined) {
                    texte = unites.distance === 'mi' ? String(Number(valeur.toFixed(2))) : String(km);
                } else {
                    texte = fmtFr(valeur, decimales);
                }
                return { valeur: valeur, unite: unite, texte: texte, complet: texte + ' ' + unite };
            }

            function fmtDistanceProfil(km) {
                const valeur = Number(km) * (unites.distance === 'mi' ? 0.621371 : 1);
                return valeur.toFixed(1) + (unites.distance === 'mi' ? ' mi' : ' km');
            }

            function fmtAltitude(m, decimales) {
                const valeur = Number(m) * (unites.altitude === 'ft' ? 3.28084 : 1);
                const unite = unites.altitude === 'ft' ? 'ft' : 'm';
                const texte = decimales === undefined ? String(Math.round(valeur)) : fmtFr(valeur, decimales);
                return { valeur: valeur, unite: unite, texte: texte, complet: texte + ' ' + unite };
            }

            function fmtVitesse(kmh, decimales) {
                const valeur = Number(kmh) * (unites.vitesse === 'mph' ? 0.621371 : 1);
                const unite = unites.vitesse === 'mph' ? 'mph' : 'km/h';
                const texte = decimales === undefined ? String(unites.vitesse === 'mph' ? Number(valeur.toFixed(2)) : kmh) : fmtFr(valeur, decimales);
                return { valeur: valeur, unite: unite, texte: texte, complet: texte + ' ' + unite };
            }

            function fmtTemperature(c, decimales) {
                const valeur = unites.temperature === 'f' ? Number(c) * 9 / 5 + 32 : Number(c);
                const unite = unites.temperature === 'f' ? '°F' : '°C';
                return { valeur: valeur, unite: unite, texte: fmtFr(valeur, decimales === undefined ? 0 : decimales), complet: fmtFr(valeur, decimales === undefined ? 0 : decimales) + ' ' + unite };
            }

            function formaterHeure(date) {
                return date.toLocaleTimeString('fr-FR', unites.heure === '12'
                    ? { hour: 'numeric', minute: '2-digit', hour12: true }
                    : { hour: '2-digit', minute: '2-digit', hour12: false });
            }

            appliquerUnites();
            if (window.matchMedia) {
                window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
                    if (themeChoisi() === 'auto') appliquerTheme('auto');
                });
            }

            let mrValeurs = {};
            let mrMaj = null;
            let mrEdition = false;

            function afficherReglagesVelo() {
                const zone = document.getElementById('mrContenu');
                zone.innerHTML = '';

                MR_GROUPES.forEach(g => {
                    const bloc = document.createElement('div');
                    bloc.className = 'mr-groupe';
                    const h = document.createElement('h3');
                    h.textContent = g.titre;
                    bloc.appendChild(h);

                    g.champs.forEach(([cle, libelle]) => {
                        const ligne = document.createElement('div');
                        ligne.className = 'mr-ligne';
                        const l = document.createElement('span');
                        l.textContent = libelle;
                        ligne.appendChild(l);

                        if (mrEdition) {
                            let inp;
                            if (cle === 'taille_cadre') {
                                inp = document.createElement('select');
                                ['S', 'M', 'L', 'XL'].forEach(val => {
                                    const opt = document.createElement('option');
                                    opt.value = val;
                                    opt.textContent = val;
                                    inp.appendChild(opt);
                                });
                            } else {
                                inp = document.createElement('input');
                                inp.type = 'text';
                                inp.inputMode = cle === 'notes' ? 'text' : 'decimal';
                                inp.maxLength = 200;
                            }
                            inp.id = 'mr-' + cle;
                            inp.value = mrValeurs[cle] || (cle === 'taille_cadre' ? 'M' : '');
                            if (cle === 'taille_cadre') inp.addEventListener('change', () => mettreAJourTailleRoues(inp.value));
                            if (cle === 'notes') { inp.style.width = '100%'; inp.style.flex = '1'; inp.style.maxWidth = '200px'; }
                            ligne.appendChild(inp);
                        } else {
                            const v = document.createElement('b');
                            v.textContent = mrValeurs[cle] || (cle === 'taille_cadre' ? 'M' : '—');
                            ligne.appendChild(v);
                        }
                        bloc.appendChild(ligne);
                    });
                    zone.appendChild(bloc);
                });

                document.getElementById('mrActions').style.display =
                    mrEdition ? 'flex' : 'none';
                document.getElementById('mrModifier').style.display =
                    mrEdition ? 'none' : 'inline-flex';

                let txt = '';
                if (mrMaj) {
                    const p = mrMaj.split('-');
                    txt = `Mis à jour le ${p[2]}/${p[1]}/${p[0]}`;
                } else if (!mrEdition) {
                    txt = 'Aucune valeur notée pour le moment.';
                }
                document.getElementById('mrDate').textContent = txt;
            }

            function mettreAJourTailleRoues(tailleChoisie) {
                const taille = tailleChoisie || mrValeurs.taille_cadre || 'M';
                const roues = document.getElementById('ftvTailleRoues');
                if (roues) roues.textContent = taille === 'S' ? '27,5 pouces' : '29 pouces';
                const selectFiche = document.getElementById('ftvTailleCadre');
                if (selectFiche && selectFiche.value !== taille) selectFiche.value = taille;
            }

            async function enregistrerTailleDepuisFiche(taille) {
                const valeurs = { ...mrValeurs, taille_cadre: taille };
                try {
                    const res = await fetch('/api/mes-reglages', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ valeurs })
                    });
                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }
                    if (!res.ok) throw new Error();
                    mrValeurs = valeurs;
                    mrMaj = new Date().toISOString();
                    mettreAJourTailleRoues(taille);
                    afficherReglagesVelo();
                } catch (e) {
                    alert("Impossible d'enregistrer la taille du cadre.");
                    mettreAJourTailleRoues(mrValeurs.taille_cadre || 'M');
                }
            }

            document.getElementById('ftvTailleCadre')?.addEventListener('change', e => {
                enregistrerTailleDepuisFiche(e.target.value);
            });

            async function chargerReglagesVelo() {
                try {
                    const res = await fetch('/api/mes-reglages', { headers: getHeaders() });
                    if (!res.ok) throw new Error();
                    const d = await res.json();
                    mrValeurs = d.valeurs || {};
                    mrMaj = d.maj_le || null;
                } catch (e) {
                    mrValeurs = {};
                }
                if (!mrEdition) afficherReglagesVelo();
                mettreAJourTailleRoues();
            }

            function modifierReglagesVelo() {
                mrEdition = true;
                afficherReglagesVelo();
            }

            function annulerReglagesVelo() {
                mrEdition = false;
                afficherReglagesVelo();
            }

            async function enregistrerReglagesVelo() {
                const valeurs = {};
                MR_GROUPES.forEach(g => g.champs.forEach(([cle]) => {
                    valeurs[cle] = document.getElementById('mr-' + cle).value;
                }));

                try {
                    const res = await fetch('/api/mes-reglages', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ valeurs })
                    });
                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }
                    if (!res.ok) throw new Error();
                    mrEdition = false;
                    await chargerReglagesVelo();
                } catch (e) {
                    alert("Impossible d'enregistrer les réglages.");
                }
            }

            remplirBouton(document.getElementById('btnPlacer'), 'repere', 'Placer les repères');
            remplirBouton(document.getElementById('mrModifier'), 'crayon', 'Modifier');
            remplirBouton(document.getElementById('mrOk'), 'valider', 'Enregistrer');
            remplirBouton(document.getElementById('mrAnnuler'), 'fermer', 'Annuler');

            // Blocs repliables de l'onglet Réglages (fermés au départ).
            function basculerSection(section) {
                const ouverte = !section.classList.contains('ouverte');
                section.classList.toggle('ouverte', ouverte);
                const entete = section.querySelector('.mr-entete');
                if (entete) entete.setAttribute('aria-expanded', String(ouverte));
            }

            document.querySelectorAll('.mr-repliable').forEach(section => {
                const entete = section.querySelector('.mr-entete');
                const chevron = section.querySelector('.mr-chevron');
                chevron.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">'
                    + ICONES.bas + '</svg>';
                entete.addEventListener('click', () => basculerSection(section));
            });

            const cartesOuvertes = new Set();

            // [difficulté, à quoi ça sert, étapes, outils, recherche YouTube]
            const GUIDES = {
                "Chaîne Shimano XT (Remplacement)": ["Moyen",
                    "Une chaîne étirée use la cassette et le plateau. La changer à temps coûte bien moins cher que de changer toute la transmission.",
                    ["Mesurer l'usure avec un testeur de chaîne (remplacer vers 0,5 %).", "Ouvrir l'ancienne chaîne au maillon rapide ou à l'extracteur.", "Couper la neuve à la même longueur et la monter.", "Fermer le maillon rapide, graisser, vérifier les vitesses."],
                    "Testeur d'usure, pince à maillon rapide, extracteur de chaîne",
                    "changer chaine Shimano 12 vitesses VTT"],
                "Nettoyage & Lubrification chaîne": ["Facile",
                    "Une chaîne propre et huilée dure bien plus longtemps et passe les vitesses sans bruit.",
                    ["Dégraisser la chaîne avec une brosse et un dégraissant.", "Rincer à l'eau douce, sans jet haute pression, puis sécher.", "Mettre une goutte de lubrifiant sur chaque maillon.", "Attendre quelques minutes et essuyer l'excédent."],
                    "Dégraissant, brosse, chiffon, lubrifiant pour chaîne",
                    "nettoyer et lubrifier chaîne vélo VTT"],
                "Plaquettes de frein avant (N04C)": ["Moyen",
                    "Les plaquettes usées freinent mal et abîment le disque. Il faut les changer avant qu'il ne reste moins d'un millimètre de matière.",
                    ["Retirer la roue et la goupille ou la vis qui retient les plaquettes.", "Sortir les plaquettes et repousser doucement les pistons avec un levier plastique.", "Monter les plaquettes neuves, remettre la goupille.", "Remonter la roue, pomper le levier, puis roder avec une vingtaine de freinages."],
                    "Plaquettes compatibles, levier plastique ou démonte-pneu, alcool isopropylique",
                    "changer plaquettes frein Shimano VTT"],
                "Plaquettes de frein arrière": ["Moyen",
                    "Mêmes principes qu'à l'avant. L'arrière s'use souvent plus vite sur un vélo électrique, plus lourd.",
                    ["Retirer la roue arrière et la goupille des plaquettes.", "Sortir les anciennes plaquettes, repousser les pistons.", "Monter les neuves, remettre la goupille.", "Remonter la roue, pomper le levier et roder les plaquettes."],
                    "Plaquettes compatibles, levier plastique, alcool isopropylique",
                    "changer plaquettes frein arrière VTT Shimano"],
                "Fourche Fox 36 - Entretien 50h": ["Atelier",
                    "Toutes les 50 heures, Fox conseille de nettoyer et regraisser les joints et la chambre d'air. Ça garde la fourche souple et évite l'usure prématurée.",
                    ["Dépressuriser la fourche et déposer les bas de fourreaux.", "Nettoyer les joints racleurs et les tubes.", "Renouveler l'huile des bas de fourreaux et regraisser les joints.", "Remonter, remettre la pression, noter les réglages."],
                    "Kit d'entretien Fox, huile Fox, clés spéciales : à faire en atelier si tu n'as pas l'outillage",
                    "Fox 36 entretien 50 heures lower leg service"],
                "Fourche Fox 36 - Révision lourde 200h": ["Atelier",
                    "La révision complète, toutes les 200 heures, remplace l'huile de cartouche et les pièces d'usure. C'est un travail d'atelier.",
                    ["Faire démonter la fourche par un atelier spécialisé Fox.", "Demander le changement de l'huile de la cartouche et de la chambre d'air.", "Faire contrôler les bagues et les joints.", "Reprendre les réglages de pression et de détente."],
                    "Atelier spécialisé Fox",
                    "Fox 36 révision complète 200 heures"],
                "Cassette Shimano XT 12-speed": ["Moyen",
                    "Les dents de la cassette s'usent. Une cassette usée fait sauter la chaîne sous l'effort. Elle se change souvent avec la chaîne.",
                    ["Retirer la roue arrière.", "Dévisser l'écrou de cassette avec l'outil, en bloquant la cassette avec un fouet.", "Monter la cassette neuve sur le corps de roue libre.", "Serrer l'écrou, remonter la roue, régler le dérailleur."],
                    "Clé à cassette, fouet à chaîne, graisse",
                    "changer cassette Shimano 12 vitesses VTT"],
                "Pneu arrière / tubeless": ["Moyen",
                    "Le pneu arrière s'use le plus vite. Des crampons arrondis ou des coupures profondes demandent de le changer.",
                    ["Dégonfler et décoller le pneu, vider le liquide préventif.", "Nettoyer la jante, vérifier le fond de jante.", "Monter le pneu neuf, ajouter le liquide préventif, gonfler d'un coup.", "Régler la pression, faire un tour de vélo et revérifier."],
                    "Démonte-pneus, pompe ou compresseur, liquide préventif, clé de valve",
                    "changer pneu tubeless VTT montage"],
                "Disque de frein avant (203 mm)": ["Moyen",
                    "Un disque trop mince chauffe et se déforme. L'épaisseur minimale est gravée dessus, souvent autour de 1,5 mm.",
                    ["Mesurer l'épaisseur avec un pied à coulisse.", "Retirer la roue, dévisser les 6 vis Torx ou le verrou Centerlock.", "Monter le disque neuf, serrer au couple indiqué.", "Changer les plaquettes en même temps et roder."],
                    "Pied à coulisse, clé Torx T25, clé Centerlock si besoin",
                    "changer disque de frein VTT 6 vis Centerlock"],
                "Disque de frein arrière (203 mm)": ["Moyen",
                    "Même contrôle qu'à l'avant. Un disque voilé ou trop fin se remplace, pas de réparation.",
                    ["Mesurer l'épaisseur avec un pied à coulisse.", "Retirer la roue, dévisser le disque.", "Monter le disque neuf et serrer au couple indiqué.", "Changer les plaquettes en même temps et roder."],
                    "Pied à coulisse, clé Torx T25, clé Centerlock si besoin",
                    "changer disque de frein arrière VTT"],
                "Purge du liquide de frein (avant + arrière)": ["Atelier",
                    "Le liquide de frein vieillit et peut perdre en efficacité. Une purge par an garde un levier ferme. Les freins Shimano utilisent de l'huile minérale, jamais de DOT.",
                    ["Prévoir un kit de purge Shimano et l'huile minérale.", "Purger chaque frein en suivant la méthode du fabricant.", "Nettoyer au dégraissant frein, jamais d'huile sur les plaquettes.", "Contrôler la fermeté du levier."],
                    "Kit de purge Shimano, huile minérale, seringue : à faire en atelier si tu débutes",
                    "purge frein Shimano huile minérale VTT"],
                "Plateau (pédalier ACID 34/36 dents)": ["Atelier",
                    "Un plateau aux dents crochues fait sauter la chaîne. Sur un moteur Bosch, le plateau se démonte avec un outil adapté.",
                    ["Regarder la forme des dents : crochues ou pointues, il faut changer.", "Retirer le plateau avec l'outil prévu pour ton moteur.", "Monter le plateau neuf du même nombre de dents.", "Serrer au couple, régler la ligne de chaîne."],
                    "Outil de plateau compatible Bosch, clé dynamométrique : atelier conseillé",
                    "changer plateau moteur Bosch VTT électrique"],
                "Galets de dérailleur arrière": ["Moyen",
                    "Les galets guident la chaîne. Usés ou avec du jeu, ils bruitent et font mal passer les vitesses.",
                    ["Regarder si les dents sont pointues ou si le galet ballotte.", "Dévisser les vis de la chape.", "Remplacer les deux galets, graisser les axes.", "Remonter la chape et tester les vitesses."],
                    "Clés Allen, graisse, galets compatibles",
                    "changer galets dérailleur Shimano"],
                "Câble et gaine de dérailleur": ["Moyen",
                    "Un câble qui frotte rend les vitesses lentes ou imprécises. Il se change environ une fois par an.",
                    ["Mettre la chaîne sur le plus petit pignon.", "Retirer l'ancien câble et la gaine.", "Poser le nouveau câble et la nouvelle gaine, graisser le passage.", "Retendre et régler la tension du câble."],
                    "Pince coupe-câble, clés Allen, graisse",
                    "changer câble et gaine dérailleur VTT"],
                "Pédales (graissage et jeu)": ["Facile",
                    "Des pédales sans jeu et bien graissées tournent mieux et durent plus longtemps.",
                    ["Vérifier le jeu en bougeant la pédale à la main.", "Retirer la pédale (le pas de vis est inversé à gauche).", "Nettoyer et graisser l'axe et le filetage.", "Remonter et serrer."],
                    "Clé de 15 mm ou clé Allen de 8 mm, graisse",
                    "démonter et graisser pédales VTT"],
                "Pneu avant / tubeless": ["Moyen",
                    "Même contrôle qu'à l'arrière. Le pneu avant s'use moins vite mais l'état des crampons compte pour l'adhérence en virage.",
                    ["Regarder les crampons et les flancs (coupures, craquelures).", "Remplacer si les crampons sont arrondis ou s'il y a une hernie.", "Remettre du liquide préventif.", "Régler la pression."],
                    "Démonte-pneus, pompe ou compresseur, liquide préventif",
                    "changer pneu tubeless VTT montage"],
                "Liquide préventif tubeless (2 pneus)": ["Facile",
                    "Le liquide sèche avec le temps et ne bouche plus les crevaisons. On le renouvelle tous les deux à quatre mois.",
                    ["Dégonfler et retirer la valve, ou décoller un côté du pneu.", "Vider le vieux liquide et nettoyer les restes séchés.", "Ajouter la dose recommandée sur le flacon.", "Regonfler, secouer la roue pour répartir le liquide."],
                    "Liquide préventif, outil de valve, pompe",
                    "ajouter liquide préventif tubeless VTT"],
                "Roulements de moyeux (avant + arrière)": ["Atelier",
                    "Des roulements usés font du jeu ou du bruit dans la roue. Un entretien régulier évite de changer le moyeu.",
                    ["Soulever la roue et la faire tourner : bruit, résistance, jeu ?", "Faire démonter le moyeu et nettoyer les roulements.", "Regraisser ou changer les roulements.", "Régler le jeu et remonter."],
                    "Extracteur de roulements, graisse : atelier conseillé",
                    "entretien roulements moyeu VTT"],
                "Rayons et voile des roues (contrôle)": ["Facile",
                    "Un rayon détendu ou une roue voilée fragilise la roue. Un contrôle régulier évite la casse.",
                    ["Faire tourner la roue, regarder si elle frotte.", "Pincer les rayons deux par deux : ils doivent sonner pareil.", "Resserrer légèrement un rayon lâche si tu sais le faire.", "Sinon, passer chez un atelier pour dévoiler."],
                    "Clé à rayons si besoin",
                    "vérifier tension rayons roue VTT"],
                "Amortisseur Fox - Entretien 50h": ["Atelier",
                    "Toutes les 50 heures, l'entretien de la chambre d'air garde l'amortisseur souple et sans fuite.",
                    ["Dépressuriser l'amortisseur.", "Démonter le manchon d'air et nettoyer.", "Remplacer l'huile et regraisser les joints.", "Remonter, remettre la pression et noter les réglages."],
                    "Kit d'entretien Fox, pompe à amortisseur : atelier conseillé",
                    "Fox Float amortisseur entretien 50 heures air can service"],
                "Amortisseur Fox - Révision complète 200h": ["Atelier",
                    "La révision complète, toutes les 200 heures, remplace l'huile et les joints. Travail d'atelier spécialisé.",
                    ["Faire démonter l'amortisseur par un atelier Fox.", "Demander la révision complète.", "Remonter et refaire les réglages.", "Noter les nouvelles valeurs dans Mes réglages."],
                    "Atelier spécialisé Fox",
                    "Fox Float amortisseur révision complète 200 heures"],
                "Roulements de suspension (biellettes)": ["Moyen",
                    "Les roulements des articulations du cadre prennent l'eau et la boue. Ils font du jeu ou des claquements s'ils sont usés.",
                    ["Bloquer la roue arrière et la secouer de côté : y a-t-il du jeu ?", "Retirer les vis et sortir l'axe.", "Nettoyer, regraisser ou changer les roulements.", "Remonter et serrer au couple du fabricant."],
                    "Clés Allen, clé dynamométrique, graisse, extracteur de roulements",
                    "changer roulements suspension VTT biellette"],
                "Jeu de direction (contrôle et graissage)": ["Moyen",
                    "Un jeu dans la direction se sent au freinage et abîme les roulements. Un réglage rapide règle souvent le problème.",
                    ["Serrer le frein avant et balancer le vélo d'avant en arrière : y a-t-il un claquement ?", "Desserrer les vis de potence, régler la vis de la coiffe, resserrer.", "Si ça accroche, démonter, nettoyer et regraisser.", "Remonter la potence, vérifier l'alignement."],
                    "Clés Allen, clé dynamométrique, graisse",
                    "régler jeu de direction VTT"],
                "Tige de selle télescopique (nettoyage et graissage)": ["Moyen",
                    "Sale ou sans graisse, la tige monte et descend moins bien. Un nettoyage régulier des joints prolonge sa durée de vie.",
                    ["Essuyer le tube, relever le joint racleur.", "Mettre quelques gouttes d'huile adaptée.", "Appuyer et relâcher la tige plusieurs fois.", "Si elle reste molle, faire une purge ou passer en atelier."],
                    "Chiffon, huile pour joints, clés Allen",
                    "entretien tige de selle télescopique VTT"],
                "Pneus - pression et état (contrôle)": ["Facile",
                    "La bonne pression améliore l'adhérence, le confort et protège les jantes. Elle dépend de ton poids et du terrain.",
                    ["Mesurer la pression avec un manomètre précis, pneus froids.", "Chercher coupures, épines plantées et usure.", "Ajuster la pression, noter les valeurs dans Mes réglages.", "Remplacer le pneu s'il y a une hernie ou une coupure profonde."],
                    "Manomètre ou pompe avec jauge",
                    "pression pneus VTT tubeless comment régler"],
                "Fourche - pression et SAG (contrôle)": ["Facile",
                    "Le SAG est l'enfoncement de la fourche sous ton poids, vélo à l'arrêt. Il détermine le confort et l'efficacité de la suspension.",
                    ["Mettre la pression avec une pompe à suspension, selon ton poids.", "Faire glisser le joint de SAG, monter sur le vélo en position de pédalage.", "Descendre sans bouger et mesurer l'enfoncement (souvent autour de 20 à 25 %).", "Ajuster la pression, noter dans Mes réglages."],
                    "Pompe à suspension, joint de SAG, règle",
                    "régler SAG fourche VTT Fox"],
                "Amortisseur - pression et SAG (contrôle)": ["Facile",
                    "Même principe qu'avec la fourche. L'amortisseur arrière se règle en général un peu plus mou que la fourche.",
                    ["Mettre la pression avec une pompe à suspension, selon ton poids.", "Monter en position de pédalage avec le joint de SAG sur le tube.", "Descendre et mesurer l'enfoncement (souvent autour de 25 à 30 %).", "Ajuster et noter les valeurs dans Mes réglages."],
                    "Pompe à suspension, joint de SAG, règle",
                    "régler SAG amortisseur arrière VTT"],
                "Contrôle des serrages (potence, cintre, roues, pédalier)": ["Facile",
                    "Les vibrations desserrent les vis. Un contrôle régulier évite de mauvaises surprises en roulant.",
                    ["Vérifier chaque vis avec une clé dynamométrique réglée.", "Respecter les valeurs gravées sur les pièces ou celles du manuel Cube.", "Contrôler potence, cintre, roues, pédalier, étriers, tige de selle.", "Ne jamais serrer plus que le couple indiqué."],
                    "Clé dynamométrique, jeu de clés Allen et Torx",
                    "contrôle serrage couple VTT clé dynamométrique"],
                "Contrôle annuel Bosch (moteur + logiciel)": ["Atelier",
                    "Chez un revendeur Bosch, l'outil de diagnostic lit les erreurs, met à jour le logiciel et contrôle le moteur et la batterie.",
                    ["Prendre rendez-vous chez un revendeur Cube ou Bosch.", "Demander le diagnostic complet et la mise à jour du logiciel.", "Faire noter les kilomètres et les éventuelles erreurs.", "Garder la facture."],
                    "Revendeur agréé Bosch",
                    "Bosch eBike diagnostic revendeur contrôle annuel"],
                "Batterie PowerTube 800 (contacts et état)": ["Facile",
                    "Des contacts propres et une batterie en bon état assurent la charge et l'autonomie. Ne jamais ouvrir la batterie.",
                    ["Retirer la batterie, regarder les contacts et le boîtier.", "Nettoyer les contacts avec un chiffon sec.", "Contrôler le verrou et l'absence de choc ou de gonflement.", "Pour un stockage long, la laisser entre 30 et 60 % de charge."],
                    "Chiffon sec",
                    "entretien batterie Bosch PowerTube vélo électrique"],
                "Chaîne (remplacement)": ["Moyen",
                    "Une chaîne étirée use la cassette et le plateau. La changer à temps coûte bien moins cher que de changer toute la transmission.",
                    ["Mesurer l'usure avec un testeur de chaîne (à remplacer vers 0,5 %).", "Ouvrir l'ancienne chaîne au maillon rapide ou à l'extracteur.", "Couper la neuve à la même longueur et la monter.", "Fermer le maillon rapide, graisser, vérifier que le passage des vitesses est net."],
                    "Testeur d'usure, pince à maillon rapide, extracteur de chaîne",
                    "changer chaine vélo"],
                "Cassette (remplacement)": ["Moyen",
                    "Les dents de la cassette s'usent. Une cassette usée fait sauter la chaîne sous l'effort. Elle se change souvent avec la chaîne.",
                    ["Retirer la roue arrière.", "Dévisser l'écrou de cassette avec l'outil, en bloquant la cassette avec un fouet.", "Monter la cassette neuve sur le corps de roue libre.", "Serrer l'écrou, remonter la roue, régler le dérailleur."],
                    "Clé de cassette, fouet à chaîne, clé dynamométrique",
                    "changer cassette vélo"],
                "Plateau (remplacement)": ["Moyen",
                    "Le plateau s'use plus lentement que la chaîne. Des dents en forme de crochet ou une chaîne qui saute sont des signes d'usure.",
                    ["Déposer la manivelle avec l'outil adapté à ton pédalier.", "Dévisser le plateau (ou l'écrou central selon le modèle).", "Monter le plateau neuf dans le bon sens.", "Remonter la manivelle, serrer au couple indiqué, vérifier le passage des vitesses."],
                    "Clés Allen ou Torx, outil de manivelle, clé dynamométrique",
                    "changer plateau pédalier vélo"],
                "Galets de dérailleur": ["Moyen",
                    "Les deux petites roues du dérailleur arrière guident la chaîne. Usées, elles font du bruit et dégradent le passage des vitesses.",
                    ["Retirer la chaîne ou la dégager de la cage du dérailleur.", "Dévisser les deux galets, un par un, pour ne pas les mélanger.", "Monter les galets neufs dans le même sens, avec un peu de graisse.", "Remettre la chaîne, vérifier que tout tourne librement."],
                    "Clés Allen, graisse, chiffon",
                    "changer galets dérailleur vélo"],
                "Freins - contrôle général (type à confirmer)": ["Facile",
                    "Le type de frein n'est pas encore confirmé. Ce contrôle général permet de repérer un problème sans supposer qu'il s'agit de patins ou de disques.",
                    ["Actionner chaque levier et vérifier qu'il revient normalement.", "Regarder si les deux freins ralentissent le vélo sans bruit anormal.", "Chercher une fuite, une pièce desserrée ou une usure visible.", "Confirmer le type de frein dans la fiche du vélo avant de choisir les pièces à remplacer."],
                    "Lampe, chiffon propre",
                    "contrôler freins vélo"],
                "Patins de frein (avant)": ["Facile",
                    "Les patins doivent toucher la jante correctement, sans être usés jusqu'au témoin ni frotter le pneu.",
                    ["Vérifier l'épaisseur et l'état du caoutchouc.", "Contrôler que le patin touche la piste de freinage et non le pneu.", "Vérifier que la roue tourne librement et que le levier garde une course normale.", "Remplacer les patins par une référence compatible et régler leur alignement."],
                    "Clé Allen, patins compatibles, chiffon",
                    "contrôler remplacer patins frein vélo"],
                "Patins de frein (arrière)": ["Facile",
                    "Les patins arrière s'usent avec le freinage et doivent rester alignés sur la jante.",
                    ["Vérifier l'épaisseur et rechercher des fissures.", "Contrôler l'alignement sur la piste de freinage.", "Vérifier le câble, le retour du levier et l'absence de frottement.", "Remplacer par des patins compatibles et tester le freinage à faible vitesse."],
                    "Clé Allen, patins compatibles, chiffon",
                    "remplacer patins frein arrière vélo"],
                "Courroie (contrôle et remplacement)": ["Atelier",
                    "Une transmission par courroie ne se graisse pas comme une chaîne. Sa tension et son alignement doivent respecter les consignes du fabricant.",
                    ["Rechercher fissures, dents abîmées ou effilochage.", "Vérifier l'alignement de la courroie et l'absence de bruit anormal.", "Ne pas appliquer d'huile ou de graisse.", "Faire régler la tension et remplacer la courroie selon les préconisations du fabricant ou par un atelier."],
                    "Lampe, notice du fabricant",
                    "entretien courroie vélo"],
                "Plaquettes de frein (avant)": ["Moyen",
                    "Les plaquettes usées freinent mal et abîment le disque. Il faut les changer avant qu'il ne reste moins d'un millimètre de matière.",
                    ["Retirer la roue et la goupille ou la vis qui retient les plaquettes.", "Sortir les plaquettes et repousser doucement les pistons avec un levier plastique.", "Monter les plaquettes neuves, remettre la goupille.", "Remonter la roue, pomper le levier et roder les plaquettes."],
                    "Plaquettes compatibles avec ton frein, levier plastique, alcool isopropylique",
                    "changer plaquettes frein à disque vélo"],
                "Plaquettes de frein (arrière)": ["Moyen",
                    "Mêmes principes qu'à l'avant. L'arrière s'use souvent plus vite, surtout sur un vélo lourd ou électrique.",
                    ["Retirer la roue arrière et la goupille des plaquettes.", "Sortir les anciennes plaquettes, repousser les pistons.", "Monter les neuves, remettre la goupille.", "Remonter la roue, pomper le levier et roder les plaquettes."],
                    "Plaquettes compatibles avec ton frein, levier plastique, alcool isopropylique",
                    "changer plaquettes frein arrière à disque vélo"],
                "Disque de frein (avant)": ["Facile",
                    "Un disque trop mince ou voilé freine mal. L'épaisseur minimale est gravée sur le disque.",
                    ["Mesurer l'épaisseur avec un pied à coulisse et la comparer au minimum gravé.", "Dévisser les vis du disque (6 vis Torx ou un verrou central).", "Monter le disque neuf, serrer les vis en croix.", "Remettre les plaquettes si besoin, roder les freins."],
                    "Pied à coulisse, clé Torx, clé dynamométrique",
                    "changer disque de frein vélo"],
                "Disque de frein (arrière)": ["Facile",
                    "Même contrôle qu'à l'avant : épaisseur minimale gravée sur le disque, et surface propre, sans huile.",
                    ["Mesurer l'épaisseur avec un pied à coulisse.", "Dévisser les vis du disque.", "Monter le disque neuf, serrer les vis en croix.", "Roder les freins avec quelques freinages progressifs."],
                    "Pied à coulisse, clé Torx, clé dynamométrique",
                    "changer disque de frein arrière vélo"],
                "Purge du liquide de frein": ["Atelier",
                    "Le liquide de frein absorbe l'humidité avec le temps. Une purge régulière garde un levier ferme et un freinage sûr.",
                    ["Vérifier que le liquide préconisé est bien le bon (huile minérale ou DOT : jamais l'un pour l'autre).", "Purger avec le kit adapté, un frein après l'autre.", "Contrôler la fermeté du levier et l'absence de fuite.", "Si tu n'as jamais purgé, confie-le à un atelier."],
                    "Kit de purge de la marque du frein, liquide adapté, gants, chiffons",
                    "purger frein à disque vélo"],
                "Pneu (avant)": ["Facile",
                    "Un pneu usé ou fendu adhère moins et crève plus facilement. L'avant est le pneu le plus important pour tenir la trajectoire.",
                    ["Regarder la gomme : crampons usés, fissures, flancs abîmés.", "Dégonfler et démonter le pneu de la jante.", "Monter le pneu neuf dans le sens de rotation indiqué sur le flanc.", "Gonfler à la pression conseillée et vérifier l'étanchéité."],
                    "Démonte-pneus, pompe, manomètre, liquide préventif si tubeless",
                    "changer pneu vélo"],
                "Pneu (arrière)": ["Facile",
                    "L'arrière s'use plus vite car il entraîne et freine. Un pneu lisse ou fendu doit être remplacé.",
                    ["Regarder la gomme : crampons usés, fissures, flancs abîmés.", "Dégonfler et démonter le pneu de la jante.", "Monter le pneu neuf dans le sens de rotation indiqué sur le flanc.", "Gonfler à la pression conseillée et vérifier l'étanchéité."],
                    "Démonte-pneus, pompe, manomètre, liquide préventif si tubeless",
                    "changer pneu arrière vélo"],
                "Fourche - entretien (50 h)": ["Atelier",
                    "Toutes les 50 heures environ, la fourche demande un nettoyage et un regraissage des joints. Ça la garde souple et évite l'usure prématurée.",
                    ["Dépressuriser la fourche et déposer les bas de fourreaux.", "Nettoyer les joints racleurs et les tubes.", "Renouveler l'huile des bas de fourreaux et regraisser les joints.", "Remonter, remettre la pression, noter les réglages."],
                    "Kit d'entretien de la marque de ta fourche, huile adaptée : à faire en atelier si tu n'as pas l'outillage",
                    "entretien fourche vélo 50 heures"],
                "Fourche - révision complète (200 h)": ["Atelier",
                    "La révision complète, environ toutes les 200 heures, remplace l'huile de cartouche et les pièces d'usure. C'est un travail d'atelier.",
                    ["Faire démonter la fourche par un atelier spécialisé.", "Demander le changement de l'huile de la cartouche et de la chambre d'air.", "Faire contrôler les bagues et les joints.", "Reprendre les réglages de pression et de détente."],
                    "Atelier spécialisé suspensions",
                    "révision complète fourche vélo 200 heures"],
                "Amortisseur - entretien (50 h)": ["Atelier",
                    "L'amortisseur arrière demande un entretien léger toutes les 50 heures environ : nettoyage et renouvellement de la graisse de la chambre d'air.",
                    ["Dépressuriser l'amortisseur et déposer la chambre d'air.", "Nettoyer, renouveler l'huile et la graisse des joints.", "Remonter et remettre la pression.", "Noter les réglages et vérifier le SAG."],
                    "Kit d'entretien de la marque de ton amortisseur, pompe à amortisseur : à faire en atelier si tu n'as pas l'outillage",
                    "entretien amortisseur arrière vélo 50 heures"],
                "Amortisseur - révision complète (200 h)": ["Atelier",
                    "La révision complète, environ toutes les 200 heures, remplace l'huile et les joints internes. Elle redonne à l'amortisseur son comportement d'origine.",
                    ["Faire démonter l'amortisseur par un atelier spécialisé.", "Demander la vidange complète et le changement des joints.", "Faire contrôler les bagues et les axes de fixation.", "Reprendre les réglages de pression et de détente."],
                    "Atelier spécialisé suspensions",
                    "révision complète amortisseur vélo 200 heures"],
                "Contrôle des serrages (vis et boulons)": ["Facile",
                    "Les vibrations desserrent peu à peu les vis. Un contrôle régulier évite une pièce qui bouge ou qui casse.",
                    ["Vérifier chaque vis avec une clé dynamométrique réglée.", "Respecter les valeurs gravées sur les pièces ou celles du manuel du vélo.", "Contrôler potence, cintre, roues, pédalier, axe de selle et freins.", "Noter ce qui avait bougé pour le surveiller."],
                    "Clé dynamométrique, jeu de clés Allen et Torx",
                    "contrôle serrage couple vélo clé dynamométrique"],
                "Contrôle annuel du moteur et du logiciel": ["Atelier",
                    "Chez un revendeur de la marque du moteur, l'outil de diagnostic lit les erreurs, met à jour le logiciel et contrôle le moteur et la batterie.",
                    ["Prendre rendez-vous chez un revendeur agréé pour ton moteur.", "Demander le diagnostic complet et la mise à jour du logiciel.", "Faire noter les kilomètres et les éventuelles erreurs.", "Garder la facture ou le compte rendu."],
                    "Revendeur agréé",
                    "contrôle annuel moteur vélo électrique diagnostic"],
                "Batterie (contacts et état)": ["Facile",
                    "Des contacts propres et une batterie en bon état assurent la charge et l'autonomie. Ne jamais ouvrir la batterie.",
                    ["Retirer la batterie, regarder les contacts et le boîtier.", "Nettoyer les contacts avec un chiffon sec.", "Contrôler le verrou et l'absence de choc ou de gonflement.", "Pour un stockage long, la laisser à moitié chargée, au sec."],
                    "Chiffon sec",
                    "entretien batterie vélo électrique"]
            };

            function lienYoutube(nom) {
                const g = GUIDES[nom];
                const q = g ? g[4] : (nom + ' entretien VTT');
                return 'https://www.youtube.com/results?search_query=' + encodeURIComponent(q);
            }

            function blocGuide(nom) {
                const g = GUIDES[nom];
                const det = document.createElement('details');
                det.className = 'guide';
                const sum = document.createElement('summary');
                sum.textContent = 'Comment faire';
                det.appendChild(sum);

                if (g) {
                    const niv = document.createElement('div');
                    niv.className = 'guide-niveau';
                    niv.textContent = 'Difficulté : ' + g[0];
                    det.appendChild(niv);

                    const quoi = document.createElement('p');
                    quoi.textContent = g[1];
                    det.appendChild(quoi);

                    const ol = document.createElement('ol');
                    g[2].forEach(t => {
                        const li = document.createElement('li');
                        li.textContent = t;
                        ol.appendChild(li);
                    });
                    det.appendChild(ol);

                    const outils = document.createElement('p');
                    outils.className = 'guide-outils';
                    outils.textContent = 'Outils : ' + g[3];
                    det.appendChild(outils);
                } else {
                    const p = document.createElement('p');
                    p.textContent = 'Pas de fiche pour cette pièce. Une recherche vidéo est disponible.';
                    det.appendChild(p);
                }

                const a = document.createElement('a');
                a.className = 'guide-video';
                a.href = lienYoutube(nom);
                a.target = '_blank';
                a.rel = 'noopener noreferrer';
                a.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" fill="currentColor"/></svg><span></span>';
                a.lastChild.textContent = 'Voir des vidéos sur YouTube';
                det.appendChild(a);
                return det;
            }

            let vertesVisibles = false;
            let verifChoisie = null;

            function nomCourt(nom) {
                return nom.split(' - ')[0].split(' (')[0];
            }

            function texteDelai(c) {
                if (!(c.seuil_jours > 0) || c.jours_ecoules === null) return '';
                const ecoules = typeof c.jours_usure === 'number' ? c.jours_usure : c.jours_ecoules;
                const reste = c.seuil_jours - ecoules;
                return reste >= 0 ? `${reste} j` : `+${-reste} j`;
            }

            let modeAffichage = 'entretien';
            try {
                if (localStorage.getItem('maillon_mode') === 'verifs') modeAffichage = 'verifs';
            } catch (e) {
                // stockage indisponible : on reste sur « entretien »
            }
            let nbVerifs = 0;
            let nbEntretien = 0;
            let urgences = { entretien: null, verifs: null };

            // Rouge s'il y a au moins un élément à faire (100 % ou plus),
            // orange s'il y en a au moins un proche (85 % ou plus).
            function urgenceDe(liste) {
                const rouges = liste.filter(c => c.pourcentage_usure >= 100).length;
                const oranges = liste.filter(
                    c => c.pourcentage_usure >= 85 && c.pourcentage_usure < 100
                ).length;
                const niveau = rouges > 0 ? 'warning' : (oranges > 0 ? 'near-warning' : '');
                return { niveau: niveau, rouges: rouges, oranges: oranges };
            }

            function ajouterPastilleMode(bouton, urgence) {
                if (!urgence || !urgence.niveau) return;

                const texte = urgence.niveau === 'warning'
                    ? nombre(urgence.rouges, 'élément à faire', 'éléments à faire')
                    : nombre(urgence.oranges, 'élément bientôt à faire', 'éléments bientôt à faire');
                const pastille = document.createElement('span');
                pastille.className = 'mode-pastille ' + urgence.niveau;
                pastille.setAttribute('role', 'img');
                pastille.setAttribute('aria-label', texte);
                pastille.title = texte;
                bouton.appendChild(pastille);
            }

            function appliquerMode() {
                if (nbVerifs === 0) modeAffichage = 'entretien';
                const verifs = modeAffichage === 'verifs';

                document.querySelector('.bike-container').dataset.mode = modeAffichage;
                document.getElementById('modeSwitch').style.display = nbVerifs > 0 ? 'flex' : 'none';
                document.getElementById('verifs').style.display = (verifs && nbVerifs > 0) ? 'block' : 'none';
                document.getElementById('composantsList').style.display = verifs ? 'none' : '';

                const bE = document.getElementById('modeEntretien');
                const bV = document.getElementById('modeVerifs');
                remplirBouton(bE, 'cle', 'Entretien');
                const pe = document.createElement('small');
                pe.textContent = String(nbEntretien);
                bE.appendChild(pe);
                // La pastille n'apparaît que sur le bouton de l'autre mode.
                if (verifs) ajouterPastilleMode(bE, urgences.entretien);
                remplirBouton(bV, 'valider', 'Vérifications');
                const pv = document.createElement('small');
                pv.textContent = String(nbVerifs);
                bV.appendChild(pv);
                if (!verifs) ajouterPastilleMode(bV, urgences.verifs);
                bE.setAttribute('aria-selected', String(!verifs));
                bV.setAttribute('aria-selected', String(verifs));

                // Une pastille choisie dans l'autre mode n'a plus de sens ici.
                document.querySelectorAll('.hotspot.choisi').forEach(h => h.classList.remove('choisi'));
                placementId = null;
            }

            function choisirMode(mode) {
                modeAffichage = mode;
                try {
                    localStorage.setItem('maillon_mode', mode);
                } catch (e) {
                    // sans importance
                }
                appliquerMode();
            }

            document.getElementById('modeEntretien').addEventListener('click', () => choisirMode('entretien'));
            document.getElementById('modeVerifs').addEventListener('click', () => choisirMode('verifs'));

            function afficherVerifications(verifs) {
                const zone = document.getElementById('verifs');
                const chips = document.getElementById('verifsChips');
                const detail = document.getElementById('verifDetail');
                chips.innerHTML = '';
                detail.style.display = 'none';
                nbVerifs = verifs.length;

                if (verifs.length === 0) {
                    zone.style.display = 'none';
                    return;
                }

                const aFaire = verifs.filter(v => v.pourcentage_usure >= 100).length;
                document.getElementById('verifsInfo').textContent =
                    aFaire > 0 ? `${aFaire} à faire` : 'Tout est à jour';

                verifs.sort((a, b) => b.pourcentage_usure - a.pourcentage_usure || a.id - b.id);

                verifs.forEach(v => {
                    const etat = v.pourcentage_usure >= 100 ? 'warning'
                        : v.pourcentage_usure >= 85 ? 'near-warning' : 'ok';
                    const rang = verifs.indexOf(v) + 1;
                    const chip = document.createElement('button');
                    chip.type = 'button';
                    chip.className = `chip ${etat}`;
                    chip.dataset.id = v.id;
                    const numChip = document.createElement('span');
                    numChip.className = 'chip-num';
                    numChip.textContent = String(rang);
                    chip.appendChild(numChip);
                    chip.appendChild(document.createTextNode(nomCourt(v.nom)));

                    // Pastille sur la photo (visible en mode « Vérifications »).
                    const pastille = document.createElement('div');
                    pastille.className = `hotspot verif ${etat === 'ok' ? '' : etat}`;
                    pastille.style.top = `${v.pos_y}%`;
                    pastille.style.left = `${v.pos_x}%`;
                    pastille.title = `${v.nom} (${v.pourcentage_usure}%)`;
                    pastille.textContent = String(rang);
                    pastille.dataset.id = v.id;
                    pastille.addEventListener('click', () => {
                        if (placementActif) {
                            placementId = v.id;
                            document.querySelectorAll('.hotspot').forEach(h => {
                                h.classList.toggle('choisi', h === pastille);
                            });
                            return;
                        }
                        verifChoisie = v.id;
                        montrerDetailVerification(verifs);
                        document.getElementById('verifDetail').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                    });
                    document.getElementById('hotspotsContainer').appendChild(pastille);
                    const delai = texteDelai(v);
                    if (delai) {
                        const pt = document.createElement('small');
                        pt.textContent = delai;
                        chip.appendChild(pt);
                    }
                    chip.addEventListener('click', () => {
                        verifChoisie = verifChoisie === v.id ? null : v.id;
                        montrerDetailVerification(verifs);
                    });
                    chips.appendChild(chip);
                });

                if (verifChoisie !== null && !verifs.some(v => v.id === verifChoisie)) {
                    verifChoisie = null;
                }
                montrerDetailVerification(verifs);
            }

            function montrerDetailVerification(verifs) {
                const detail = document.getElementById('verifDetail');
                document.querySelectorAll('#verifsChips .chip').forEach(ch => {
                    ch.classList.toggle('choisie', Number(ch.dataset.id) === verifChoisie);
                });
                document.querySelectorAll('.hotspot.verif').forEach(h => {
                    h.classList.toggle('choisi', !placementActif && Number(h.dataset.id) === verifChoisie);
                });

                const v = verifs.find(x => x.id === verifChoisie);
                if (!v) {
                    detail.style.display = 'none';
                    return;
                }

                detail.innerHTML = '';
                const nom = document.createElement('div');
                nom.className = 'vd-nom';
                nom.textContent = v.nom;
                const info = document.createElement('div');
                info.className = 'vd-info';
                info.textContent = v.seuil_jours > 0
                    ? `Fait il y a ${v.jours_ecoules} j · à refaire tous les ${v.seuil_jours} j`
                    : `Seuil : ${fmtDistance(v.seuil_alerte).complet}`;
                detail.appendChild(nom);
                detail.appendChild(info);

                detail.appendChild(blocGuide(v.nom));

                const bFait = document.createElement('button');
                bFait.className = 'btn-action';
                remplirBouton(bFait, 'cle', 'Marquer fait');
                bFait.addEventListener('click', () => remplacer(v.id, v.nom));
                detail.appendChild(bFait);

                const outils = document.createElement('div');
                outils.className = 'card-tools';
                const bSeuils = document.createElement('button');
                bSeuils.type = 'button';
                remplirBouton(bSeuils, 'crayon', 'Seuils');
                bSeuils.addEventListener('click', () => modifierSeuils(v));
                const bRetirer = document.createElement('button');
                bRetirer.type = 'button';
                bRetirer.className = 'danger';
                remplirBouton(bRetirer, 'poubelle', 'Retirer');
                bRetirer.addEventListener('click', () => retirerComposant(v));
                outils.appendChild(bSeuils);
                outils.appendChild(bRetirer);
                detail.appendChild(outils);
                detail.style.display = 'block';
            }
            let placementActif = false;
            let placementId = null;

            function basculerCarte(card, id) {
                const ouverte = !card.classList.contains('ouverte');
                card.classList.toggle('ouverte', ouverte);
                card.querySelector('.card-top').setAttribute('aria-expanded', String(ouverte));
                if (ouverte) cartesOuvertes.add(id); else cartesOuvertes.delete(id);
                majBoutonDeplier();
            }

            function majBoutonDeplier() {
                const b = document.getElementById('btnToutDeplier');
                if (!b) return;
                const cartes = document.querySelectorAll('#composantsList .card:not(.masquee)');
                const total = cartes.length;
                const ouvertes = document.querySelectorAll('#composantsList .card.ouverte:not(.masquee)').length;
                const tout = total > 0 && ouvertes >= total;
                remplirBouton(b, tout ? 'haut' : 'bas', tout ? 'Tout replier' : 'Tout déplier');
            }

            function toutDeplier() {
                const cartes = document.querySelectorAll('#composantsList .card:not(.masquee)');
                const tout = document.querySelectorAll('#composantsList .card.ouverte:not(.masquee)').length >= cartes.length;
                cartes.forEach(card => {
                    const id = Number(card.id.replace('card-', ''));
                    card.classList.toggle('ouverte', !tout);
                    card.querySelector('.card-top').setAttribute('aria-expanded', String(!tout));
                    if (tout) cartesOuvertes.delete(id); else cartesOuvertes.add(id);
                });
                majBoutonDeplier();
            }

            function basculerPlacement() {
                placementActif = !placementActif;
                placementId = null;
                const bp = document.getElementById('btnPlacer');
                remplirBouton(bp, placementActif ? 'valider' : 'repere',
                    placementActif ? 'Terminer le placement' : 'Placer les repères');
                bp.classList.toggle('actif', placementActif);
                document.getElementById('placementAide').style.display =
                    placementActif ? 'inline' : 'none';
                document.querySelector('.bike-container').classList.toggle('placement', placementActif);
                document.querySelectorAll('.hotspot').forEach(h => h.classList.remove('choisi'));
            }

            async function deplacerRepere(event) {
                if (!placementActif || placementId === null) return;
                if (event.target.classList.contains('hotspot')) return;

                const rect = event.currentTarget.getBoundingClientRect();
                const x = Math.round((event.clientX - rect.left) / rect.width * 100);
                const y = Math.round((event.clientY - rect.top) / rect.height * 100);

                try {
                    const res = await fetch(`/api/composants/${placementId}/position`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({
                            pos_x: Math.max(0, Math.min(100, x)),
                            pos_y: Math.max(0, Math.min(100, y))
                        })
                    });
                    if (!res.ok) throw new Error();
                    const garde = placementId;
                    await chargerEtat();
                    placementId = garde;
                    document.querySelectorAll('.hotspot').forEach(h => {
                        h.classList.toggle('choisi', Number(h.dataset.id) === garde);
                    });
                } catch (e) {
                    alert("Impossible de déplacer le repère.");
                }
            }

            async function modifierSeuils(c) {
                const km = prompt(
                    `Seuil en km pour « ${c.nom} » (0 = aucun) :`,
                    c.seuil_alerte || 0
                );
                if (km === null) return;
                const jours = prompt(
                    `Seuil en jours pour « ${c.nom} » (0 = aucun) :`,
                    c.seuil_jours || 0
                );
                if (jours === null) return;

                try {
                    const res = await fetch(`/api/composants/${c.id}/seuils`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({
                            seuil_alerte: parseInt(km, 10) || 0,
                            seuil_jours: parseInt(jours, 10) || 0
                        })
                    });
                    if (!res.ok) {
                        const err = await res.json().catch(() => ({}));
                        alert(err.detail || "Seuils refusés.");
                        return;
                    }
                    await chargerEtat();
                } catch (e) {
                    alert("Impossible de modifier les seuils.");
                }
            }

            async function retirerComposant(c) {
                if (!confirm(`Retirer « ${c.nom} » de la liste ? Son historique sera aussi supprimé.`)) {
                    return;
                }
                try {
                    const res = await fetch(`/api/composants/${c.id}/supprimer`, {
                        method: 'POST',
                        headers: getHeaders()
                    });
                    if (!res.ok) throw new Error();
                    await chargerEtat();
                } catch (e) {
                    alert("Impossible de retirer cette pièce.");
                }
            }

            async function chargerEtat() {
                try {
                    const res = await fetch('/api/etat', { headers: getHeaders() });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error("Erreur lors du chargement des composants.");
                    }

                    const data = await res.json();
                    kmTotalActuel = Number(data.km_total) || 0;
                    etatComposants = data.composants;

                    document.getElementById('veloInfo').innerText = fmtDistance(data.km_total).complet;

                    const container =
                        document.getElementById('composantsList');

                    const hotspotsContainer =
                        document.getElementById('hotspotsContainer');

                    const selectComp =
                        document.getElementById('histComposant');

                    container.innerHTML = '';
                    hotspotsContainer.innerHTML = '';
                    selectComp.innerHTML = '';

                    const boite = document.querySelector('.bike-container');
                    boite.onclick = deplacerRepere;
                    const verifs = [];

                    data.composants.forEach((c, index) => {
                        let statusClass = 'ok';
                        let hotspotClass = '';

                        if (c.pourcentage_usure >= 100) {
                            statusClass = 'warning';
                            hotspotClass = 'warning';
                        } else if (c.pourcentage_usure >= 85) {
                            statusClass = 'near-warning';
                            hotspotClass = 'near-warning';
                        }

                        const pctAffiche =
                            c.pourcentage_usure > 999
                                ? '999%+'
                                : `${c.pourcentage_usure}%`;

                        const badgeText =
                            c.pourcentage_usure >= 100
                                ? `À faire · ${pctAffiche}`
                                : pctAffiche;

                        const estMaintenance = c.categorie === 'maintenance';
                        const btnLabel = estMaintenance ? 'Marquer fait' : 'Remplacer';

                        const option = document.createElement('option');
                        option.value = c.id;
                        option.textContent = c.nom;
                        selectComp.appendChild(option);

                        if (c.verification) {
                            verifs.push(c);
                            return;
                        }

                        const hotspot = document.createElement('div');
                        hotspot.className = `hotspot entretien ${hotspotClass}`;
                        hotspot.style.top = `${c.pos_y}%`;
                        hotspot.style.left = `${c.pos_x}%`;
                        hotspot.title = `${c.nom} (${c.pourcentage_usure}%)`;
                        hotspot.textContent = c.numero || (index + 1);
                        hotspot.dataset.id = c.id;

                        hotspot.addEventListener('click', () => {
                            if (placementActif) {
                                placementId = c.id;
                                document.querySelectorAll('.hotspot').forEach(h => {
                                    h.classList.toggle('choisi', h === hotspot);
                                });
                                return;
                            }
                            remplacer(c.id, c.nom);
                        });

                        hotspotsContainer.appendChild(hotspot);

                        const card = document.createElement('div');
                        card.className = `card ${statusClass}`;
                        card.id = `card-${c.id}`;

                        const cardTop = document.createElement('div');
                        cardTop.className = 'card-top';

                        const componentName = document.createElement('div');
                        componentName.className = 'component-name';

                        const pastille = document.createElement('span');
                        pastille.className = `num ${statusClass}`;
                        pastille.textContent = c.numero || (index + 1);
                        const libelle = document.createElement('span');
                        libelle.className = 'nom';
                        libelle.textContent = c.nom;
                        componentName.appendChild(pastille);
                        componentName.appendChild(libelle);

                        const badge = document.createElement('div');
                        badge.className = `badge ${statusClass}`;
                        badge.textContent = badgeText;

                        cardTop.appendChild(componentName);
                        cardTop.appendChild(badge);

                        const statsRow = document.createElement('div');
                        statsRow.className = 'stats-row';

                        if (c.seuil_alerte > 0) {
                            const kmText = document.createElement('span');
                            kmText.textContent =
                                `Parcouru depuis l'intervention : ${fmtDistance(c.km_actuel).complet}`;

                            const thresholdText = document.createElement('span');
                            thresholdText.textContent =
                                `Seuil : ${fmtDistance(c.seuil_alerte).complet}`;

                            statsRow.appendChild(kmText);
                            statsRow.appendChild(thresholdText);
                        }

                        if (c.seuil_jours > 0) {
                            const jTxt = document.createElement('span');
                            jTxt.textContent =
                                `Temps écoulé : ${c.jours_ecoules} j`;
                            const jSeuil = document.createElement('span');
                            jSeuil.textContent =
                                `Tous les ${c.seuil_jours} j`;
                            statsRow.appendChild(jTxt);
                            statsRow.appendChild(jSeuil);
                        }

                        const barBg = document.createElement('div');
                        barBg.className = 'bar-bg';

                        const barFill = document.createElement('div');
                        barFill.className = `bar-fill ${statusClass}`;
                        barFill.style.width =
                            `${Math.min(c.pourcentage_usure, 100)}%`;

                        barBg.appendChild(barFill);

                        const button = document.createElement('button');
                        button.className = 'btn-action';
                        remplirBouton(button, estMaintenance ? 'cle' : 'echange', btnLabel);
                        button.addEventListener('click', () => {
                            remplacer(c.id, c.nom);
                        });

                        const chevron = document.createElement('span');
                        chevron.className = 'chevron';
                        chevron.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">'
                            + ICONES.bas + '</svg>';
                        cardTop.appendChild(chevron);
                        cardTop.setAttribute('role', 'button');
                        cardTop.setAttribute('tabindex', '0');
                        cardTop.setAttribute('aria-expanded', 'false');
                        cardTop.addEventListener('click', () => basculerCarte(card, c.id));
                        cardTop.addEventListener('keydown', (ev) => {
                            if (ev.key === 'Enter' || ev.key === ' ') {
                                ev.preventDefault();
                                basculerCarte(card, c.id);
                            }
                        });

                        const detail = document.createElement('div');
                        detail.className = 'card-detail';

                        card.appendChild(cardTop);
                        card.appendChild(barBg);
                        card.appendChild(detail);
                        detail.appendChild(statsRow);

                        if (
                            c.seuil_alerte > 0 &&
                            c.km_usure !== undefined &&
                            Math.abs(c.km_usure - c.km_actuel) >= 1
                        ) {
                            const usureInfo = document.createElement('div');
                            usureInfo.className = 'form-help';
                            usureInfo.style.margin = '0 0 6px';
                            usureInfo.textContent =
                                `Usure estimée selon vos sorties : ${fmtDistance(c.km_usure, 1).complet}`;
                            detail.appendChild(usureInfo);
                        }

                        detail.appendChild(blocGuide(c.nom));
                        detail.appendChild(button);

                        const outils = document.createElement('div');
                        outils.className = 'card-tools';

                        const bSeuils = document.createElement('button');
                        bSeuils.type = 'button';
                        remplirBouton(bSeuils, 'crayon', 'Seuils');
                        bSeuils.addEventListener('click', () => modifierSeuils(c));

                        const bRetirer = document.createElement('button');
                        bRetirer.type = 'button';
                        bRetirer.className = 'danger';
                        remplirBouton(bRetirer, 'poubelle', 'Retirer');
                        bRetirer.addEventListener('click', () => retirerComposant(c));

                        outils.appendChild(bSeuils);
                        outils.appendChild(bRetirer);
                        detail.appendChild(outils);

                        if (cartesOuvertes.has(c.id)) {
                            card.classList.add('ouverte');
                            cardTop.setAttribute('aria-expanded', 'true');
                        }

                        if (!vertesVisibles && c.pourcentage_usure < 85) {
                            card.classList.add('masquee');
                        }

                        container.appendChild(card);
                    });

                    afficherVerifications(verifs);
                    nbEntretien = data.composants.filter(c => !c.verification).length;
                    urgences = {
                        entretien: urgenceDe(data.composants.filter(c => !c.verification)),
                        verifs: urgenceDe(data.composants.filter(c => c.verification))
                    };
                    appliquerMode();

                    const nbPieces = data.composants.filter(c => !c.verification).length;
                    const entete = document.createElement('div');
                    entete.className = 'liste-entete';
                    const titreListe = document.createElement('strong');
                    titreListe.textContent = `${nbPieces} pièces à suivre`;
                    const bTout = document.createElement('button');
                    bTout.type = 'button';
                    bTout.id = 'btnToutDeplier';
                    bTout.addEventListener('click', toutDeplier);
                    entete.appendChild(titreListe);
                    entete.appendChild(bTout);
                    container.insertBefore(entete, container.firstChild);

                    const nbVertes = data.composants.filter(
                        c => !c.verification && c.pourcentage_usure < 85
                    ).length;
                    titreListe.textContent = vertesVisibles || nbVertes === 0
                        ? `${nbPieces} pièces à suivre`
                        : `${nbPieces - nbVertes} à surveiller · ${nbVertes} en bon état`;

                    if (nbVertes > 0) {
                        const bVertes = document.createElement('button');
                        bVertes.type = 'button';
                        bVertes.id = 'btnVertes';
                        bVertes.className = 'btn-voir-plus';
                        remplirBouton(bVertes, vertesVisibles ? 'haut' : 'bas',
                            vertesVisibles
                                ? 'Masquer les pièces en bon état'
                                : `Voir les ${nbVertes} pièces en bon état`);
                        bVertes.addEventListener('click', () => {
                            vertesVisibles = !vertesVisibles;
                            chargerEtat();
                        });
                        container.appendChild(bVertes);
                    }
                    majBoutonDeplier();

                    afficherCoefsPieces(data.composants);
                    await chargerReglagesVelo();
                    await chargerHistorique();
                    await chargerSorties();
                    await chargerReglages();
                    actualiserPastilles();

                } catch (e) {
                    console.error(e);
                    document.getElementById('composantsList').innerHTML =
                        '<div class="loading">Impossible de charger les composants.</div>';
                }
            }


            let historiqueDonnees = [];
            let histSelectionActive = false;
            const histSelection = new Set();
            let histEdition = null;
            let histFiltreId = '';
            let kmTotalActuel = 0;

            function aujourdhuiISO() {
                const d = new Date();
                const mois = String(d.getMonth() + 1).padStart(2, '0');
                const jour = String(d.getDate()).padStart(2, '0');
                return `${d.getFullYear()}-${mois}-${jour}`;
            }

            function dateFrancaise(texte) {
                const morceaux = String(texte || '').slice(0, 10).split('-');
                return morceaux.length === 3 ? morceaux.reverse().join('/') : String(texte || '');
            }

            // Bouton « trois points » et son menu (même présentation que les sorties).
            function ajouterMenuPoints(parent, libelle, actions, options) {
                const opt = options || {};
                const menu = document.createElement('div');
                menu.className = 'menu-sortie' + (opt.classeMenu ? ' ' + opt.classeMenu : '');
                menu.setAttribute('role', 'menu');

                const kebab = document.createElement('button');
                kebab.type = 'button';
                kebab.className = 'menu-sortie-btn' + (opt.classeBtn ? ' ' + opt.classeBtn : '');
                kebab.setAttribute('aria-label', libelle);
                kebab.setAttribute('aria-haspopup', 'menu');
                kebab.setAttribute('aria-expanded', 'false');
                remplirBouton(kebab, opt.icone || 'points', '');

                actions.forEach(a => {
                    const el = document.createElement('button');
                    el.type = 'button';
                    el.setAttribute('role', 'menuitem');
                    if (a.danger) el.className = 'danger';
                    el.disabled = !!a.desactive;
                    remplirBouton(el, a.icone, a.texte);
                    el.addEventListener('click', (e) => {
                        e.stopPropagation();
                        fermerMenusSortie();
                        a.action();
                    });
                    menu.appendChild(el);
                });

                kebab.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const etaitOuvert = menu.classList.contains('ouvert');
                    fermerMenusSortie();
                    if (!etaitOuvert) {
                        menu.classList.add('ouvert');
                        placerMenuSortie(menu);
                        kebab.setAttribute('aria-expanded', 'true');
                    }
                });

                parent.appendChild(kebab);
                parent.appendChild(menu);
                return kebab;
            }

            // ---- Filtres du carnet : recherche, composant, période, tri ----
            let histRecherche = '';
            let histPeriode = { debut: '', fin: '' };
            let histTri = 'recent';
            let histFiltreUI = null;
            let calendrierUI = null;

            ICONES.reinit = '<path d="M3 12a9 9 0 1 0 2.64-6.36L3 8"/><path d="M3 3v5h5"/>';
            ICONES.calendrier = '<rect x="3.5" y="5" width="17" height="15" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4"/>';
            ICONES.chevronG = '<path d="M15 6l-6 6 6 6"/>';
            ICONES.chevronD = '<path d="M9 6l6 6-6 6"/>';

            // Interventions affichées : celles qui passent tous les filtres.
            function interventionsVisibles() {
                const mot = histRecherche.trim().toLocaleLowerCase('fr');
                let liste = historiqueDonnees.filter(h => {
                    if (histFiltreId && String(h.composant_id) !== histFiltreId) return false;
                    const jour = String(h.date_remplacement || '').slice(0, 10);
                    if (histPeriode.debut && jour < histPeriode.debut) return false;
                    if (histPeriode.fin && jour > histPeriode.fin) return false;
                    if (mot) {
                        const texte = [h.composant_nom, h.notes].filter(Boolean).join(' ').toLocaleLowerCase('fr');
                        if (!texte.includes(mot)) return false;
                    }
                    return true;
                });

                if (histTri === 'ancien') {
                    liste = liste.slice().reverse();
                } else if (histTri === 'cout-desc') {
                    liste = liste.slice().sort((a, b) => (Number(b.cout) || 0) - (Number(a.cout) || 0));
                } else if (histTri === 'cout-asc') {
                    liste = liste.slice().sort((a, b) => (Number(a.cout) || 0) - (Number(b.cout) || 0));
                }

                return liste;
            }

            function histFiltreActif() {
                return Boolean(histFiltreId || histRecherche.trim() || histPeriode.debut || histPeriode.fin);
            }

            function textePeriodeHist(p = histPeriode) {
                if (p.debut && p.fin) {
                    return p.debut === p.fin
                        ? dateFrancaise(p.debut)
                        : `${dateFrancaise(p.debut)} → ${dateFrancaise(p.fin)}`;
                }
                if (p.debut) return `Depuis le ${dateFrancaise(p.debut)}`;
                if (p.fin) return `Jusqu'au ${dateFrancaise(p.fin)}`;
                return 'Toutes les périodes';
            }

            function actualiserFiltreHistorique() {
                if (!histFiltreUI) return;
                const actif = histFiltreActif() || histTri !== 'recent';
                histFiltreUI.bouton.classList.toggle('actif', actif);
                histFiltreUI.bouton.setAttribute('aria-label',
                    actif ? 'Filtrer le carnet (filtre actif)' : 'Filtrer le carnet');
                remplirBouton(histFiltreUI.boutonPeriode, 'calendrier', textePeriodeHist());
            }

            function positionnerPanneauHistorique() {
                if (!histFiltreUI || !histFiltreUI.panneau.classList.contains('ouvert')) return;
                const rect = histFiltreUI.bouton.getBoundingClientRect();
                const marge = 12;
                const largeur = Math.min(340, window.innerWidth - marge * 2);
                const gauche = Math.max(marge, Math.min(rect.right - largeur, window.innerWidth - largeur - marge));
                const panneau = histFiltreUI.panneau;
                panneau.style.position = 'fixed';
                panneau.style.top = `${Math.max(marge, rect.bottom + 6)}px`;
                panneau.style.left = `${gauche}px`;
                panneau.style.right = 'auto';
                panneau.style.width = `${largeur}px`;
                panneau.style.boxSizing = 'border-box';
            }

            function creerFiltreHistorique() {
                const panneau = document.createElement('div');
                panneau.id = 'histFiltres';
                panneau.className = 'menu-sortie menu-filtre menu-filtres-sorties';
                panneau.addEventListener('click', (e) => e.stopPropagation());

                const contenu = document.createElement('div');
                contenu.className = 'sorties-filtres-contenu';
                panneau.appendChild(contenu);

                const ajouterChamp = (texte, idChamp, element) => {
                    const etiquette = document.createElement('label');
                    etiquette.htmlFor = idChamp;
                    etiquette.textContent = texte;
                    contenu.appendChild(etiquette);
                    contenu.appendChild(element);
                };

                const recherche = document.createElement('input');
                recherche.type = 'search';
                recherche.id = 'histRecherche';
                recherche.placeholder = 'Composant, note...';
                recherche.setAttribute('aria-label', 'Rechercher une intervention');
                recherche.addEventListener('input', () => {
                    histRecherche = recherche.value;
                    histSelection.clear();
                    actualiserFiltreHistorique();
                    afficherHistorique();
                });
                ajouterChamp('Rechercher une intervention', 'histRecherche', recherche);

                const selComposant = document.createElement('select');
                selComposant.id = 'histFiltreComposant';
                selComposant.setAttribute('aria-label', 'Filtrer par composant');
                selComposant.addEventListener('change', () => {
                    histFiltreId = selComposant.value;
                    histSelection.clear();
                    actualiserFiltreHistorique();
                    afficherHistorique();
                });
                ajouterChamp('Composant', 'histFiltreComposant', selComposant);

                const boutonPeriode = document.createElement('button');
                boutonPeriode.type = 'button';
                boutonPeriode.id = 'histFiltrePeriode';
                boutonPeriode.className = 'hist-periode-btn';
                boutonPeriode.addEventListener('click', () => ouvrirCalendrierHistorique());
                ajouterChamp('Période', 'histFiltrePeriode', boutonPeriode);

                const selTri = document.createElement('select');
                selTri.id = 'histTri';
                selTri.setAttribute('aria-label', 'Trier les interventions');
                [
                    ['recent', 'Plus récentes'],
                    ['ancien', 'Plus anciennes'],
                    ['cout-desc', 'Coût décroissant'],
                    ['cout-asc', 'Coût croissant']
                ].forEach(([valeur, texte]) => {
                    const option = document.createElement('option');
                    option.value = valeur;
                    option.textContent = texte;
                    selTri.appendChild(option);
                });
                selTri.addEventListener('change', () => {
                    histTri = selTri.value;
                    actualiserFiltreHistorique();
                    afficherHistorique();
                });
                ajouterChamp('Trier les interventions', 'histTri', selTri);

                const boutonReinit = document.createElement('button');
                boutonReinit.type = 'button';
                remplirBouton(boutonReinit, 'reinit', 'Réinitialiser les filtres');
                boutonReinit.addEventListener('click', (e) => {
                    e.stopPropagation();
                    histRecherche = '';
                    histFiltreId = '';
                    histPeriode = { debut: '', fin: '' };
                    histTri = 'recent';
                    recherche.value = '';
                    selComposant.value = '';
                    selTri.value = 'recent';
                    histSelection.clear();
                    fermerMenusSortie();
                    actualiserFiltreHistorique();
                    afficherHistorique();
                });
                panneau.appendChild(boutonReinit);

                const bouton = document.createElement('button');
                bouton.type = 'button';
                bouton.className = 'menu-sortie-btn hist-filtre-btn';
                bouton.setAttribute('aria-haspopup', 'true');
                bouton.setAttribute('aria-expanded', 'false');
                remplirBouton(bouton, 'filtre', '');
                bouton.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const etaitOuvert = panneau.classList.contains('ouvert');
                    fermerMenusSortie();
                    if (!etaitOuvert) {
                        panneau.classList.add('ouvert');
                        bouton.setAttribute('aria-expanded', 'true');
                        positionnerPanneauHistorique();
                    }
                });

                window.addEventListener('scroll', positionnerPanneauHistorique, true);
                window.addEventListener('resize', positionnerPanneauHistorique);

                histFiltreUI = { panneau, bouton, boutonPeriode, recherche, selComposant, selTri };
                return histFiltreUI;
            }

            // Met en place le bouton entonnoir et son panneau (sans les recréer à chaque affichage).
            function monterFiltreHistorique(zone, comptes) {
                const ui = histFiltreUI || creerFiltreHistorique();

                ui.selComposant.innerHTML = '';
                const toutes = document.createElement('option');
                toutes.value = '';
                toutes.textContent = `Tous les composants (${historiqueDonnees.length})`;
                ui.selComposant.appendChild(toutes);

                Array.from(comptes.entries())
                    .sort((a, b) => a[1].nom.localeCompare(b[1].nom, 'fr'))
                    .forEach(entree => {
                        const option = document.createElement('option');
                        option.value = entree[0];
                        option.textContent = `${entree[1].nom} (${entree[1].n})`;
                        ui.selComposant.appendChild(option);
                    });

                ui.selComposant.value = histFiltreId;
                ui.selTri.value = histTri;

                if (ui.panneau.parentNode !== zone) zone.appendChild(ui.panneau);
                if (ui.bouton.parentNode !== zone) zone.appendChild(ui.bouton);
                actualiserFiltreHistorique();
            }

            // Fenêtre calendrier pour choisir une période (premier jour, puis dernier jour).
            function creerCalendrierPeriode() {
                const fenetre = document.createElement('div');
                fenetre.className = 'fenetre-profil';
                fenetre.style.display = 'none';
                fenetre.setAttribute('role', 'dialog');
                fenetre.setAttribute('aria-modal', 'true');
                fenetre.setAttribute('aria-label', 'Choisir une période');

                const boite = document.createElement('div');
                boite.className = 'fp-boite cal-boite';
                fenetre.appendChild(boite);

                const haut = document.createElement('div');
                haut.className = 'fp-haut';
                const titre = document.createElement('strong');
                titre.textContent = 'Choisir une période';
                const fermerBtn = document.createElement('button');
                fermerBtn.type = 'button';
                fermerBtn.className = 'cal-rond';
                fermerBtn.setAttribute('aria-label', 'Fermer');
                remplirBouton(fermerBtn, 'fermer', '');
                haut.appendChild(titre);
                haut.appendChild(fermerBtn);
                boite.appendChild(haut);

                const raccourcis = document.createElement('div');
                raccourcis.className = 'cal-raccourcis';
                boite.appendChild(raccourcis);

                const navigation = document.createElement('div');
                navigation.className = 'cal-navigation';
                const precedent = document.createElement('button');
                precedent.type = 'button';
                precedent.className = 'cal-rond';
                precedent.setAttribute('aria-label', 'Mois précédent');
                remplirBouton(precedent, 'chevronG', '');
                const titreMois = document.createElement('strong');
                titreMois.setAttribute('aria-live', 'polite');
                const suivant = document.createElement('button');
                suivant.type = 'button';
                suivant.className = 'cal-rond';
                suivant.setAttribute('aria-label', 'Mois suivant');
                remplirBouton(suivant, 'chevronD', '');
                navigation.appendChild(precedent);
                navigation.appendChild(titreMois);
                navigation.appendChild(suivant);
                boite.appendChild(navigation);

                const semaine = document.createElement('div');
                semaine.className = 'cal-semaine';
                ['L', 'M', 'M', 'J', 'V', 'S', 'D'].forEach(lettre => {
                    const cellule = document.createElement('span');
                    cellule.textContent = lettre;
                    semaine.appendChild(cellule);
                });
                boite.appendChild(semaine);

                const grille = document.createElement('div');
                grille.className = 'cal-grille';
                boite.appendChild(grille);

                const resume = document.createElement('p');
                resume.className = 'form-help cal-resume';
                boite.appendChild(resume);

                const actions = document.createElement('div');
                actions.className = 'cal-actions';
                const effacer = document.createElement('button');
                effacer.type = 'button';
                effacer.className = 'cal-secondaire';
                effacer.textContent = 'Toutes les périodes';
                const valider = document.createElement('button');
                valider.type = 'button';
                valider.className = 'btn-primary';
                valider.textContent = 'Valider';
                actions.appendChild(effacer);
                actions.appendChild(valider);
                boite.appendChild(actions);

                let choix = { debut: '', fin: '' };
                let vue = { annee: 2026, mois: 0 };
                let joursMarques = new Set();
                let auAppliquer = null;

                const versISO = (d) => {
                    const mois = String(d.getMonth() + 1).padStart(2, '0');
                    const jour = String(d.getDate()).padStart(2, '0');
                    return `${d.getFullYear()}-${mois}-${jour}`;
                };

                const placerVue = (texteISO) => {
                    const d = texteISO ? new Date(texteISO + 'T12:00:00') : new Date();
                    vue = { annee: d.getFullYear(), mois: d.getMonth() };
                };

                const dessiner = () => {
                    const premier = new Date(vue.annee, vue.mois, 1);
                    const decalage = (premier.getDay() + 6) % 7;
                    const nbJours = new Date(vue.annee, vue.mois + 1, 0).getDate();
                    const aujourdhui = aujourdhuiISO();
                    const joursAvecEntretien = joursMarques;
                    const nomMois = premier.toLocaleDateString('fr-FR', { month: 'long', year: 'numeric' });
                    titreMois.textContent = nomMois.charAt(0).toUpperCase() + nomMois.slice(1);

                    grille.innerHTML = '';
                    for (let i = 0; i < decalage; i++) {
                        const videCase = document.createElement('span');
                        grille.appendChild(videCase);
                    }

                    for (let j = 1; j <= nbJours; j++) {
                        const jourISO = versISO(new Date(vue.annee, vue.mois, j));
                        const b = document.createElement('button');
                        b.type = 'button';
                        b.className = 'cal-jour';
                        b.textContent = String(j);
                        b.setAttribute('aria-label', dateFrancaise(jourISO));

                        const fin = choix.fin || choix.debut;
                        if (choix.debut && jourISO >= choix.debut && jourISO <= fin) b.classList.add('dans-periode');
                        if (jourISO === choix.debut || jourISO === fin && choix.debut) b.classList.add('borne');
                        if (jourISO === aujourdhui) b.classList.add('auj');
                        if (joursAvecEntretien.has(jourISO)) b.classList.add('avec-entretien');

                        b.addEventListener('click', () => {
                            if (!choix.debut || choix.fin) {
                                choix = { debut: jourISO, fin: '' };
                            } else if (jourISO < choix.debut) {
                                choix = { debut: jourISO, fin: choix.debut };
                            } else {
                                choix = { debut: choix.debut, fin: jourISO };
                            }
                            dessiner();
                        });
                        grille.appendChild(b);
                    }

                    if (!choix.debut) {
                        resume.textContent = 'Touchez le premier jour, puis le dernier jour.';
                    } else if (!choix.fin) {
                        resume.textContent = `Du ${dateFrancaise(choix.debut)} — touchez le dernier jour (ou Valider pour ce seul jour).`;
                    } else if (choix.debut === choix.fin) {
                        resume.textContent = `Le ${dateFrancaise(choix.debut)}.`;
                    } else {
                        resume.textContent = `Du ${dateFrancaise(choix.debut)} au ${dateFrancaise(choix.fin)}.`;
                    }
                };

                const raccourci = (texte, calcul) => {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.className = 'cal-puce';
                    b.textContent = texte;
                    b.addEventListener('click', () => {
                        choix = calcul();
                        placerVue(choix.fin || choix.debut);
                        dessiner();
                    });
                    raccourcis.appendChild(b);
                };
                const reculer = (n) => {
                    const d = new Date();
                    d.setDate(d.getDate() - n);
                    return versISO(d);
                };
                raccourci('7 jours', () => ({ debut: reculer(6), fin: aujourdhuiISO() }));
                raccourci('30 jours', () => ({ debut: reculer(29), fin: aujourdhuiISO() }));
                raccourci('Cette année', () => ({ debut: `${new Date().getFullYear()}-01-01`, fin: aujourdhuiISO() }));

                const fermer = () => { fenetre.style.display = 'none'; };

                const appliquer = (periode) => {
                    fermer();
                    if (auAppliquer) auAppliquer(periode);
                };

                precedent.addEventListener('click', () => {
                    vue = vue.mois === 0 ? { annee: vue.annee - 1, mois: 11 } : { annee: vue.annee, mois: vue.mois - 1 };
                    dessiner();
                });
                suivant.addEventListener('click', () => {
                    vue = vue.mois === 11 ? { annee: vue.annee + 1, mois: 0 } : { annee: vue.annee, mois: vue.mois + 1 };
                    dessiner();
                });
                fermerBtn.addEventListener('click', fermer);
                effacer.addEventListener('click', () => appliquer({ debut: '', fin: '' }));
                valider.addEventListener('click', () => appliquer(
                    choix.debut ? { debut: choix.debut, fin: choix.fin || choix.debut } : { debut: '', fin: '' }
                ));
                fenetre.addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (e.target === fenetre) fermer();
                });
                document.addEventListener('keydown', (e) => {
                    if (e.key === 'Escape' && fenetre.style.display !== 'none') fermer();
                });

                document.body.appendChild(fenetre);

                return {
                    ouvrir(periode, jours, appliquerPeriode) {
                        joursMarques = jours || new Set();
                        auAppliquer = appliquerPeriode;
                        choix = { debut: periode.debut, fin: periode.fin };
                        placerVue(choix.fin || choix.debut);
                        dessiner();
                        fenetre.style.display = 'flex';
                    }
                };
            }

            // Ouvre le calendrier : période de départ, jours à repérer d'un point,
            // et ce qu'il faut faire quand on valide.
            function ouvrirCalendrierPeriode(periode, jours, appliquerPeriode) {
                if (!calendrierUI) calendrierUI = creerCalendrierPeriode();
                calendrierUI.ouvrir(periode, jours, appliquerPeriode);
            }

            function ouvrirCalendrierHistorique() {
                ouvrirCalendrierPeriode(
                    histPeriode,
                    new Set(historiqueDonnees.map(h => String(h.date_remplacement || '').slice(0, 10))),
                    (periode) => {
                        histPeriode = periode;
                        histSelection.clear();
                        actualiserFiltreHistorique();
                        afficherHistorique();
                    }
                );
            }

            async function chargerHistorique() {
                try {
                    const res = await fetch('/api/historique', { headers: getHeaders() });
                    if (!res.ok) throw new Error();

                    historiqueDonnees = await res.json();

                    // On oublie les lignes sélectionnées qui n'existent plus.
                    const ids = new Set(historiqueDonnees.map(h => h.id));
                    Array.from(histSelection).forEach(id => {
                        if (!ids.has(id)) histSelection.delete(id);
                    });

                    afficherHistorique();

                } catch (e) {
                    console.error(e);
                }
            }

            function afficherHistorique() {
                const listDiv = document.getElementById('historyList');
                const zoneMenu = document.getElementById('histMenuZone');
                const total = document.getElementById('histTotal');
                const barre = document.getElementById('histSelection');
                const vide = historiqueDonnees.length === 0;

                if (vide) {
                    histSelectionActive = false;
                    histSelection.clear();
                }

                // Filtre par composant : seuls les composants qui ont des interventions.
                const comptes = new Map();
                historiqueDonnees.forEach(h => {
                    const cle = String(h.composant_id);
                    const c = comptes.get(cle) || { nom: h.composant_nom, n: 0 };
                    c.n += 1;
                    comptes.set(cle, c);
                });

                if (histFiltreId && !comptes.has(histFiltreId)) histFiltreId = '';

                const visibles = interventionsVisibles();
                document.getElementById('histCompteur').textContent = vide ? '' : String(historiqueDonnees.length);

                // Bouton entonnoir (filtre), à gauche des trois points.
                // Le panneau de filtres reste en place d'un affichage à l'autre
                // (sinon la saisie dans la recherche perdrait le curseur à chaque lettre).
                Array.from(zoneMenu.children).forEach(enfant => {
                    const garde = histFiltreUI
                        && (enfant === histFiltreUI.panneau || enfant === histFiltreUI.bouton);
                    if (!garde) enfant.remove();
                });
                monterFiltreHistorique(zoneMenu, comptes);

                // Menu général (trois points en haut à droite).
                ajouterMenuPoints(zoneMenu, "Actions de l'historique", [
                    { icone: 'plus', texte: 'Ajouter une intervention', action: () => ouvrirFenetreIntervention(null, null) },
                    {
                        icone: 'cases',
                        texte: histSelectionActive ? 'Quitter la sélection' : 'Sélectionner plusieurs',
                        desactive: vide,
                        action: () => {
                            histSelectionActive = !histSelectionActive;
                            histSelection.clear();
                            afficherHistorique();
                        }
                    },
                    { icone: 'telecharger', texte: 'Exporter en tableur (CSV)', desactive: vide, action: exporterHistoriqueCsv }
                ]);

                // Total des interventions et des coûts.
                const somme = visibles.reduce((t, h) => t + (Number(h.cout) || 0), 0);
                total.textContent = vide
                    ? ''
                    : (histFiltreActif()
                        ? `${visibles.length} sur ${nombre(historiqueDonnees.length, 'intervention', 'interventions')}`
                        : nombre(visibles.length, 'intervention', 'interventions'))
                        + (somme > 0
                            ? ' · ' + somme.toLocaleString('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' € dépensés'
                            : '');

                // Barre d'actions de la sélection.
                barre.innerHTML = '';
                barre.style.display = histSelectionActive ? 'flex' : 'none';

                if (histSelectionActive) {
                    const compteur = document.createElement('span');
                    compteur.textContent = histSelection.size === 0
                        ? 'Touchez les interventions à choisir'
                        : nombre(histSelection.size, 'sélectionnée', 'sélectionnées');
                    barre.appendChild(compteur);

                    const toutes = histSelection.size === visibles.length;
                    const bTout = document.createElement('button');
                    bTout.type = 'button';
                    bTout.textContent = toutes ? 'Tout désélectionner' : 'Tout sélectionner';
                    bTout.addEventListener('click', () => {
                        histSelection.clear();
                        if (!toutes) visibles.forEach(h => histSelection.add(h.id));
                        afficherHistorique();
                    });
                    barre.appendChild(bTout);

                    const bSuppr = document.createElement('button');
                    bSuppr.type = 'button';
                    bSuppr.className = 'danger';
                    bSuppr.disabled = histSelection.size === 0;
                    bSuppr.textContent = '🗑️ Supprimer';
                    bSuppr.addEventListener('click', supprimerSelection);
                    barre.appendChild(bSuppr);

                    const bAnnuler = document.createElement('button');
                    bAnnuler.type = 'button';
                    bAnnuler.textContent = 'Annuler';
                    bAnnuler.addEventListener('click', () => {
                        histSelectionActive = false;
                        histSelection.clear();
                        afficherHistorique();
                    });
                    barre.appendChild(bAnnuler);
                }

                // Liste.
                listDiv.innerHTML = '';

                if (vide) {
                    const p = document.createElement('p');
                    p.style.color = 'var(--text-muted)';
                    p.style.fontSize = '0.85rem';
                    p.textContent = "Aucune intervention enregistrée. Utilisez le menu ⋮ pour en ajouter une.";
                    listDiv.appendChild(p);
                    return;
                }

                visibles.forEach(h => {
                    const item = document.createElement('div');
                    item.className = 'history-item';

                    let caseSel = null;

                    if (histSelectionActive) {
                        item.classList.add('en-selection');
                        caseSel = document.createElement('input');
                        caseSel.type = 'checkbox';
                        caseSel.className = 'hist-case';
                        caseSel.checked = histSelection.has(h.id);
                        caseSel.setAttribute('aria-label', `Choisir ${h.composant_nom}`);
                        item.classList.toggle('choisie', caseSel.checked);

                        const basculer = () => {
                            if (caseSel.checked) histSelection.add(h.id);
                            else histSelection.delete(h.id);
                            afficherHistorique();
                        };
                        caseSel.addEventListener('change', basculer);
                        item.addEventListener('click', (e) => {
                            if (e.target === caseSel) return;
                            caseSel.checked = !caseSel.checked;
                            basculer();
                        });
                        item.appendChild(caseSel);
                    } else {
                        item.classList.add('avec-menu');
                    }

                    const details = document.createElement('div');
                    details.className = 'history-details';

                    const title = document.createElement('strong');
                    title.textContent = h.composant_nom;

                    const detailText = document.createElement('div');
                    detailText.style.color = 'var(--text-muted)';
                    detailText.style.marginTop = '4px';

                    const kmText =
                        h.km_intervention !== null && h.km_intervention !== undefined
                            ? `🚲 À ${fmtDistance(h.km_intervention).complet}`
                            : '🚲 Kilométrage non renseigné';

                    detailText.textContent =
                        `${h.notes || 'Intervention réalisée'} · ` +
                        `${dateFrancaise(h.date_remplacement)} · ${kmText}`;

                    details.appendChild(title);
                    details.appendChild(detailText);

                    const cost = document.createElement('div');
                    cost.className = 'history-cost';
                    cost.textContent = h.cout > 0 ? `${h.cout} €` : '';

                    item.appendChild(details);
                    item.appendChild(cost);

                    // Menu « trois points » de la ligne.
                    if (!histSelectionActive) {
                        ajouterMenuPoints(item, `Actions : ${h.composant_nom}`, [
                            { icone: 'crayon', texte: 'Modifier', action: () => ouvrirFenetreIntervention(h, null) },
                            { icone: 'echange', texte: "Refaire aujourd'hui", action: () => ouvrirFenetreIntervention(null, h) },
                            { icone: 'poubelle', texte: 'Supprimer', danger: true, action: () => supprimerIntervention(h.id, h.composant_nom, dateFrancaise(h.date_remplacement)) }
                        ]);
                    }

                    listDiv.appendChild(item);
                });
            }


            async function supprimerIntervention(id, nom, jour) {
                const confirmation = confirm(
                    `Supprimer l'intervention « ${nom} » du ${jour} ? ` +
                    `La pièce reviendra au point de départ de l'intervention ` +
                    `précédente (ou à 0 km s'il n'y en a pas).`
                );

                if (!confirmation) {
                    return;
                }

                try {
                    const res = await fetch(`/api/historique/${id}/supprimer`, {
                        method: 'POST',
                        headers: getHeaders()
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    // Recharge tout : l'usure de la pièce change.
                    await chargerEtat();

                } catch (e) {
                    alert("Suppression impossible.");
                }
            }


            async function supprimerSelection() {
                const ids = Array.from(histSelection);

                if (ids.length === 0) return;

                const confirmation = confirm(
                    `Supprimer ${nombre(ids.length, 'intervention', 'interventions')} ? ` +
                    `Chaque pièce reviendra au point de départ d'avant ces interventions.`
                );

                if (!confirmation) return;

                try {
                    const res = await fetch('/api/historique/supprimer-plusieurs', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ ids: ids })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) throw new Error();

                    histSelection.clear();
                    histSelectionActive = false;
                    await chargerEtat();

                } catch (e) {
                    alert("Suppression impossible.");
                }
            }


            function exporterHistoriqueCsv() {
                const aExporter = interventionsVisibles();

                if (aExporter.length === 0) return;

                const separateur = ';';
                const saut = String.fromCharCode(13, 10);
                const guillemet = String.fromCharCode(34);

                const champ = (valeur) => {
                    const texte = String(valeur === null || valeur === undefined ? '' : valeur);
                    return guillemet + texte.split(guillemet).join(guillemet + guillemet) + guillemet;
                };

                const lignes = [
                    ['Date', 'Composant', 'Kilométrage', 'Coût (€)', 'Notes'].map(champ).join(separateur)
                ];

                aExporter.forEach(h => {
                    lignes.push([
                        dateFrancaise(h.date_remplacement),
                        h.composant_nom,
                        h.km_intervention,
                        String(Number(h.cout) || 0).replace('.', ','),
                        h.notes
                    ].map(champ).join(separateur));
                });

                // Le premier caractère permet à Excel de lire correctement les accents.
                const contenu = String.fromCharCode(0xFEFF) + lignes.join(saut);
                const blob = new Blob([contenu], { type: 'text/csv;charset=utf-8' });
                const lien = document.createElement('a');
                const adresse = URL.createObjectURL(blob);

                lien.href = adresse;
                lien.download = `historique_interventions_${aujourdhuiISO()}.csv`;
                document.body.appendChild(lien);
                lien.click();
                lien.remove();
                URL.revokeObjectURL(adresse);
            }


            // Fenêtre d'ajout / de modification d'une intervention.
            // edition = intervention à modifier ; modele = intervention à refaire.
            function ouvrirFenetreIntervention(edition, modele) {
                histEdition = edition || null;

                const source = edition || modele || null;
                const select = document.getElementById('histComposant');

                if (source) select.value = String(source.composant_id);
                select.disabled = !!edition;

                document.getElementById('histTitre').textContent = edition
                    ? "✏️ Modifier l'intervention"
                    : (modele ? "🔁 Refaire l'intervention" : '🔧 Enregistrer une intervention');

                document.getElementById('histDate').value = edition
                    ? String(edition.date_remplacement || '').slice(0, 10)
                    : aujourdhuiISO();

                const km = edition ? edition.km_intervention : kmTotalActuel;
                document.getElementById('histKm').value =
                    km === null || km === undefined || km === 0 ? (edition ? '0' : '') : String(km);

                document.getElementById('histCout').value =
                    source && source.cout > 0 ? String(source.cout) : '';
                document.getElementById('histNotes').value =
                    source && source.notes ? source.notes : '';

                document.getElementById('histAide').textContent = edition
                    ? "Le composant ne peut pas être changé : supprimez l'intervention et recréez-la si besoin. Si c'est la dernière de ce composant, le nouveau kilométrage devient son point de départ."
                    : "Ce kilométrage deviendra le point de départ du suivi d'entretien du composant. Pour une intervention d'aujourd'hui, laissez le kilométrage actuel du vélo.";

                document.getElementById('histEnregistrer').textContent = edition
                    ? 'Enregistrer les modifications'
                    : "Enregistrer l'intervention";

                document.getElementById('fenetreIntervention').style.display = 'flex';

                setTimeout(() => {
                    const premier = edition
                        ? document.getElementById('histDate')
                        : select;
                    premier.focus();
                }, 50);
            }

            function fermerFenetreIntervention() {
                document.getElementById('fenetreIntervention').style.display = 'none';
                document.getElementById('histComposant').disabled = false;
                histEdition = null;
            }

            async function enregistrerIntervention() {
                const composant_id = parseInt(
                    document.getElementById('histComposant').value,
                    10
                );

                const date_remplacement =
                    document.getElementById('histDate').value;

                const km_intervention = parseInt(
                    document.getElementById('histKm').value,
                    10
                );

                const cout =
                    parseFloat(document.getElementById('histCout').value) || 0;

                const notes =
                    document.getElementById('histNotes').value.trim()
                    || (date_remplacement === aujourdhuiISO() ? 'Intervention' : 'Ancien entretien');

                if (!date_remplacement) {
                    alert("Veuillez sélectionner la date de l'intervention.");
                    return;
                }

                if (isNaN(km_intervention) || km_intervention < 0) {
                    alert("Veuillez saisir un kilométrage valide.");
                    return;
                }

                if (!histEdition && isNaN(composant_id)) {
                    alert("Veuillez choisir un composant.");
                    return;
                }

                const bouton = document.getElementById('histEnregistrer');
                bouton.disabled = true;

                try {
                    const corps = { date_remplacement, km_intervention, cout, notes };
                    let adresse = '/api/historique';

                    if (histEdition) {
                        adresse = `/api/historique/${histEdition.id}/modifier`;
                    } else {
                        corps.composant_id = composant_id;
                    }

                    const res = await fetch(adresse, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify(corps)
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail);

                    fermerFenetreIntervention();
                    await chargerEtat();

                } catch (e) {
                    alert(e.message || "Une erreur est survenue.");
                } finally {
                    bouton.disabled = false;
                    if (document.getElementById('fenetreIntervention').style.display !== 'none') {
                        bouton.focus();
                    }
                }
            }

            (function preparerFenetreIntervention() {
                const fenetre = document.getElementById('fenetreIntervention');
                const fermer = document.getElementById('histFermer');

                fermer.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">'
                    + ICONES.fermer + '</svg>';
                fermer.addEventListener('click', fermerFenetreIntervention);

                fenetre.addEventListener('click', (e) => {
                    if (e.target === fenetre) fermerFenetreIntervention();
                });

                // Échap ferme la fenêtre, même si le focus est ailleurs.
                document.addEventListener('keydown', (e) => {
                    if (e.key === 'Escape' && fenetre.style.display !== 'none') {
                        fermerFenetreIntervention();
                    }
                });

                fenetre.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter' && e.target.tagName === 'INPUT') {
                        e.preventDefault();
                        enregistrerIntervention();
                    }
                });
            })();


                        async function syncKm() {
                const input = document.getElementById('kmInput');
                const km = parseInt(input.value, 10);

                if (isNaN(km) || km < 0) {
                    alert("Veuillez saisir un kilométrage valide.");
                    return;
                }

                try {
                    const res = await fetch('/api/sync', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ km_total: km })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail);

                    input.value = '';
                    await chargerEtat();

                } catch (e) {
                    alert(e.message || "Impossible de mettre à jour le kilométrage.");
                }
            }


            async function remplacer(id, nom) {
                if (!confirm(`Confirmer l'intervention pour : ${nom} ?`)) {
                    return;
                }

                const aujourdhui = new Date().toISOString().split('T')[0];

                try {
                    const res = await fetch(`/api/composants/${id}/remplacer`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({
                            notes: "Intervention standard",
                            cout: 0.0,
                            date_remplacement: aujourdhui
                        })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) throw new Error();
                    await chargerEtat();

                } catch (e) {
                    alert("Impossible d'enregistrer l'intervention.");
                }
            }


            const LIBELLES_TERRAIN = {
                mixte: 'Mixte',
                route: 'Route',
                chemin: 'Chemin',
                sentier: 'Sentier technique'
            };


            function formaterDuree(secondes) {
                const minutesTotales = Math.round(secondes / 60);
                const h = Math.floor(minutesTotales / 60);
                const m = minutesTotales % 60;

                return h > 0
                    ? `${h} h ${String(m).padStart(2, '0')}`
                    : `${m} min`;
            }


            function dessinerTrace(trace) {
                const ns = 'http://www.w3.org/2000/svg';
                const svg = document.createElementNS(ns, 'svg');

                svg.setAttribute('viewBox', '0 0 100 60');
                svg.setAttribute('width', '90');
                svg.setAttribute('height', '54');
                svg.style.flexShrink = '0';

                if (!trace || trace.length < 2) {
                    return svg;
                }

                const lats = trace.map(p => p[0]);
                const lons = trace.map(p => p[1]);
                const minLat = Math.min(...lats);
                const maxLat = Math.max(...lats);
                const minLon = Math.min(...lons);
                const maxLon = Math.max(...lons);

                const kx = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180);
                const largeur = ((maxLon - minLon) * kx) || 1e-6;
                const hauteur = (maxLat - minLat) || 1e-6;
                const echelle = Math.min(92 / largeur, 52 / hauteur);
                const dx = (100 - largeur * echelle) / 2;
                const dy = (60 - hauteur * echelle) / 2;

                const points = trace.map(p => {
                    const x = dx + (p[1] - minLon) * kx * echelle;
                    const y = dy + (maxLat - p[0]) * echelle;
                    return `${x.toFixed(1)},${y.toFixed(1)}`;
                }).join(' ');

                const ligne = document.createElementNS(ns, 'polyline');
                ligne.setAttribute('points', points);
                ligne.setAttribute('fill', 'none');
                ligne.setAttribute('stroke', '#3b82f6');
                ligne.setAttribute('stroke-width', '2');
                ligne.setAttribute('stroke-linejoin', 'round');
                ligne.setAttribute('stroke-linecap', 'round');

                svg.appendChild(ligne);

                return svg;
            }


            // ---------- Pente et type de chemin ----------
            const COULEURS_PENTE = [
                { max: 4, couleur: '#22c55e', nom: 'moins de 4 %' },
                { max: 8, couleur: '#facc15', nom: '4 à 8 %' },
                { max: 12, couleur: '#f97316', nom: '8 à 12 %' },
                { max: 16, couleur: '#ef4444', nom: '12 à 16 %' },
                { max: 1000, couleur: '#7f1d1d', nom: 'plus de 16 %' }
            ];
            const TYPES_CHEMIN = {
                r: { couleur: '#94a3b8', nom: 'Route' },
                v: { couleur: '#38bdf8', nom: 'Piste cyclable' },
                c: { couleur: '#d4a24c', nom: 'Chemin / piste' },
                s: { couleur: '#8b5a2b', nom: 'Sentier' },
                u: { couleur: '#475569', nom: 'Inconnu' }
            };

            function couleurPente(pourcent) {
                const a = Math.abs(pourcent);
                return COULEURS_PENTE.find(c => a < c.max).couleur;
            }

            function distanceLatLon(a, b) {
                const rad = Math.PI / 180;
                const dLat = (b[0] - a[0]) * rad;
                const dLon = (b[1] - a[1]) * rad;
                const h = Math.sin(dLat / 2) ** 2 +
                    Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLon / 2) ** 2;
                return 2 * 6371000 * Math.asin(Math.sqrt(h));
            }

            // Calcule distances cumulées, altitudes lissées et pente (en %) de chaque tronçon.
            function analyserProfil(trace) {
                if (!Array.isArray(trace) || trace.length < 3) return null;
                if (trace.some(p => !Array.isArray(p) || typeof p[3] !== 'number')) return null;

                const n = trace.length;
                const cum = [0];
                for (let i = 1; i < n; i++) {
                    cum.push(cum[i - 1] + distanceLatLon(trace[i - 1], trace[i]));
                }
                if (cum[n - 1] < 100) return null;

                const brut = trace.map(p => p[3]);
                const alt = brut.map((_, i) => {
                    let somme = 0;
                    let nb = 0;
                    for (let j = Math.max(0, i - 2); j <= Math.min(n - 1, i + 2); j++) {
                        somme += brut[j];
                        nb++;
                    }
                    return somme / nb;
                });

                const pentes = [];
                for (let i = 0; i < n - 1; i++) {
                    let a = i;
                    let b = i + 1;
                    while (cum[b] - cum[a] < 120 && (a > 0 || b < n - 1)) {
                        if (a > 0) a--;
                        if (cum[b] - cum[a] < 120 && b < n - 1) b++;
                    }
                    const d = cum[b] - cum[a];
                    pentes.push(d > 0 ? ((alt[b] - alt[a]) / d) * 100 : 0);
                }
                return { cum: cum, alt: alt, pentes: pentes };
            }

            function classerChemin(tags) {
                const route = tags.highway || '';
                const surface = tags.surface || '';
                const dur = ['asphalt', 'paved', 'concrete', 'paving_stones', 'sett', 'chipseal', 'concrete:plates', 'concrete:lanes'];
                const meuble = ['unpaved', 'gravel', 'fine_gravel', 'compacted', 'dirt', 'ground', 'earth', 'grass', 'sand', 'mud', 'pebblestone', 'rock', 'woodchips', 'grass_paver'];

                if (route === 'cycleway') return 'v';
                if (dur.includes(surface)) return 'r';
                if (meuble.includes(surface)) {
                    return ['path', 'footway', 'bridleway', 'steps'].includes(route) ? 's' : 'c';
                }
                if (['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential', 'service', 'living_street', 'road'].includes(route)) return 'r';
                if (route === 'track') return 'c';
                if (['path', 'footway', 'bridleway', 'steps'].includes(route)) return 's';
                return 'u';
            }

            // Interroge OpenStreetMap (Overpass) et attribue un type de chemin à chaque point du tracé.
            async function chercherTypesChemin(trace) {
                const lats = trace.map(p => p[0]);
                const lons = trace.map(p => p[1]);
                const marge = 0.0004;
                const sud = Math.min(...lats) - marge;
                const nord = Math.max(...lats) + marge;
                const ouest = Math.min(...lons) - marge;
                const est = Math.max(...lons) + marge;
                if ((nord - sud) > 0.5 || (est - ouest) > 0.7) {
                    throw new Error('trop grand');
                }

                const requete = `[out:json][timeout:40];way["highway"](${sud},${ouest},${nord},${est});out tags geom qt;`;
                const serveurs = [
                    'https://overpass-api.de/api/interpreter',
                    'https://overpass.kumi.systems/api/interpreter'
                ];
                let donnees = null;

                for (const url of serveurs) {
                    try {
                        const res = await fetch(url, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                            body: 'data=' + encodeURIComponent(requete)
                        });
                        if (res.ok) {
                            donnees = await res.json();
                            break;
                        }
                    } catch (e) {
                        console.error(e);
                    }
                }
                if (!donnees || !Array.isArray(donnees.elements)) throw new Error('overpass');

                const lat0 = (sud + nord) / 2;
                const kx = Math.cos(lat0 * Math.PI / 180) * 111320;
                const ky = 110540;
                const voies = donnees.elements
                    .filter(e => e.geometry && e.geometry.length > 1 && e.tags)
                    .map(e => ({
                        code: classerChemin(e.tags),
                        pts: e.geometry.map(g => [g.lon * kx, g.lat * ky])
                    }));

                const distSegment = (px, py, a, b) => {
                    const dx = b[0] - a[0];
                    const dy = b[1] - a[1];
                    const l2 = dx * dx + dy * dy;
                    let t = l2 > 0 ? ((px - a[0]) * dx + (py - a[1]) * dy) / l2 : 0;
                    t = Math.max(0, Math.min(1, t));
                    return Math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy));
                };

                let codes = '';
                for (const p of trace) {
                    const px = p[1] * kx;
                    const py = p[0] * ky;
                    let meilleur = 'u';
                    let ecart = 30;
                    for (const v of voies) {
                        for (let i = 0; i < v.pts.length - 1; i++) {
                            const a = v.pts[i];
                            const b = v.pts[i + 1];
                            if (px < Math.min(a[0], b[0]) - 30 || px > Math.max(a[0], b[0]) + 30 ||
                                py < Math.min(a[1], b[1]) - 30 || py > Math.max(a[1], b[1]) + 30) continue;
                            const d = distSegment(px, py, a, b);
                            if (d < ecart) { ecart = d; meilleur = v.code; }
                        }
                    }
                    codes += meilleur;
                }
                return codes;
            }

            function resumeChemins(codes, cum) {
                const total = {};
                for (let i = 0; i < codes.length - 1; i++) {
                    total[codes[i]] = (total[codes[i]] || 0) + (cum[i + 1] - cum[i]);
                }
                const somme = cum[cum.length - 1] || 1;
                return Object.keys(TYPES_CHEMIN)
                    .filter(k => total[k])
                    .map(k => ({ code: k, part: Math.round((total[k] / somme) * 100) }));
            }

            function legendeProfil(elements) {
                const l = document.createElement('div');
                l.className = 'profil-legende';
                elements.forEach(e => {
                    const sp = document.createElement('span');
                    const i = document.createElement('i');
                    i.style.background = e.couleur;
                    sp.appendChild(i);
                    sp.appendChild(document.createTextNode(e.texte));
                    l.appendChild(sp);
                });
                return l;
            }

            // Pas des repères kilométriques selon la longueur de la sortie.
            function pasKm(totalM) {
                if (totalM <= 3000) return 0.5;
                if (totalM <= 8000) return 1;
                if (totalM <= 15000) return 2;
                if (totalM <= 40000) return 5;
                return 10;
            }

            // Dessine le profil (pente en couleurs, type de chemin, repères en km).
            // `grand` : version de la fenêtre (axe des altitudes, plus de place).
            function construireProfil(s, profil, largeur, hauteur, grand, liste) {
                const ns = 'http://www.w3.org/2000/svg';
                const codes = s.surfaces && s.surfaces.length === s.trace.length ? s.surfaces : null;
                const police = grand ? 12 : 16;
                const margeG = grand ? 48 : 0;
                const hautBande = codes ? (grand ? 16 : 20) : 0;
                const bas = hautBande + (codes ? 10 : 0) + police + 8;
                const hautGraph = hauteur - bas;
                const total = profil.cum[profil.cum.length - 1];
                const mini = Math.min(...profil.alt);
                const maxi = Math.max(...profil.alt);
                // À la verticale : un peu de marge pour ne pas exagérer les pics.
                // À l'horizontale : le graphique utilise toute la hauteur, comme avant.
                const paysageEcran = document.documentElement.classList.contains('paysage');
                const etendue = paysageEcran
                    ? Math.max(30, maxi - mini)
                    : Math.max(60, (maxi - mini) * 1.3, liste ? total / 50 : total / 30);
                const x = (d) => margeG + (d / total) * (largeur - margeG);
                const y = (a) => 8 + (1 - (a - mini) / etendue) * (hautGraph - 16);

                const svg = document.createElementNS(ns, 'svg');
                svg.setAttribute('viewBox', `0 0 ${largeur} ${hauteur}`);
                svg.setAttribute('role', 'img');
                svg.setAttribute('aria-label', 'Profil de pente de la sortie');

                const ajouter = (nom, attrs, parent) => {
                    const el = document.createElementNS(ns, nom);
                    Object.keys(attrs).forEach(k => el.setAttribute(k, attrs[k]));
                    (parent || svg).appendChild(el);
                    return el;
                };
                const texte = (contenu, attrs) => {
                    const t = ajouter('text', attrs);
                    t.setAttribute('font-size', police);
                    t.style.fill = 'var(--text-muted)';
                    t.textContent = contenu;
                    return t;
                };

                for (let k = 0; k < profil.pentes.length; k++) {
                    const x0 = x(profil.cum[k]);
                    const x1 = x(profil.cum[k + 1]) + 0.6;
                    ajouter('polygon', {
                        points: `${x0},${hautGraph} ${x0},${y(profil.alt[k])} ${x1},${y(profil.alt[k + 1])} ${x1},${hautGraph}`,
                        fill: couleurPente(profil.pentes[k])
                    });
                }

                if (codes) {
                    for (let k = 0; k < codes.length - 1; k++) {
                        ajouter('rect', {
                            x: x(profil.cum[k]),
                            y: hautGraph + 8,
                            width: x(profil.cum[k + 1]) - x(profil.cum[k]) + 0.6,
                            height: hautBande,
                            fill: (TYPES_CHEMIN[codes[k]] || TYPES_CHEMIN.u).couleur
                        });
                    }
                }

                // Repères kilométriques
                const pas = pasKm(total) * 1000;
                const baseAxe = hauteur - 5;
                for (let d = pas; d < total * 0.88; d += pas) {
                    const xx = x(d);
                    ajouter('line', {
                        x1: xx, x2: xx, y1: 0, y2: hautGraph + (codes ? 8 + hautBande : 4),
                        stroke: 'rgba(255,255,255,0.7)', 'stroke-width': 1, 'stroke-dasharray': '3 3'
                    });
                    const km = d / 1000;
                    const libelle = unites.distance === 'km'
                        ? (Number.isInteger(km) ? `${km} km` : `${km.toFixed(1)} km`)
                        : fmtDistanceProfil(km);
                    texte(libelle, { x: xx, y: baseAxe, 'text-anchor': 'middle' });
                }
                texte('0', { x: x(0) + (grand ? 0 : 2), y: baseAxe, 'text-anchor': grand ? 'middle' : 'start' });
                texte(fmtDistanceProfil(total / 1000), { x: largeur - 2, y: baseAxe, 'text-anchor': 'end' });

                if (grand) {
                    [mini, (mini + maxi) / 2, maxi].forEach(a => {
                        ajouter('line', {
                            x1: margeG, x2: largeur, y1: y(a), y2: y(a),
                            stroke: 'rgba(255,255,255,0.25)', 'stroke-width': 1
                        });
                        texte(fmtAltitude(a).complet, { x: margeG - 4, y: y(a) + 4, 'text-anchor': 'end' });
                    });
                }

                return { svg: svg, x: x, total: total, codes: codes, mini: mini, maxi: maxi, margeG: margeG };
            }

            function legendesProfil(s, profil) {
                const zone = document.createElement('div');
                zone.appendChild(legendeProfil(COULEURS_PENTE.map(c => ({ couleur: c.couleur, texte: c.nom }))));
                const codes = s.surfaces && s.surfaces.length === s.trace.length ? s.surfaces : null;
                if (codes) {
                    zone.appendChild(legendeProfil(
                        resumeChemins(codes, profil.cum).map(r => ({
                            couleur: TYPES_CHEMIN[r.code].couleur,
                            texte: `${TYPES_CHEMIN[r.code].nom} ${r.part} %`
                        }))
                    ));
                }
                return zone;
            }

            function indiceA(cum, d) {
                let bas = 0;
                let haut = cum.length - 1;
                while (haut - bas > 1) {
                    const m = (bas + haut) >> 1;
                    if (cum[m] <= d) bas = m; else haut = m;
                }
                return bas;
            }

            // Fenêtre plein écran : profil agrandi, on touche le graphique pour lire chaque point.
            function ouvrirProfil(s) {
                const profil = analyserProfil(s.trace);
                if (!profil) return;
                profilAffiche = s;

                const fenetre = document.getElementById('fenetreProfil');
                const zone = document.getElementById('fpGraph');
                const info = document.getElementById('fpInfo');
                const jour = new Date(s.debut).toLocaleDateString('fr-FR');
                document.getElementById('fpTitre').textContent = `${s.nom} · ${jour}`;
                zone.textContent = '';
                fenetre.style.display = 'flex';

                const largeur = Math.max(300, Math.min(900, zone.clientWidth || 340));
                const paysage = window.innerHeight < 520 && window.innerWidth > window.innerHeight;
                const hauteur = paysage ? 170 : (largeur > 600 ? 320 : 250);
                const g = construireProfil(s, profil, largeur, hauteur, true);
                zone.appendChild(g.svg);

                const resume = [
                    fmtDistanceProfil(g.total / 1000),
                    fmtAltitude(s.denivele_pos).texte + ' ' + fmtAltitude(s.denivele_pos).unite + ' D+',
                    fmtAltitude(s.denivele_neg).texte + ' ' + fmtAltitude(s.denivele_neg).unite + ' D-',
                    `pente max ${Math.round(Math.max(...profil.pentes.map(Math.abs)))} %`
                ].join(' · ');
                info.textContent = `${resume} — touche le graphique pour lire un point`;

                const NS = 'http://www.w3.org/2000/svg';
                const curseur = document.createElementNS(NS, 'line');
                curseur.setAttribute('y1', 0);
                curseur.setAttribute('y2', hauteur);
                curseur.setAttribute('stroke', '#ffffff');
                curseur.setAttribute('stroke-width', 2);
                curseur.style.display = 'none';
                g.svg.appendChild(curseur);

                const lire = (evenement) => {
                    const boite = g.svg.getBoundingClientRect();
                    const px = ((evenement.clientX - boite.left) / boite.width) * largeur;
                    const d = Math.max(0, Math.min(g.total, ((px - g.margeG) / (largeur - g.margeG)) * g.total));
                    const k = indiceA(profil.cum, d);
                    const pente = profil.pentes[Math.min(k, profil.pentes.length - 1)];
                    const fleche = pente > 0.5 ? '↑' : (pente < -0.5 ? '↓' : '→');
                    let ligne = `${fmtDistanceProfil(d / 1000)} · ${fmtAltitude(profil.alt[k]).complet} · pente ${Math.round(Math.abs(pente))} % ${fleche}`;
                    if (g.codes) {
                        ligne += ` · ${(TYPES_CHEMIN[g.codes[k]] || TYPES_CHEMIN.u).nom}`;
                    }
                    info.textContent = ligne;
                    const xx = g.x(d);
                    curseur.setAttribute('x1', xx);
                    curseur.setAttribute('x2', xx);
                    curseur.style.display = '';
                };
                g.svg.style.touchAction = 'none';
                g.svg.addEventListener('pointerdown', lire);
                g.svg.addEventListener('pointermove', (e) => {
                    if (e.buttons || e.pointerType === 'touch') lire(e);
                });
                g.svg.addEventListener('mousemove', lire);

                const leg = document.getElementById('fpLegendes');
                leg.textContent = '';
                leg.appendChild(legendesProfil(s, profil));
            }

            function fermerProfil() {
                profilAffiche = null;
                document.getElementById('fenetreProfil').style.display = 'none';
            }

            // Suit l'orientation de l'écran (le téléphone peut tourner à tout moment).
            let profilAffiche = null;

            function classeOrientation() {
                const paysage = window.innerWidth > window.innerHeight && window.innerHeight <= 520;
                document.documentElement.classList.toggle('paysage', paysage);
            }

            function majOrientation() {
                classeOrientation();

                try {
                    if (profilAffiche && document.getElementById('fenetreProfil').style.display !== 'none') {
                        ouvrirProfil(profilAffiche);
                    }
                    if (v3d.carte && typeof v3d.carte.resize === 'function') {
                        v3d.carte.resize();
                    }
                } catch (e) {
                    console.error(e);
                }
            }

            function surRotation() {
                majOrientation();
                // Selon les téléphones, la nouvelle taille d'écran arrive avec un peu de retard.
                setTimeout(majOrientation, 150);
                setTimeout(majOrientation, 450);
            }

            window.addEventListener('resize', surRotation);
            window.addEventListener('orientationchange', surRotation);
            if (window.visualViewport) {
                window.visualViewport.addEventListener('resize', surRotation);
            }
            if (window.matchMedia) {
                const suivi = window.matchMedia('(orientation: landscape)');
                if (suivi.addEventListener) suivi.addEventListener('change', surRotation);
            }
            classeOrientation();

            (function brancherProfil() {
                remplirBouton(document.getElementById('fpFermer'), 'fermer', '');
                document.getElementById('fpFermer').addEventListener('click', fermerProfil);
                document.getElementById('fenetreProfil').addEventListener('click', (e) => {
                    if (e.target.id === 'fenetreProfil') fermerProfil();
                });
                document.addEventListener('keydown', (e) => {
                    if (e.key === 'Escape') fermerProfil();
                });
            })();

            // Profil d'une sortie dans la liste : repères en km, clic = fenêtre agrandie.
            function dessinerProfil(s) {
                const profil = analyserProfil(s.trace);
                if (!profil) return null;

                const g = construireProfil(
                    s, profil, 600,
                    document.documentElement.classList.contains('paysage') ? 175 : 270,
                    false, true
                );
                g.svg.style.cursor = 'pointer';
                g.svg.addEventListener('click', () => ouvrirProfil(s));

                const bloc = document.createElement('div');
                bloc.className = 'profil-sortie';
                bloc.appendChild(g.svg);

                const info = document.createElement('div');
                info.className = 'profil-legende';
                info.textContent = `Altitude ${fmtAltitude(g.mini).texte} à ${fmtAltitude(g.maxi).complet} sur ${fmtDistance(g.total / 1000, 1).complet} · touche le profil pour l'agrandir`;
                bloc.appendChild(info);
                bloc.appendChild(legendesProfil(s, profil));

                if (!g.codes) {
                    const zoneAnalyse = document.createElement('div');
                    zoneAnalyse.className = 'profil-analyse';
                    zoneAnalyse.dataset.analyse = String(s.id);
                    rendreAnalyse(zoneAnalyse, s);
                    bloc.appendChild(zoneAnalyse);
                }

                return bloc;
            }

            // Répartition réelle du terrain d'une sortie analysée (en % de la distance).
            function repartitionTerrain(s) {
                const codes = s.surfaces;
                const tr = s.trace;
                if (!codes || !Array.isArray(tr) || codes.length !== tr.length || tr.length < 2) return null;

                const tot = { r: 0, c: 0, s: 0, u: 0 };
                let somme = 0;
                for (let i = 0; i < tr.length - 1; i++) {
                    const d = distanceLatLon(tr[i], tr[i + 1]);
                    const k = codes[i] === 'v' ? 'r' : (codes[i] in tot ? codes[i] : 'u');
                    tot[k] += d;
                    somme += d;
                }
                if (somme <= 0) return null;

                const noms = [['r', 'route'], ['c', 'chemin'], ['s', 'sentier'], ['u', 'inconnu']];
                const parts = noms
                    .map(([k, nom]) => ({ nom: nom, k: k, pct: Math.round((tot[k] / somme) * 100) }))
                    .filter(x => x.pct > 0);
                return {
                    texte: parts.map(x => `${x.pct} % ${x.nom}`).join(' · '),
                    inconnu: tot.u / somme
                };
            }

            // ---------- Analyse automatique du type de chemin ----------
            const etatAnalyse = {};
            const dejaEssayees = new Set();
            let analyseEnCours = false;
            const MAX_ANALYSES_AUTO = 30;

            function pauseMs(ms) {
                return new Promise(resolve => setTimeout(resolve, ms));
            }

            // Affiche, dans le bloc profil, l'état de l'analyse (en cours, ou bouton pour relancer).
            function rendreAnalyse(zone, sortie) {
                zone.textContent = '';
                const etat = etatAnalyse[sortie.id];

                if (etat === 'attente' || etat === 'cours') {
                    const t = document.createElement('div');
                    t.className = 'profil-legende';
                    t.textContent = etat === 'cours'
                        ? "Analyse du terrain en cours (route, chemin, sentier)..."
                        : "Analyse du terrain en attente...";
                    zone.appendChild(t);
                    return;
                }

                const bouton = document.createElement('button');
                bouton.className = 'btn-action';
                bouton.style.width = 'auto';
                bouton.style.padding = '6px 12px';
                bouton.style.marginTop = '8px';
                bouton.textContent = etat === 'echec'
                    ? "Analyse impossible, réessayer"
                    : "Analyser le type de chemin";
                bouton.addEventListener('click', () => {
                    dejaEssayees.delete(sortie.id);
                    analyserSorties([sortie]);
                });
                zone.appendChild(bouton);
            }

            function majEtatAnalyse(sortie, etat) {
                etatAnalyse[sortie.id] = etat;
                const zone = document.querySelector(`[data-analyse="${sortie.id}"]`);
                if (zone) rendreAnalyse(zone, sortie);
            }

            const fileAnalyses = [];

            async function analyserSorties(liste) {
                liste.forEach(sortie => {
                    if (!fileAnalyses.includes(sortie)) {
                        fileAnalyses.push(sortie);
                        majEtatAnalyse(sortie, 'attente');
                    }
                });
                if (analyseEnCours) return;
                analyseEnCours = true;
                let reussies = 0;

                while (fileAnalyses.length > 0) {
                    const sortie = fileAnalyses.shift();
                    dejaEssayees.add(sortie.id);
                    majEtatAnalyse(sortie, 'cours');
                    try {
                        const codes = await chercherTypesChemin(sortie.trace);
                        const res = await fetch(`/api/sorties/${sortie.id}/surfaces`, {
                            method: 'POST',
                            headers: getHeaders(),
                            body: JSON.stringify({ codes: codes })
                        });
                        if (!res.ok) throw new Error('enregistrement');
                        sortie.surfaces = codes;
                        majEtatAnalyse(sortie, 'ok');
                        reussies++;
                    } catch (e) {
                        console.error(e);
                        majEtatAnalyse(sortie, 'echec');
                    }
                    await pauseMs(fileAnalyses.length > 0 ? 1500 : 0);
                }

                analyseEnCours = false;

                if (reussies > 0) {
                    await chargerSorties();
                    await chargerEtat();
                }
            }

            // Lance l'analyse des sorties qui n'en ont pas encore (les plus récentes d'abord).
            function analyserAuto(sorties) {
                if (analyseEnCours) return;
                const afaire = sorties
                    .filter(x => !x.surfaces && Array.isArray(x.trace) && x.trace.length >= 2
                        && !dejaEssayees.has(x.id))
                    .slice(0, MAX_ANALYSES_AUTO);
                if (afaire.length > 0) analyserSorties(afaire);
            }

            // Ouvre le menu vers le haut s'il dépasserait en bas de l'écran (sous la barre d'onglets).
            function placerMenuSortie(menu) {
                menu.classList.remove('vers-haut');
                const limiteBas = window.innerHeight - 96;
                if (menu.getBoundingClientRect().bottom <= limiteBas) return;
                menu.classList.add('vers-haut');
                if (menu.getBoundingClientRect().top < 70) {
                    menu.classList.remove('vers-haut');
                    window.scrollBy(0, Math.ceil(menu.getBoundingClientRect().bottom - limiteBas));
                }
            }

            function fermerMenusSortie() {
                document.querySelectorAll('.menu-sortie.ouvert').forEach(m => m.classList.remove('ouvert'));
                document.querySelectorAll('.menu-sortie-btn').forEach(b => b.setAttribute('aria-expanded', 'false'));
            }

            document.addEventListener('click', fermerMenusSortie);
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') fermerMenusSortie();
            });

            let sortiesConnues = [];
            let sortiesPeriode = { debut: '', fin: '' };

            function initialiserMenuFiltresSorties() {
                const zone = document.getElementById('sortiesMenuZone');
                const panneau = document.getElementById('sortiesFiltres');
                if (!zone || !panneau || document.getElementById('sortiesFiltreBouton')) return;

                const bouton = document.createElement('button');
                bouton.type = 'button';
                bouton.id = 'sortiesFiltreBouton';
                bouton.className = 'menu-sortie-btn hist-filtre-btn';
                bouton.setAttribute('aria-label', 'Filtrer et rechercher les sorties');
                bouton.setAttribute('aria-haspopup', 'true');
                bouton.setAttribute('aria-expanded', 'false');
                remplirBouton(bouton, 'filtre', '');
                bouton.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const etaitOuvert = panneau.classList.contains('ouvert');
                    fermerMenusSortie();
                    if (!etaitOuvert) {
                        // Remonte la page pour que tout le panneau tienne à l'écran.
                        const haut = bouton.getBoundingClientRect().top;
                        if (haut > 160) window.scrollBy(0, haut - 130);
                        panneau.classList.add('ouvert');
                        positionnerPanneauFiltresSorties();
                        bouton.setAttribute('aria-expanded', 'true');
                    }
                });
                panneau.addEventListener('click', (e) => e.stopPropagation());
                const boutonReinitialiser = document.getElementById('reinitialiserFiltresSorties');
                if (boutonReinitialiser) {
                    boutonReinitialiser.addEventListener('click', (e) => {
                        e.stopPropagation();
                        reinitialiserFiltresSorties();
                        panneau.classList.remove('ouvert');
                        bouton.setAttribute('aria-expanded', 'false');
                    });
                }
                // Place le panneau dans l'en-tête positionné : il s'ouvre sous l'icône filtre.
                zone.appendChild(panneau);
                zone.appendChild(bouton);

                // Le menu trois points est conservé, mais reste vide pour le moment.
                ajouterMenuPoints(zone, 'Actions des sorties', [
                    { icone: 'importer', texte: 'Importer des fichiers GPX', action: () => ouvrirImport('gpx') },
                    { icone: 'importer', texte: 'Importer une archive Strava (.zip)', action: () => ouvrirImport('strava') }
                ]);
                const fermerImportBtn = document.getElementById('importFermer');
                if (fermerImportBtn) remplirBouton(fermerImportBtn, 'fermer', '');
                // Garder le panneau aligné sous l'icône pendant le défilement
                // et lorsque l'orientation ou la largeur du téléphone change.
                window.addEventListener('scroll', positionnerPanneauFiltresSorties, true);
                window.addEventListener('resize', positionnerPanneauFiltresSorties);
                actualiserBoutonFiltreSorties();
            }

            // ---- Favoris (gardés sur cet appareil) et partage ----
            let sortiesFavorisSeuls = false;

            function lireFavoris() {
                try {
                    const l = JSON.parse(localStorage.getItem('maillons_favoris') || '[]');
                    return Array.isArray(l) ? l.map(String) : [];
                } catch (e) { return []; }
            }

            function estFavori(id) { return lireFavoris().includes(String(id)); }

            function basculerFavori(id) {
                const liste = lireFavoris();
                const cle = String(id);
                const i = liste.indexOf(cle);
                if (i >= 0) liste.splice(i, 1); else liste.push(cle);
                try { localStorage.setItem('maillons_favoris', JSON.stringify(liste)); } catch (e) { /* stockage refusé */ }
                return i < 0;
            }

            function basculerFiltreFavoris() {
                sortiesFavorisSeuls = !sortiesFavorisSeuls;
                appliquerFiltresSorties();
            }

            function majBoutonFiltreFavoris() {
                const b = document.getElementById('favorisSortiesBtn');
                if (!b) return;
                remplirBouton(b, 'coeur', sortiesFavorisSeuls ? 'Favoris seulement' : 'Toutes les sorties');
                b.classList.toggle('actif', sortiesFavorisSeuls);
                b.setAttribute('aria-pressed', sortiesFavorisSeuls ? 'true' : 'false');
            }

            // Image de partage façon Komoot : carte avec le parcours, profil et chiffres clés.
            function chargerImageCarte(url) {
                return new Promise(resolve => {
                    const img = new Image();
                    const fin = setTimeout(() => resolve(null), 10000);
                    img.crossOrigin = 'anonymous';
                    img.onload = () => { clearTimeout(fin); resolve(img); };
                    img.onerror = () => { clearTimeout(fin); resolve(null); };
                    img.src = url;
                });
            }

            async function creerImagePartage(s, typeFond) {
                const brut = (Array.isArray(s.trace) ? s.trace : []).filter(p => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]));
                if (brut.length < 2) return null;
                const pas = Math.max(1, Math.ceil(brut.length / 1500));
                const pts = brut.filter((p, k) => k % pas === 0 || k === brut.length - 1);
                const L = 1080, H = 1350, MH = 860, MARGE = 120;
                const proj = (lat, lon, W) => {
                    const r = lat * Math.PI / 180;
                    return [(lon + 180) / 360 * W, (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * W];
                };
                const lats = pts.map(p => p[0]), lons = pts.map(p => p[1]);
                const sud = Math.min(...lats), nord = Math.max(...lats), ouest = Math.min(...lons), est = Math.max(...lons);
                let z = 16;
                for (; z > 6; z--) {
                    const W = 512 * Math.pow(2, z);
                    const a = proj(nord, ouest, W), b = proj(sud, est, W);
                    if (b[0] - a[0] <= L - 2 * MARGE && b[1] - a[1] <= MH - 2 * MARGE) break;
                }
                const W = 512 * Math.pow(2, z);
                const hautGauche = proj(nord, ouest, W), basDroite = proj(sud, est, W);
                const ox = (hautGauche[0] + basDroite[0]) / 2 - L / 2;
                const oy = (hautGauche[1] + basDroite[1]) / 2 - MH / 2;

                const canvas = document.createElement('canvas');
                canvas.width = L; canvas.height = H;
                const ctx = canvas.getContext('2d');

                // Fond de carte (tuiles CARTO / OpenStreetMap)
                ctx.save();
                ctx.beginPath(); ctx.rect(0, 0, L, MH); ctx.clip();
                const fond = ctx.createLinearGradient(0, 0, 0, MH);
                fond.addColorStop(0, '#dfeceb'); fond.addColorStop(1, '#c6dcd9');
                ctx.fillStyle = fond; ctx.fillRect(0, 0, L, MH);
                let tuilesOk = 0, mention = '';
                if (typeFond) {
                    // Esri : tuiles de 256 px, d'où un niveau de zoom de plus pour la même échelle.
                    const sat = typeFond === 'sat';
                    const zt = sat ? z + 1 : z, taille = sat ? 256 : 512, n = Math.pow(2, zt);
                    const pxMonde = taille * n;
                    const k = pxMonde / W;
                    const taches = [];
                    for (let ty = Math.floor(oy * k / taille); ty <= Math.floor((oy + MH) * k / taille); ty++) {
                        if (ty < 0 || ty >= n) continue;
                        for (let tx = Math.floor(ox * k / taille); tx <= Math.floor((ox + L) * k / taille); tx++) {
                            const txm = ((tx % n) + n) % n;
                            const url = sat
                                ? 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/' + zt + '/' + ty + '/' + txm
                                : 'https://' + 'abcd'.charAt((txm + ty) % 4) + '.basemaps.cartocdn.com/rastertiles/voyager/' + zt + '/' + txm + '/' + ty + '@2x.png';
                            taches.push(chargerImageCarte(url).then(img => {
                                if (img) { ctx.drawImage(img, tx * taille / k - ox, ty * taille / k - oy, taille / k, taille / k); tuilesOk++; }
                            }));
                        }
                    }
                    await Promise.all(taches);
                    if (!tuilesOk) throw new Error('aucune tuile de carte chargée');
                    mention = sat ? '© Esri, Maxar, Earthstar Geographics' : '© OpenStreetMap · © CARTO';
                }
                // Parcours : coloré selon la pente, comme sur la carte de la fiche
                const complet = (Array.isArray(s.trace) ? s.trace : []);
                let profilPartage = null;
                try { profilPartage = analyserProfil(complet); } catch (e) { profilPartage = null; }
                const valides = brut;
                const pasP = Math.max(1, Math.ceil(valides.length / 1500));
                const idx = [];
                for (let k = 0; k < valides.length; k += pasP) idx.push(k);
                if (idx[idx.length - 1] !== valides.length - 1) idx.push(valides.length - 1);
                const xy = idx.map(k => { const q = proj(valides[k][0], valides[k][1], W); return [q[0] - ox, q[1] - oy]; });
                const pentesOk = profilPartage && profilPartage.pentes && profilPartage.pentes.length === complet.length - 1 && valides.length === complet.length;
                ctx.lineJoin = 'round'; ctx.lineCap = 'round';
                ctx.beginPath();
                xy.forEach((q, k) => { if (k === 0) ctx.moveTo(q[0], q[1]); else ctx.lineTo(q[0], q[1]); });
                ctx.lineWidth = 22; ctx.strokeStyle = 'rgba(255,255,255,0.95)'; ctx.stroke();
                for (let k = 0; k < xy.length - 1; k++) {
                    let pente = null;
                    if (pentesOk) {
                        const debutIdx = idx[k], finIdx = idx[k + 1];
                        let somme = 0, nb = 0;
                        for (let m = debutIdx; m < finIdx; m++) { somme += profilPartage.pentes[m]; nb++; }
                        pente = nb ? somme / nb : null;
                    }
                    ctx.beginPath(); ctx.moveTo(xy[k][0], xy[k][1]); ctx.lineTo(xy[k + 1][0], xy[k + 1][1]);
                    ctx.lineWidth = 12; ctx.strokeStyle = pente === null ? '#35e0c8' : couleurPente(pente); ctx.stroke();
                }
                const pastille = (q, couleur) => {
                    ctx.beginPath(); ctx.arc(q[0], q[1], 19, 0, Math.PI * 2);
                    ctx.fillStyle = couleur; ctx.fill(); ctx.lineWidth = 7; ctx.strokeStyle = '#fff'; ctx.stroke();
                };
                const debut = xy[0], fin = xy[xy.length - 1];
                pastille(debut, '#22c55e');
                if (Math.hypot(fin[0] - debut[0], fin[1] - debut[1]) > 40) pastille(fin, '#ef4444');
                // Mention obligatoire des sources de la carte
                if (tuilesOk && mention) {
                    ctx.font = '22px system-ui, sans-serif';
                    const lm = ctx.measureText(mention).width + 20;
                    ctx.fillStyle = 'rgba(0,0,0,0.55)'; ctx.fillRect(L - lm, MH - 34, lm, 34);
                    ctx.fillStyle = '#ffffff'; ctx.textAlign = 'left'; ctx.fillText(mention, L - lm + 10, MH - 10);
                }
                ctx.restore();

                // Bandeau du bas
                ctx.fillStyle = '#0b1a20'; ctx.fillRect(0, MH, L, H - MH);
                const barre = ctx.createLinearGradient(0, 0, L, 0);
                barre.addColorStop(0, '#35e0c8'); barre.addColorStop(1, '#0a8f9c');
                ctx.fillStyle = barre; ctx.fillRect(0, MH, L, 8);
                ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
                ctx.fillStyle = '#ffffff';
                let taille = 60;
                ctx.font = '800 ' + taille + 'px system-ui, sans-serif';
                while (ctx.measureText(s.nom || 'Sortie').width > L - 100 && taille > 32) { taille -= 4; ctx.font = '800 ' + taille + 'px system-ui, sans-serif'; }
                ctx.fillText(s.nom || 'Sortie', 50, MH + 92);
                ctx.fillStyle = '#9fb4ba'; ctx.font = '500 32px system-ui, sans-serif';
                ctx.fillText(new Date(s.debut).toLocaleDateString('fr-FR', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }), 50, MH + 140);

                // Profil d'altitude
                const alts = brut.map(p => Number(p[3])).filter(a => Number.isFinite(a));
                if (alts.length > brut.length * 0.5 && alts.length > 5) {
                    const pasA = Math.max(1, Math.ceil(alts.length / 300));
                    const a2 = alts.filter((a, k) => k % pasA === 0);
                    const mn = Math.min(...a2), mx = Math.max(...a2), x0 = 50, x1 = L - 50, y0 = MH + 170, y1 = MH + 250;
                    ctx.beginPath(); ctx.moveTo(x0, y1);
                    a2.forEach((a, k) => ctx.lineTo(x0 + (x1 - x0) * k / Math.max(1, a2.length - 1), y1 - (y1 - y0) * (mx > mn ? (a - mn) / (mx - mn) : 0.5)));
                    ctx.lineTo(x1, y1); ctx.closePath();
                    const rempl = ctx.createLinearGradient(0, y0, 0, y1);
                    rempl.addColorStop(0, 'rgba(53,224,200,0.75)'); rempl.addColorStop(1, 'rgba(53,224,200,0.12)');
                    ctx.fillStyle = rempl; ctx.fill();
                }

                // Chiffres clés
                const chiffres = [
                    [unites.distance === 'km' ? String(s.distance_km).replace('.', ',') : fmtDistance(s.distance_km).texte, fmtDistance(s.distance_km).unite],
                    [unites.altitude === 'm' ? String(Math.round(s.denivele_pos)) : fmtAltitude(s.denivele_pos).texte, fmtAltitude(s.denivele_pos).unite + ' D+'],
                    [formaterDuree(s.duree_s), 'durée'],
                    [unites.vitesse === 'kmh' ? String(s.vitesse_moy).replace('.', ',') : fmtVitesse(s.vitesse_moy).texte, fmtVitesse(s.vitesse_moy).unite]
                ];
                chiffres.forEach((c, k) => {
                    const x = 50 + k * ((L - 100) / 4);
                    ctx.fillStyle = '#ffffff'; ctx.font = '800 50px system-ui, sans-serif'; ctx.fillText(c[0], x, MH + 340);
                    ctx.fillStyle = '#9fb4ba'; ctx.font = '500 28px system-ui, sans-serif'; ctx.fillText(c[1], x, MH + 380);
                });
                // Logo Maillons (le même que dans l'en-tête de l'appli)
                const svgLogo = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="192" height="192">'
                    + '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#35e0c8"/><stop offset="1" stop-color="#0a8f9c"/></linearGradient></defs>'
                    + '<rect width="96" height="96" rx="24" fill="url(#g)"/><g fill="none" stroke-linecap="round">'
                    + '<rect x="14" y="34" width="42" height="22" rx="11" transform="rotate(-32 35 45)" stroke="#06262b" stroke-width="9"/>'
                    + '<rect x="40" y="42" width="42" height="22" rx="11" transform="rotate(-32 61 53)" stroke="#06262b" stroke-width="9"/>'
                    + '<rect x="14" y="34" width="42" height="22" rx="11" transform="rotate(-32 35 45)" stroke="#fff" stroke-width="5"/>'
                    + '<rect x="40" y="42" width="42" height="22" rx="11" transform="rotate(-32 61 53)" stroke="#fff" stroke-width="5"/></g></svg>';
                const logo = await chargerImageCarte('data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgLogo));
                ctx.textAlign = 'right'; ctx.fillStyle = '#35e0c8'; ctx.font = '800 38px system-ui, sans-serif';
                ctx.fillText('Maillons', L - 50, H - 38);
                if (logo) {
                    const tl = ctx.measureText('Maillons').width;
                    ctx.drawImage(logo, L - 50 - tl - 16 - 64, H - 38 - 50, 64, 64);
                }

                return await new Promise((resolve, reject) => {
                    try { canvas.toBlob(b => b ? resolve(b) : reject(new Error('image vide')), 'image/jpeg', 0.82); }
                    catch (e) { reject(e); }
                });
            }

            // Fabrique l'image de partage : satellite, sinon plan, sinon fond uni.
            async function fabriquerImagePartage(s) {
                for (const fond of ['sat', 'plan', '']) {
                    try {
                        const blob = await creerImagePartage(s, fond);
                        if (blob) return blob;
                    } catch (e) {
                        console.warn('Image de partage : fond « ' + fond + ' » impossible', e);
                    }
                }
                return null;
            }

            function resumeSortie(s) {
                const jour = new Date(s.debut).toLocaleDateString('fr-FR');
                return s.nom + ' · ' + jour + String.fromCharCode(10)
                    + fmtDistance(s.distance_km).texte + ' ' + fmtDistance(s.distance_km).unite + ' · ' + fmtAltitude(s.denivele_pos).texte + ' ' + fmtAltitude(s.denivele_pos).unite + ' D+ · ' + formaterDuree(s.duree_s) + ' · ' + fmtVitesse(s.vitesse_moy).texte + ' ' + fmtVitesse(s.vitesse_moy).unite;
            }

            // Partage de l'image de la sortie (carte, parcours, chiffres).
            async function partagerSortie(s, bouton) {
                const texte = resumeSortie(s);
                if (bouton) { bouton.classList.add('occupe'); bouton.disabled = true; }
                let blob = null;
                try {
                    blob = await fabriquerImagePartage(s);
                } finally {
                    if (bouton) { bouton.classList.remove('occupe'); bouton.disabled = false; }
                }
                const nomFichier = (String(s.nom || 'sortie').replace(/[^a-z0-9]+/gi, '-').replace(/^-+|-+$/g, '') || 'sortie') + '.jpg';
                if (blob) {
                    const fichier = new File([blob], nomFichier, { type: 'image/jpeg' });
                    if (navigator.canShare && navigator.canShare({ files: [fichier] })) {
                        try {
                            await navigator.share({ files: [fichier], title: s.nom, text: texte });
                        } catch (e) {
                            if (e && e.name !== 'AbortError') console.warn(e);
                        }
                        return;
                    }
                    const lien = document.createElement('a');
                    lien.href = URL.createObjectURL(blob);
                    lien.download = nomFichier;
                    document.body.appendChild(lien);
                    lien.click();
                    lien.remove();
                    setTimeout(() => URL.revokeObjectURL(lien.href), 4000);
                    return;
                }
                if (navigator.share) {
                    try { await navigator.share({ title: s.nom, text: texte }); } catch (e) { /* annulé */ }
                    return;
                }
                try {
                    await navigator.clipboard.writeText(texte);
                    alert("Le partage n'est pas disponible sur cet appareil : le résumé de la sortie est copié.");
                } catch (e) {
                    prompt('Copie ce résumé pour le partager :', texte);
                }
            }

            function ouvrirImport(mode) {
                const bloc = document.getElementById('blocImport');
                if (!bloc) return;
                bloc.setAttribute('data-mode', mode);
                bloc.style.display = 'block';
                const strava = document.getElementById('blocStrava');
                if (strava) strava.open = (mode === 'strava');
                bloc.scrollIntoView({ block: 'center' });
            }

            function fermerImport() {
                const bloc = document.getElementById('blocImport');
                if (bloc) bloc.style.display = 'none';
            }

            function positionnerPanneauFiltresSorties() {
                const bouton = document.getElementById('sortiesFiltreBouton');
                const panneau = document.getElementById('sortiesFiltres');
                if (!bouton || !panneau || !panneau.classList.contains('ouvert')) return;

                // Recalcule la position à chaque défilement : sur mobile, l'icône
                // peut bouger alors que le panneau en position fixe reste immobile.
                const rect = bouton.getBoundingClientRect();
                const marge = 12;
                const largeur = Math.min(340, Math.max(220, window.innerWidth - marge * 2));
                const gauche = Math.max(marge, Math.min(rect.right - largeur, window.innerWidth - largeur - marge));
                panneau.style.position = 'fixed';
                panneau.style.top = `${Math.max(marge, rect.bottom + 6)}px`;
                panneau.style.left = `${gauche}px`;
                panneau.style.right = 'auto';
                panneau.style.width = `${Math.min(340, window.innerWidth - marge * 2)}px`;
                panneau.style.maxWidth = `calc(100vw - ${marge * 2}px)`;
                panneau.style.boxSizing = 'border-box';
                // Si l'écran est trop petit, le panneau défile au lieu de passer sous la barre du bas.
                const hautPanneau = Math.max(marge, rect.bottom + 6);
                panneau.style.maxHeight = `${Math.max(160, window.innerHeight - hautPanneau - 96)}px`;
                panneau.style.overflowY = 'auto';
            }

            function actualiserBoutonFiltreSorties() {
                const bouton = document.getElementById('sortiesFiltreBouton');
                if (!bouton) return;
                const actif = Boolean(
                    (document.getElementById('rechercheSorties').value || '').trim()
                    || sortiesPeriode.debut
                    || sortiesPeriode.fin
                    || kmSaisi('kmMinSorties') !== null
                    || kmSaisi('kmMaxSorties') !== null
                    || Boolean((document.getElementById('zoneSorties') || {}).value)
                    || sortiesFavorisSeuls
                    || document.getElementById('triSorties').value !== 'date-desc'
                );
                majBoutonFiltreFavoris();
                const boutonPeriode = document.getElementById('periodeSortiesBtn');
                if (boutonPeriode) remplirBouton(boutonPeriode, 'calendrier', textePeriodeHist(sortiesPeriode));
                bouton.classList.toggle('actif', actif);
                bouton.setAttribute('aria-label', actif ? 'Filtres des sorties (filtre actif)' : 'Filtrer et rechercher les sorties');
            }

            // Lit un nombre de kilomètres saisi (virgule ou point) ; null si le champ est vide ou invalide.
            function kmSaisi(id) {
                const champ = document.getElementById(id);
                if (!champ) return null;
                const n = parseFloat(String(champ.value || '').trim().replace(',', '.'));
                return Number.isFinite(n) ? n : null;
            }

            // ---- Zones géographiques : le village ou la ville la plus proche du centre de chaque sortie ----
            let zonesSorties = { parSortie: {}, noms: {} };
            let zonesSignature = '';
            let zonesDernierEchec = 0;
            let zonesEnCours = false;

            function centreSortie(sortie) {
                const trace = Array.isArray(sortie.trace) ? sortie.trace : [];
                let sud = 90, nord = -90, ouest = 180, est = -180;
                trace.forEach(p => {
                    if (!Array.isArray(p) || !Number.isFinite(p[0]) || !Number.isFinite(p[1])) return;
                    sud = Math.min(sud, p[0]); nord = Math.max(nord, p[0]);
                    ouest = Math.min(ouest, p[1]); est = Math.max(est, p[1]);
                });
                return sud > nord ? null : [(sud + nord) / 2, (ouest + est) / 2];
            }

            function distanceKmGps(a, b) {
                const rad = Math.PI / 180;
                const dLat = (b[0] - a[0]) * rad, dLon = (b[1] - a[1]) * rad;
                const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLon / 2) ** 2;
                return 12742 * Math.asin(Math.sqrt(h));
            }

            // Nom de la commune au centre de chaque sortie (service Nominatim d'OpenStreetMap), gardé sur l'appareil.
            let zonesErreur = '';
            let zonesEtat = 'attente';

            function lireNomsZones() {
                try { return JSON.parse(localStorage.getItem('maillons_zones_noms') || '{}') || {}; } catch (e) { return {}; }
            }

            async function nomCommune(c) {
                const url = 'https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=10&addressdetails=1&accept-language=fr'
                    + '&lat=' + c[0].toFixed(5) + '&lon=' + c[1].toFixed(5);
                const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 12000);
                try {
                    const res = await fetch(url, { signal: ctl.signal });
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    const d = await res.json();
                    const a = (d && d.address) || {};
                    return a.village || a.town || a.city || a.municipality || a.hamlet || a.suburb || a.county || '';
                } finally {
                    clearTimeout(timer);
                }
            }

            function remplirListeZones() {
                const select = document.getElementById('zoneSorties');
                if (!select) return;
                const precedent = select.value;
                const compte = {};
                Object.values(zonesSorties.parSortie).forEach(z => { compte[z] = (compte[z] || 0) + 1; });
                const noms = Object.keys(compte).sort((a, b) => compte[b] - compte[a] || a.localeCompare(b, 'fr'));
                select.innerHTML = '';
                const toutes = document.createElement('option');
                toutes.value = '';
                toutes.textContent = noms.length ? 'Toutes les zones'
                    : zonesEtat === 'recherche' ? 'Recherche des zones…'
                    : zonesEtat === 'sans-trace' ? 'Toutes les zones (aucune sortie localisée)'
                    : zonesEtat === 'attente' ? 'Toutes les zones'
                    : 'Toutes les zones (liste indisponible' + (zonesErreur ? ' : ' + zonesErreur : '') + ')';
                select.appendChild(toutes);
                noms.forEach(nom => {
                    const o = document.createElement('option');
                    o.value = nom;
                    o.textContent = 'Côté de ' + nom + ' (' + compte[nom] + ')';
                    select.appendChild(o);
                });
                select.value = noms.includes(precedent) ? precedent : '';
                actualiserBoutonFiltreSorties();
            }

            async function calculerZonesSorties(sorties) {
                const signature = sorties.map(s => s.id).join(',');
                if (signature === zonesSignature || zonesEnCours || Date.now() - zonesDernierEchec < 120000) return;
                zonesEnCours = true;
                zonesEtat = 'recherche';
                remplirListeZones();
                try {
                    await calculerZonesSortiesInterne(sorties, signature);
                } finally {
                    zonesEnCours = false;
                }
            }

            async function calculerZonesSortiesInterne(sorties, signature) {
                zonesSignature = signature;
                const centres = sorties.map(s => ({ id: s.id, c: centreSortie(s) })).filter(x => x.c);
                if (!centres.length) { zonesEtat = 'sans-trace'; remplirListeZones(); return; }
                const noms = lireNomsZones();
                const afficher = () => {
                    const parSortie = {};
                    centres.forEach(x => { if (noms[x.id]) parSortie[x.id] = noms[x.id]; });
                    zonesSorties = { parSortie: parSortie, noms: {} };
                    if (Object.keys(parSortie).length) zonesEtat = 'ok';
                    remplirListeZones();
                };
                afficher();
                zonesErreur = '';
                let echecs = 0;
                for (const x of centres) {
                    if (noms[x.id]) continue;
                    try {
                        const nom = await nomCommune(x.c);
                        if (nom) {
                            noms[x.id] = nom;
                            try { localStorage.setItem('maillons_zones_noms', JSON.stringify(noms)); } catch (e) { /* stockage refusé */ }
                            afficher();
                        }
                        echecs = 0;
                    } catch (e) {
                        zonesErreur = String(e && e.name === 'AbortError' ? 'délai dépassé' : e);
                        console.warn('Zones : commune introuvable', e);
                        if (++echecs >= 3) break;
                    }
                    // Nominatim demande au plus une requête par seconde.
                    await new Promise(r => setTimeout(r, 1100));
                }
                if (!Object.keys(zonesSorties.parSortie).length) {
                    zonesEtat = 'echec';
                    if (!zonesErreur) zonesErreur = 'aucune commune trouvée';
                    remplirListeZones();
                }
                if (zonesErreur) {
                    zonesSignature = '';
                    zonesDernierEchec = Date.now() - 90000;
                }
            }

            function preparerFiltresSorties(sorties) {
                actualiserBoutonFiltreSorties();
                const uniteFiltre = document.getElementById('uniteFiltreDistance');
                if (uniteFiltre) uniteFiltre.textContent = unites.distance === 'mi' ? 'mi' : 'km';
                calculerZonesSorties(sorties).catch(e => console.warn(e));
            }

            // Jour (aaaa-mm-jj) d'une sortie, à l'heure locale.
            function jourLocalSortie(texte) {
                const d = new Date(texte);
                if (isNaN(d.getTime())) return '';
                const mois = String(d.getMonth() + 1).padStart(2, '0');
                const jour = String(d.getDate()).padStart(2, '0');
                return `${d.getFullYear()}-${mois}-${jour}`;
            }

            function ouvrirCalendrierSorties() {
                ouvrirCalendrierPeriode(
                    sortiesPeriode,
                    new Set(sortiesConnues.map(s => jourLocalSortie(s.debut))),
                    (periode) => {
                        sortiesPeriode = periode;
                        appliquerFiltresSorties();
                    }
                );
            }

            function filtrerSorties(sorties) {
                const recherche = (document.getElementById('rechercheSorties').value || '').trim().toLocaleLowerCase('fr');
                const resultat = sorties.filter(s => {
                    const texte = [s.nom, s.notes, s.sport, s.terrain].filter(Boolean).join(' ').toLocaleLowerCase('fr');
                    if (recherche && !texte.includes(recherche)) return false;
                    if (sortiesPeriode.debut || sortiesPeriode.fin) {
                        const jour = jourLocalSortie(s.debut);
                        if (sortiesPeriode.debut && jour < sortiesPeriode.debut) return false;
                        if (sortiesPeriode.fin && jour > sortiesPeriode.fin) return false;
                    }
                    const facteurFiltre = unites.distance === 'mi' ? 1 / 0.621371 : 1;
                    const minSaisi = kmSaisi('kmMinSorties'), maxSaisi = kmSaisi('kmMaxSorties');
                    const kmMin = minSaisi === null ? null : minSaisi * facteurFiltre;
                    const kmMax = maxSaisi === null ? null : maxSaisi * facteurFiltre;
                    const km = Number(s.distance_km || 0);
                    if (kmMin !== null && km < kmMin) return false;
                    if (kmMax !== null && km > kmMax) return false;
                    if (sortiesFavorisSeuls && !estFavori(s.id)) return false;
                    const zoneChoisie = (document.getElementById('zoneSorties') || {}).value;
                    if (zoneChoisie && zonesSorties.parSortie[s.id] !== zoneChoisie) return false;
                    return true;
                });
                const tri = document.getElementById('triSorties').value;
                resultat.sort((a, b) => {
                    if (tri === 'date-asc') return new Date(a.debut) - new Date(b.debut);
                    if (tri === 'distance-desc') return Number(b.distance_km || 0) - Number(a.distance_km || 0);
                    if (tri === 'distance-asc') return Number(a.distance_km || 0) - Number(b.distance_km || 0);
                    if (tri === 'denivele-desc') return Number(b.denivele_pos || 0) - Number(a.denivele_pos || 0);
                    if (tri === 'denivele-asc') return Number(a.denivele_pos || 0) - Number(b.denivele_pos || 0);
                    return new Date(b.debut) - new Date(a.debut);
                });
                return resultat;
            }

            function appliquerFiltresSorties() {
                actualiserBoutonFiltreSorties();
                if (sortiesConnues.length) chargerSorties(true);
            }

            function reinitialiserFiltresSorties() {
                document.getElementById('rechercheSorties').value = '';
                sortiesPeriode = { debut: '', fin: '' };
                document.getElementById('kmMinSorties').value = '';
                document.getElementById('kmMaxSorties').value = '';
                const zoneSel = document.getElementById('zoneSorties');
                if (zoneSel) zoneSel.value = '';
                sortiesFavorisSeuls = false;
                document.getElementById('triSorties').value = 'date-desc';
                actualiserBoutonFiltreSorties();
                chargerSorties(true);
            }

            async function chargerSorties(reutiliserCache = false) {
                const liste = document.getElementById('sortiesList');
                initialiserMenuFiltresSorties();

                try {
                    let sorties;
                    const depuisCache = reutiliserCache && sortiesConnues.length > 0;
                    if (depuisCache) {
                        sorties = sortiesConnues;
                    } else {
                        const res = await fetch('/api/sorties', { headers: getHeaders() });
                        if (!res.ok) throw new Error();
                        sorties = await res.json();
                        sortiesConnues = sorties;
                    }
                    preparerFiltresSorties(sorties);
                    const sortiesAffichees = filtrerSorties(sorties);
                    liste.innerHTML = '';
                    document.getElementById('compteurSorties').textContent =
                        sortiesAffichees.length + ' sortie' + (sortiesAffichees.length > 1 ? 's' : '') + ' affichée' + (sortiesAffichees.length > 1 ? 's' : '') + ' sur ' + sorties.length;

                    if (sorties.length === 0) {
                        const vide = document.createElement('p');
                        vide.style.color = 'var(--text-muted)';
                        vide.style.fontSize = '0.85rem';
                        vide.textContent = 'Aucune sortie importée.';
                        liste.appendChild(vide);
                        return;
                    }

                    if (sortiesAffichees.length === 0) {
                        const vide = document.createElement('p');
                        vide.style.color = 'var(--text-muted)';
                        vide.style.fontSize = '0.85rem';
                        vide.textContent = 'Aucune sortie ne correspond à ces critères.';
                        liste.appendChild(vide);
                    }

                    sortiesAffichees.forEach(s => {
                        const item = document.createElement('div');
                        item.className = 'history-item';

                        const details = document.createElement('div');
                        details.className = 'history-details';

                        const titre = document.createElement('strong');
                        const jour = new Date(s.debut).toLocaleDateString('fr-FR');
                        titre.textContent = `${s.nom} · ${jour}`;
                        titre.className = 'titre-sortie';
                        titre.style.display = 'block';

                        // Pastille rouge « Nouveau » tant que la sortie n'a pas été touchée.
                        if (sortieEstNouvelle(s.id)) {
                            const nouveau = document.createElement('span');
                            nouveau.className = 'pastille-nouvelle';
                            nouveau.textContent = 'Nouveau';
                            titre.prepend(nouveau);
                            item.addEventListener('click', () => {
                                effacerNouvelleSortie(s.id);
                                nouveau.remove();
                            });
                        }

                        // Pastille rouge « À vérifier » : sortie reçue automatiquement,
                        // rangée sur le dernier vélo utilisé, à confirmer ou à changer.
                        if (Number(s.a_verifier) === 1) {
                            const aVerifier = document.createElement('span');
                            aVerifier.className = 'pastille-verifier';
                            aVerifier.textContent = 'À vérifier';
                            titre.prepend(aVerifier);
                        }

                        const ligne1 = document.createElement('div');
                        ligne1.style.color = 'var(--text-muted)';
                        ligne1.style.marginTop = '4px';
                        ligne1.textContent =
                            `${fmtDistance(s.distance_km).texte} ${fmtDistance(s.distance_km).unite} · ${fmtAltitude(s.denivele_pos).texte} ${fmtAltitude(s.denivele_pos).unite} D+ · ` +
                            `${formaterDuree(s.duree_s)} · ${fmtVitesse(s.vitesse_moy).texte} ${fmtVitesse(s.vitesse_moy).unite}`;

                        const ligne2 = document.createElement('div');
                        ligne2.style.color = 'var(--text-muted)';
                        ligne2.style.display = 'flex';
                        ligne2.style.flexWrap = 'wrap';
                        ligne2.style.alignItems = 'center';
                        ligne2.style.gap = '6px';
                        ligne2.style.marginTop = '4px';

                        const menuTerrain = document.createElement('select');
                        menuTerrain.setAttribute('aria-label', 'Terrain de la sortie');
                        menuTerrain.style.cssText =
                            'width:auto;flex:0 0 auto;margin:0;padding:3px 6px;font-size:0.8rem;';

                        const cles = Object.keys(LIBELLES_TERRAIN);

                        if (!cles.includes(s.terrain)) {
                            cles.push(s.terrain);
                        }

                        cles.forEach(cle => {
                            const choix = document.createElement('option');
                            choix.value = cle;
                            choix.textContent = LIBELLES_TERRAIN[cle] || cle;
                            menuTerrain.appendChild(choix);
                        });

                        menuTerrain.value = s.terrain;
                        menuTerrain.addEventListener('change', () => {
                            changerTerrain(s.id, menuTerrain.value, menuTerrain, s.terrain);
                        });

                        const repart = s.coef_analyse ? repartitionTerrain(s) : null;
                        if (repart) {
                            const texteTerrain = document.createElement('span');
                            texteTerrain.style.fontWeight = '600';
                            texteTerrain.textContent = `Terrain analysé : ${repart.texte}`;
                            ligne2.appendChild(texteTerrain);
                        }

                        // Le choix du terrain ne sert plus que sans analyse,
                        // ou si une part importante du parcours reste inconnue.
                        if (!repart || repart.inconnu >= 0.2) {
                            ligne2.appendChild(menuTerrain);
                        }

                        if (s.notes) {
                            const note = document.createElement('span');
                            note.textContent = `· ${s.notes}`;
                            ligne2.appendChild(note);
                        }

                        const ligneUsure = document.createElement('div');
                        ligneUsure.className = 'ligne-usure';
                        if (s.coef_analyse) {
                            ligneUsure.textContent =
                                `Usure des pièces ×${String(s.coef_usure).replace('.', ',')} · d'après le terrain analysé`;
                        } else {
                            ligneUsure.textContent =
                                `Usure des pièces ×${String(s.coef_usure).replace('.', ',')} · terrain choisi`;
                        }

                        const bouton = document.createElement('button');
                        bouton.className = 'btn-action';
                        bouton.style.marginTop = '8px';
                        bouton.style.width = 'auto';
                        bouton.style.padding = '6px 12px';
                        bouton.textContent = '🗑️ Supprimer';
                        bouton.addEventListener('click', () => {
                            supprimerSortie(s.id);
                        });

                        const nbPhotos = s.photos ? s.photos.length : 0;
                        const boutonPhotos = document.createElement('button');
                        boutonPhotos.className = 'btn-action';
                        boutonPhotos.style.marginTop = '8px';
                        boutonPhotos.style.marginRight = '8px';
                        boutonPhotos.style.width = 'auto';
                        boutonPhotos.style.padding = '6px 12px';
                        boutonPhotos.textContent = '📷 Ajouter des photos';
                        boutonPhotos.disabled = nbPhotos >= MAX_PHOTOS;

                        const choixPhotos = document.createElement('input');
                        choixPhotos.type = 'file';
                        choixPhotos.accept = 'image/*';
                        choixPhotos.multiple = true;
                        choixPhotos.style.display = 'none';
                        const statut = document.createElement('div');
                        statut.className = 'sortie-statut';
                        statut.style.display = 'none';
                        const etatPhotos = {
                            disabled: false,
                            get textContent() { return ''; },
                            set textContent(v) {
                                statut.textContent = v;
                                statut.style.display = v ? '' : 'none';
                            }
                        };
                        choixPhotos.addEventListener('change', () => {
                            ajouterPhotos(s.id, nbPhotos, choixPhotos, etatPhotos);
                        });
                        boutonPhotos.addEventListener('click', () => {
                            choixPhotos.click();
                        });

                        details.appendChild(titre);
                        details.appendChild(ligne1);
                        details.appendChild(ligne2);
                        details.appendChild(ligneUsure);
                        const nbVideos = s.videos ? s.videos.length : 0;
                        const boutonVideo = document.createElement('button');
                        boutonVideo.className = 'btn-action';
                        boutonVideo.style.marginTop = '8px';
                        boutonVideo.style.marginRight = '8px';
                        boutonVideo.style.width = 'auto';
                        boutonVideo.style.padding = '6px 12px';
                        boutonVideo.textContent = '🎬 Ajouter une vidéo';
                        boutonVideo.disabled = nbVideos >= MAX_VIDEOS;
                        boutonVideo.addEventListener('click', () => {
                            ajouterVideo(s.id);
                        });

                        const bouton3d = document.createElement('button');
                        bouton3d.className = 'btn-survol';
                        bouton3d.type = 'button';
                        remplirBouton(bouton3d, 'montagne', 'Survol 3D');
                        bouton3d.disabled = !s.trace || s.trace.length < 2;
                        bouton3d.addEventListener('click', () => {
                            ouvrirV3d(s);
                        });

                        details.appendChild(bouton3d);
                        details.appendChild(choixPhotos);
                        details.appendChild(statut);

                        // Menu « trois points » en haut à droite de la sortie.
                        const menu = document.createElement('div');
                        menu.className = 'menu-sortie';
                        menu.setAttribute('role', 'menu');

                        const kebab = document.createElement('button');
                        kebab.type = 'button';
                        kebab.className = 'menu-sortie-btn';
                        kebab.setAttribute('aria-label', 'Actions de la sortie');
                        kebab.setAttribute('aria-haspopup', 'menu');
                        kebab.setAttribute('aria-expanded', 'false');
                        remplirBouton(kebab, 'points', '');

                        const ajouterAction = (icone, texte, action, danger, desactive) => {
                            const el = document.createElement('button');
                            el.type = 'button';
                            el.setAttribute('role', 'menuitem');
                            if (danger) el.className = 'danger';
                            el.disabled = !!desactive;
                            remplirBouton(el, icone, texte);
                            el.addEventListener('click', (e) => {
                                e.stopPropagation();
                                fermerMenusSortie();
                                action();
                            });
                            menu.appendChild(el);
                        };

                        ajouterAction('appareil', 'Ajouter des photos', () => choixPhotos.click(), false, nbPhotos >= MAX_PHOTOS);
                        ajouterAction('video', 'Ajouter une vidéo', () => ajouterVideo(s.id), false, nbVideos >= MAX_VIDEOS);
                        ajouterAction('crayon', 'Modifier les notes', () => modifierNotesSortie(s), false, false);
                        ajouterAction('telecharger', 'Exporter en GPX', () => exporterSortieGpx(s), false, !Array.isArray(s.trace) || s.trace.length < 2);
                        ajouterAction('crayon', 'Renommer', () => renommerSortie(s), false, false);
                        ajouterAction('valider', 'Confirmer le vélo', () => confirmerVeloSortie(s), false, Number(s.a_verifier) !== 1);
                        ajouterAction('repere', 'Changer de vélo', () => changerVeloSortie(s), false, velosDisponibles.length < 2);
                        ajouterAction('poubelle', 'Supprimer', () => supprimerSortie(s.id), true, false);

                        kebab.addEventListener('click', (e) => {
                            e.stopPropagation();
                            const etaitOuvert = menu.classList.contains('ouvert');
                            fermerMenusSortie();
                            if (!etaitOuvert) {
                                menu.classList.add('ouvert');
                                placerMenuSortie(menu);
                                kebab.setAttribute('aria-expanded', 'true');
                            }
                        });

                        item.classList.add('sortie-item');

                        const boutonPartage = document.createElement('button');
                        boutonPartage.type = 'button';
                        boutonPartage.className = 'menu-sortie-btn bouton-partage';
                        boutonPartage.setAttribute('aria-label', 'Partager la sortie');
                        remplirBouton(boutonPartage, 'partage', '');
                        boutonPartage.addEventListener('click', (e) => {
                            e.stopPropagation();
                            fermerMenusSortie();
                            partagerSortie(s, boutonPartage);
                        });

                        const boutonFavori = document.createElement('button');
                        boutonFavori.type = 'button';
                        boutonFavori.className = 'menu-sortie-btn bouton-favori';
                        const majFavori = () => {
                            const actif = estFavori(s.id);
                            boutonFavori.classList.toggle('actif', actif);
                            boutonFavori.setAttribute('aria-pressed', actif ? 'true' : 'false');
                            boutonFavori.setAttribute('aria-label', actif ? 'Retirer des favoris' : 'Ajouter aux favoris');
                        };
                        remplirBouton(boutonFavori, 'coeur', '');
                        majFavori();
                        boutonFavori.addEventListener('click', (e) => {
                            e.stopPropagation();
                            fermerMenusSortie();
                            basculerFavori(s.id);
                            majFavori();
                            if (sortiesFavorisSeuls) chargerSorties(true);
                        });

                        item.appendChild(boutonFavori);
                        item.appendChild(boutonPartage);
                        item.appendChild(kebab);
                        item.appendChild(menu);

                        item.appendChild(details);

                        const mediaDiv = document.createElement('div');
                        mediaDiv.className = 'sortie-media';
                        mediaDiv.style.marginTop = '34px';

                        if (s.photos && s.photos.length > 0) {
                            const gallery = document.createElement('div');
                            gallery.className = 'photos-gallery';
                            s.photos.forEach((pData, numero) => {
                                const img = document.createElement('img');
                                img.src = pData;
                                img.className = 'sortie-img';
                                img.style.cursor = 'pointer';
                                img.addEventListener('click', () => {
                                    ouvrirPhoto(s.id, numero, s.photos);
                                });
                                gallery.appendChild(img);
                            });
                            mediaDiv.appendChild(gallery);
                        }

                        if (s.videos && s.videos.length > 0) {
                            const zoneVideos = document.createElement('div');
                            zoneVideos.className = 'videos-liste';
                            s.videos.forEach((v, numero) => {
                                zoneVideos.appendChild(carteVideo(s.id, v, numero));
                            });
                            item.style.flexWrap = 'wrap';
                            item.appendChild(zoneVideos);
                        }

                        mediaDiv.appendChild(dessinerTrace(s.trace));
                        item.insertBefore(mediaDiv, item.querySelector('.videos-liste'));

                        const blocProfil = dessinerProfil(s);
                        if (blocProfil) {
                            item.style.flexWrap = 'wrap';
                            item.appendChild(blocProfil);
                        }

                        // On touche la sortie (hors boutons, photos, menus) pour ouvrir sa fiche.
                        item.classList.add('sortie-cliquable');
                        item.addEventListener('click', (e) => {
                            if (e.target.closest('button, select, input, a, label, img, video, .profil-sortie, .videos-liste, .menu-sortie, .photos-gallery')) return;
                            ouvrirFicheSortie(s);
                        });

                        liste.appendChild(item);
                    });

                    if (!depuisCache) analyserAuto(sorties);

                } catch (e) {
                    console.error(e);
                }
            }


            const MAX_PHOTOS = 8;
            const MAX_VIDEOS = 10;

            // ---------- Fiche d'une sortie : on touche une sortie pour l'ouvrir ----------
            let ficheUI = null;
            let ficheCarte = null;
            let ficheJeton = 0;

            const METEO_CODES = [
                [[0], '☀️', 'Ciel dégagé'],
                [[1], '🌤️', 'Plutôt dégagé'],
                [[2], '⛅', 'Partiellement nuageux'],
                [[3], '☁️', 'Couvert'],
                [[45, 48], '🌫️', 'Brouillard'],
                [[51, 53, 55, 56, 57], '🌦️', 'Bruine'],
                [[61, 63, 65, 66, 67], '🌧️', 'Pluie'],
                [[71, 73, 75, 77, 85, 86], '🌨️', 'Neige'],
                [[80, 81, 82], '🌦️', 'Averses'],
                [[95, 96, 99], '⛈️', 'Orage']
            ];

            function fmtFr(n, decimales = 0) {
                return Number(n).toLocaleString('fr-FR', {
                    minimumFractionDigits: decimales,
                    maximumFractionDigits: decimales
                });
            }

            // Vitesse maximale tenue au moins 10 secondes (évite les sauts isolés du GPS).
            function vitesseMaxSortie(trace) {
                if (!Array.isArray(trace) || trace.length < 3) return null;
                if (trace.some(p => !Array.isArray(p) || typeof p[2] !== 'number')) return null;
                let max = 0;
                let j = 1;
                for (let i = 0; i < trace.length - 1; i++) {
                    if (j <= i) j = i + 1;
                    while (j < trace.length && trace[j][2] - trace[i][2] < 10) j++;
                    if (j >= trace.length) break;
                    const dt = trace[j][2] - trace[i][2];
                    const v = (distanceLatLon(trace[i], trace[j]) / dt) * 3.6;
                    if (v <= 100 && v > max) max = v;
                }
                return max > 0 ? max : null;
            }

            function cumulTrace(trace) {
                const cum = [0];
                for (let i = 1; i < trace.length; i++) {
                    cum.push(cum[i - 1] + distanceLatLon(trace[i - 1], trace[i]));
                }
                return cum;
            }

            // Météo de la sortie (service gratuit Open-Meteo : fonctionne seulement en ligne).
            async function chercherMeteoSortie(s) {
                const pts = Array.isArray(s.trace) ? s.trace.filter(p => Array.isArray(p) && p.length >= 2) : [];
                const debut = new Date(s.debut);
                if (pts.length === 0 || isNaN(debut.getTime())) throw new Error('donnees');

                const duree = Number(s.duree_s) > 0 ? Number(s.duree_s) : 3600;
                const fin = new Date(debut.getTime() + duree * 1000);
                const milieu = pts[Math.floor(pts.length / 2)];
                const jourUtc = (d) => d.toISOString().slice(0, 10);
                const params = 'latitude=' + Number(milieu[0]).toFixed(3)
                    + '&longitude=' + Number(milieu[1]).toFixed(3)
                    + '&start_date=' + jourUtc(debut)
                    + '&end_date=' + jourUtc(fin)
                    + '&hourly=temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,weather_code'
                    + '&timezone=UTC';
                const recente = (Date.now() - debut.getTime()) < 85 * 86400000;
                const prevision = 'https://api.open-meteo.com/v1/forecast?' + params;
                const archive = 'https://archive-api.open-meteo.com/v1/archive?' + params;
                const adresses = recente ? [prevision, archive] : [archive, prevision];

                let h = null;
                for (const adresse of adresses) {
                    try {
                        const res = await fetch(adresse);
                        if (!res.ok) continue;
                        const donnees = await res.json();
                        if (donnees && donnees.hourly && Array.isArray(donnees.hourly.time)) {
                            h = donnees.hourly;
                            break;
                        }
                    } catch (e) {
                        console.error(e);
                    }
                }
                if (!h) throw new Error('meteo');

                const indices = [];
                h.time.forEach((t, i) => {
                    const debutHeure = Date.parse(t + 'Z');
                    if (debutHeure + 3600000 > debut.getTime() && debutHeure < fin.getTime()) indices.push(i);
                });
                if (indices.length === 0) throw new Error('heures');

                const valeurs = (cle) => (h[cle] || [])
                    ? indices.map(i => (h[cle] || [])[i]).filter(v => typeof v === 'number')
                    : [];
                const moy = (l) => (l.length ? l.reduce((a, b) => a + b, 0) / l.length : null);
                const temps = valeurs('temperature_2m');
                const pluie = valeurs('precipitation');
                const vent = valeurs('wind_speed_10m');
                const rafales = valeurs('wind_gusts_10m');
                const codes = valeurs('weather_code');

                const compte = {};
                codes.forEach(c => { compte[c] = (compte[c] || 0) + 1; });
                const frequent = Object.keys(compte).sort((a, b) => compte[b] - compte[a])[0];

                return {
                    temp: moy(temps),
                    tempMin: temps.length ? Math.min(...temps) : null,
                    tempMax: temps.length ? Math.max(...temps) : null,
                    pluie: pluie.length ? pluie.reduce((a, b) => a + b, 0) : null,
                    vent: moy(vent),
                    rafales: rafales.length ? Math.max(...rafales) : null,
                    code: frequent === undefined ? null : Number(frequent)
                };
            }

            function styleCarteFiche() {
                return {
                    version: 8,
                    sources: {
                        sat: {
                            type: 'raster',
                            tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
                            tileSize: 256,
                            maxzoom: 19,
                            attribution: 'Imagerie © Esri, Maxar, Earthstar Geographics'
                        },
                        plan: {
                            type: 'raster',
                            tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
                            tileSize: 256,
                            maxzoom: 19,
                            attribution: '© contributeurs OpenStreetMap'
                        }
                    },
                    layers: [
                        { id: 'fond-sat', type: 'raster', source: 'sat' },
                        { id: 'fond-plan', type: 'raster', source: 'plan', layout: { visibility: 'none' } }
                    ]
                };
            }

            function barreRepartition(parts) {
                const total = parts.reduce((t, p) => t + p.km, 0);
                const zone = document.createElement('div');
                if (total <= 0) return zone;

                const barre = document.createElement('div');
                barre.className = 'fiche-barre';
                parts.forEach(p => {
                    const seg = document.createElement('div');
                    seg.style.width = ((p.km / total) * 100) + '%';
                    seg.style.background = p.couleur;
                    barre.appendChild(seg);
                });
                zone.appendChild(barre);

                const legende = document.createElement('div');
                legende.className = 'fiche-legende';
                parts.forEach(p => {
                    const ligne = document.createElement('span');
                    const pastille = document.createElement('i');
                    pastille.style.background = p.couleur;
                    ligne.appendChild(pastille);
                    ligne.appendChild(document.createTextNode(
                        `${p.libelle} · ${fmtDistance(p.km, 1).complet} (${Math.round((p.km / total) * 100)} %)`
                    ));
                    legende.appendChild(ligne);
                });
                zone.appendChild(legende);
                return zone;
            }

            function creerFicheUI() {
                const fenetre = document.createElement('div');
                fenetre.className = 'fenetre-profil';
                fenetre.style.display = 'none';
                fenetre.setAttribute('role', 'dialog');
                fenetre.setAttribute('aria-modal', 'true');
                fenetre.setAttribute('aria-label', 'Fiche de la sortie');

                const boite = document.createElement('div');
                boite.className = 'fp-boite fiche-boite';
                fenetre.appendChild(boite);

                const fermer = () => {
                    ficheJeton++;
                    if (ficheCarte) {
                        try { ficheCarte.remove(); } catch (e) { console.error(e); }
                        ficheCarte = null;
                    }
                    fenetre.style.display = 'none';
                };

                fenetre.addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (e.target === fenetre) fermer();
                });
                document.addEventListener('keydown', (e) => {
                    if (e.key === 'Escape' && fenetre.style.display !== 'none') fermer();
                });
                document.body.appendChild(fenetre);

                const bloc = (titre) => {
                    const b = document.createElement('section');
                    b.className = 'fiche-bloc';
                    const h = document.createElement('h3');
                    h.textContent = titre;
                    b.appendChild(h);
                    boite.appendChild(b);
                    return b;
                };

                const tuile = (parent, libelle, valeur) => {
                    const t = document.createElement('div');
                    t.className = 'fiche-tuile';
                    const l = document.createElement('span');
                    l.textContent = libelle;
                    const v = document.createElement('strong');
                    v.textContent = valeur;
                    t.appendChild(l);
                    t.appendChild(v);
                    parent.appendChild(t);
                    return v;
                };

                const afficher = (s) => {
                    const jeton = ++ficheJeton;
                    if (ficheCarte) {
                        try { ficheCarte.remove(); } catch (e) { console.error(e); }
                        ficheCarte = null;
                    }
                    boite.textContent = '';

                    const trace = Array.isArray(s.trace) ? s.trace : [];
                    const profil = analyserProfil(trace);
                    const debut = new Date(s.debut);
                    const dateTexte = isNaN(debut.getTime())
                        ? ''
                        : debut.toLocaleDateString('fr-FR', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
                            + ' · départ ' + formaterHeure(debut);

                    // En-tête
                    const haut = document.createElement('div');
                    haut.className = 'fp-haut';
                    const titres = document.createElement('div');
                    const titre = document.createElement('strong');
                    titre.className = 'fiche-titre';
                    titre.textContent = s.nom || 'Sortie';
                    const sous = document.createElement('div');
                    sous.className = 'form-help';
                    sous.style.margin = '2px 0 0';
                    sous.textContent = dateTexte;
                    titres.appendChild(titre);
                    titres.appendChild(sous);
                    const boutonFermer = document.createElement('button');
                    boutonFermer.type = 'button';
                    boutonFermer.className = 'cal-rond';
                    boutonFermer.setAttribute('aria-label', 'Fermer');
                    remplirBouton(boutonFermer, 'fermer', '');
                    boutonFermer.addEventListener('click', fermer);
                    haut.appendChild(titres);
                    haut.appendChild(boutonFermer);
                    boite.appendChild(haut);

                    // Carte vue de dessus
                    const blocCarte = document.createElement('div');
                    blocCarte.className = 'fiche-carte';
                    boite.appendChild(blocCarte);

                    // Chiffres clés
                    const blocStats = bloc('En chiffres');
                    const grille = document.createElement('div');
                    grille.className = 'fiche-grille';
                    blocStats.appendChild(grille);
                    const altitudes = trace.map(p => (Array.isArray(p) ? p[3] : null)).filter(a => typeof a === 'number');
                    const vmax = vitesseMaxSortie(trace);
                    tuile(grille, 'Distance', fmtDistance(s.distance_km, 1).complet);
                    tuile(grille, 'Durée', formaterDuree(s.duree_s));
                    tuile(grille, 'Vitesse moyenne', fmtVitesse(s.vitesse_moy, 1).complet);
                    tuile(grille, 'Vitesse maximum', vmax === null ? '—' : fmtVitesse(vmax, 1).complet);
                    tuile(grille, 'Dénivelé positif', fmtAltitude(s.denivele_pos).complet);
                    tuile(grille, 'Dénivelé négatif', fmtAltitude(s.denivele_neg).complet);
                    if (altitudes.length) {
                        tuile(grille, 'Altitude maximum', fmtAltitude(Math.max(...altitudes)).complet);
                        tuile(grille, 'Altitude minimum', fmtAltitude(Math.min(...altitudes)).complet);
                    }
                    if (profil) {
                        tuile(grille, 'Pente max en montée', `${Math.round(Math.max(...profil.pentes))} %`);
                        tuile(grille, 'Pente max en descente', `${Math.round(Math.abs(Math.min(...profil.pentes)))} %`);
                    }
                    if (s.coef_usure) {
                        tuile(grille, 'Usure des pièces', `×${fmtFr(s.coef_usure, 2)}`);
                    }

                    // Météo (remplie quand le service répond)
                    const blocMeteo = bloc('Météo pendant la sortie');
                    const texteMeteo = document.createElement('p');
                    texteMeteo.className = 'form-help';
                    texteMeteo.style.margin = '0';
                    texteMeteo.textContent = 'Recherche de la météo…';
                    blocMeteo.appendChild(texteMeteo);
                    chercherMeteoSortie(s).then(m => {
                        if (jeton !== ficheJeton) return;
                        const cond = METEO_CODES.find(x => x[0].includes(m.code));
                        texteMeteo.className = 'fiche-meteo-titre';
                        texteMeteo.textContent = cond ? `${cond[1]} ${cond[2]}` : '🌡️ Conditions variables';
                        const g = document.createElement('div');
                        g.className = 'fiche-grille';
                        blocMeteo.appendChild(g);
                        if (m.temp !== null) {
                            tuile(g, 'Température', fmtTemperature(m.temp).complet);
                            tuile(g, 'Min · max', fmtTemperature(m.tempMin).texte + ' à ' + fmtTemperature(m.tempMax).complet);
                        }
                        if (m.pluie !== null) tuile(g, 'Pluie', `${fmtFr(m.pluie, 1)} mm`);
                        if (m.vent !== null) tuile(g, 'Vent moyen', fmtVitesse(m.vent, 0).complet);
                        if (m.rafales !== null) tuile(g, 'Rafales max', fmtVitesse(m.rafales, 0).complet);
                    }).catch(() => {
                        if (jeton !== ficheJeton) return;
                        texteMeteo.textContent = 'Météo indisponible (il faut une connexion internet).';
                    });

                    // Profil d'altitude
                    const blocProfil = bloc("Profil d'altitude");
                    if (profil) {
                        fenetre.style.display = 'flex';
                        const largeur = Math.max(280, Math.min(520, (boite.clientWidth || 340) - 28));
                        const g = construireProfil(s, profil, largeur, 230, true);
                        blocProfil.appendChild(g.svg);
                        blocProfil.appendChild(legendesProfil(s, profil));
                    } else {
                        const p = document.createElement('p');
                        p.className = 'form-help';
                        p.textContent = "Pas assez d'altitudes dans cette sortie pour dessiner le profil.";
                        blocProfil.appendChild(p);
                    }

                    // Pentes
                    if (profil) {
                        const blocPentes = bloc('Pentes');
                        const km = COULEURS_PENTE.map(() => 0);
                        for (let i = 0; i < profil.pentes.length; i++) {
                            const d = profil.cum[i + 1] - profil.cum[i];
                            const k = COULEURS_PENTE.findIndex(c => Math.abs(profil.pentes[i]) < c.max);
                            km[k < 0 ? km.length - 1 : k] += d / 1000;
                        }
                        blocPentes.appendChild(barreRepartition(
                            COULEURS_PENTE.map((c, i) => ({ couleur: c.couleur, libelle: c.nom, km: km[i] })).filter(p => p.km > 0)
                        ));
                    }

                    // Terrain
                    const blocTerrain = bloc('Terrain');
                    const codes = s.surfaces && s.surfaces.length === trace.length ? s.surfaces : null;
                    if (codes && trace.length > 1) {
                        const totaux = {};
                        for (let i = 0; i < trace.length - 1; i++) {
                            const cle = codes[i] in TYPES_CHEMIN ? codes[i] : 'u';
                            totaux[cle] = (totaux[cle] || 0) + distanceLatLon(trace[i], trace[i + 1]) / 1000;
                        }
                        blocTerrain.appendChild(barreRepartition(
                            Object.keys(TYPES_CHEMIN)
                                .filter(k => totaux[k] > 0)
                                .map(k => ({ couleur: TYPES_CHEMIN[k].couleur, libelle: TYPES_CHEMIN[k].nom, km: totaux[k] }))
                        ));
                    } else {
                        const p = document.createElement('p');
                        p.className = 'form-help';
                        p.style.margin = '0';
                        p.textContent = `Terrain choisi : ${LIBELLES_TERRAIN[s.terrain] || s.terrain || 'mixte'} (analyse du chemin pas encore disponible).`;
                        blocTerrain.appendChild(p);
                    }

                    if (s.notes) {
                        const blocNotes = bloc('Notes');
                        const p = document.createElement('p');
                        p.style.margin = '0';
                        p.textContent = s.notes;
                        blocNotes.appendChild(p);
                    }

                    // Bouton vers le survol 3D
                    const bouton3d = document.createElement('button');
                    bouton3d.type = 'button';
                    bouton3d.className = 'btn-survol';
                    remplirBouton(bouton3d, 'montagne', 'Survol 3D');
                    bouton3d.disabled = trace.length < 2;
                    bouton3d.addEventListener('click', () => {
                        fermer();
                        ouvrirV3d(s);
                    });
                    boite.appendChild(bouton3d);

                    fenetre.style.display = 'flex';
                    boite.scrollTop = 0;

                    // Carte : MapLibre si possible, sinon simple tracé dessiné.
                    const secours = () => {
                        blocCarte.textContent = '';
                        blocCarte.appendChild(dessinerTrace(trace));
                    };
                    const pts = trace.filter(p => Array.isArray(p) && p.length >= 2).map(p => [p[1], p[0]]);
                    if (pts.length < 2) {
                        blocCarte.style.display = 'none';
                        return;
                    }
                    chargerMapLibre().then(() => {
                        if (jeton !== ficheJeton) return;
                        const ml = window.maplibregl;
                        const carte = new ml.Map({
                            container: blocCarte,
                            style: styleCarteFiche(),
                            center: pts[0],
                            zoom: 11,
                            cooperativeGestures: true,
                            attributionControl: false
                        });
                        // Mention des sources : repliée derrière le « i », qui l'ouvre au toucher.
                        carte.addControl(new ml.AttributionControl({ compact: true }));
                        const replierMention = () => {
                            const el = carte.getContainer().querySelector('.maplibregl-ctrl-attrib');
                            if (!el) return;
                            el.classList.remove('maplibregl-compact-show');
                            el.removeAttribute('open');
                        };
                        replierMention();
                        carte.once('load', replierMention);
                        carte.once('idle', replierMention);
                        setTimeout(replierMention, 500);
                        ficheCarte = carte;
                        carte.on('load', () => {
                            if (jeton !== ficheJeton) return;
                            const features = [];
                            for (let i = 0; i < pts.length - 1; i++) {
                                const pente = profil && profil.pentes.length === pts.length - 1 ? profil.pentes[i] : null;
                                features.push({
                                    type: 'Feature',
                                    properties: { c: pente === null ? '#35e0c8' : couleurPente(pente) },
                                    geometry: { type: 'LineString', coordinates: [pts[i], pts[i + 1]] }
                                });
                            }
                            carte.addSource('trace', { type: 'geojson', data: { type: 'FeatureCollection', features: features } });
                            carte.addLayer({
                                id: 'trace-bord', type: 'line', source: 'trace',
                                layout: { 'line-cap': 'round', 'line-join': 'round' },
                                paint: { 'line-color': '#ffffff', 'line-width': 7, 'line-opacity': 0.9 }
                            });
                            carte.addLayer({
                                id: 'trace-ligne', type: 'line', source: 'trace',
                                layout: { 'line-cap': 'round', 'line-join': 'round' },
                                paint: { 'line-color': ['get', 'c'], 'line-width': 4 }
                            });
                            new ml.Marker({ color: '#22c55e' }).setLngLat(pts[0]).addTo(carte);
                            new ml.Marker({ color: '#ef4444' }).setLngLat(pts[pts.length - 1]).addTo(carte);
                            const bornes = new ml.LngLatBounds(pts[0], pts[0]);
                            pts.forEach(p => bornes.extend(p));
                            carte.fitBounds(bornes, { padding: 36, animate: false });
                        });

                        const fond = document.createElement('button');
                        fond.type = 'button';
                        fond.className = 'fiche-fond';
                        fond.textContent = 'Plan';
                        let satellite = true;
                        fond.addEventListener('click', () => {
                            satellite = !satellite;
                            carte.setLayoutProperty('fond-sat', 'visibility', satellite ? 'visible' : 'none');
                            carte.setLayoutProperty('fond-plan', 'visibility', satellite ? 'none' : 'visible');
                            fond.textContent = satellite ? 'Plan' : 'Satellite';
                        });
                        blocCarte.appendChild(fond);
                    }).catch(() => {
                        if (jeton === ficheJeton) secours();
                    });
                };

                return { afficher: afficher, fermer: fermer };
            }

            function ouvrirFicheSortie(s) {
                if (!ficheUI) ficheUI = creerFicheUI();
                ficheUI.afficher(s);
            }

            // ---------- Survol 3D ----------
            const MAPLIBRE_JS = [
                'https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js',
                'https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js'
            ];
            const MAPLIBRE_CSS = 'https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css';
            let jetonV3d = 0;
            const INCLINAISONS = [45, 60, 75];
            const VITESSES = [1, 2, 4];

            const v3d = {
                carte: null, pts: [], cum: [], total: 0, f: 0, jouer: false,
                vitesse: 1, inclinaison: 60, cap: 0, dernier: 0, raf: 0,
                fond: 'sat', enregistreur: null, morceaux: [], titre: '',
                dureeBase: 60, prete: false, compteImages: 0,
                tps: [], decalage: 0, alts: [], surfaces: '', pentes: [], mode: 'pente', autres: [], voirAutres: false, photos: [], marqueurs: [], reperes: [], panneauxPente: [], repereVisibles: true, prochaine: 0, sortieId: 0, minuteur: 0
            };

            function chargerScript(url) {
                return new Promise((resolve, reject) => {
                    const sc = document.createElement('script');
                    sc.src = url;
                    sc.async = true;
                    sc.onload = () => resolve();
                    sc.onerror = () => { sc.remove(); reject(new Error('script')); };
                    document.head.appendChild(sc);
                });
            }

            async function chargerMapLibre() {
                if (window.maplibregl) return;

                if (!document.getElementById('maplibreCss')) {
                    const l = document.createElement('link');
                    l.id = 'maplibreCss';
                    l.rel = 'stylesheet';
                    l.href = MAPLIBRE_CSS;
                    document.head.appendChild(l);
                }

                for (const url of MAPLIBRE_JS) {
                    try {
                        await chargerScript(url);
                        if (window.maplibregl) return;
                    } catch (e) {
                        console.error(e);
                    }
                }
                throw new Error('maplibre');
            }

            function distanceM(a, b) {
                const R = 6371000;
                const rad = Math.PI / 180;
                const dLat = (b[1] - a[1]) * rad;
                const dLon = (b[0] - a[0]) * rad;
                const h = Math.sin(dLat / 2) ** 2 +
                    Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(dLon / 2) ** 2;
                return 2 * R * Math.asin(Math.sqrt(h));
            }

            function capEntre(a, b) {
                const rad = Math.PI / 180;
                const y = Math.sin((b[0] - a[0]) * rad) * Math.cos(b[1] * rad);
                const x = Math.cos(a[1] * rad) * Math.sin(b[1] * rad) -
                    Math.sin(a[1] * rad) * Math.cos(b[1] * rad) * Math.cos((b[0] - a[0]) * rad);
                return (Math.atan2(y, x) / rad + 360) % 360;
            }

            function ecartAngle(cible, actuel) {
                return ((cible - actuel + 540) % 360) - 180;
            }

            // Point du parcours à la distance d (en mètres) depuis le départ.
            function pointA(d) {
                const pts = v3d.pts;
                const cum = v3d.cum;
                if (d <= 0) return pts[0].slice();
                if (d >= v3d.total) return pts[pts.length - 1].slice();
                let bas = 0;
                let haut = cum.length - 1;
                while (haut - bas > 1) {
                    const milieu = (bas + haut) >> 1;
                    if (cum[milieu] <= d) bas = milieu; else haut = milieu;
                }
                const seg = cum[haut] - cum[bas];
                const t = seg > 0 ? (d - cum[bas]) / seg : 0;
                return [
                    pts[bas][0] + (pts[haut][0] - pts[bas][0]) * t,
                    pts[bas][1] + (pts[haut][1] - pts[bas][1]) * t
                ];
            }

            function preparerParcours(trace) {
                // La trace est enregistrée en [latitude, longitude] ; la carte veut [longitude, latitude].
                const valides = trace.filter(p => Array.isArray(p) && p.length >= 2);
                const pts = valides.map(p => [p[1], p[0]]);
                v3d.tps = valides.map(p => (typeof p[2] === 'number' ? p[2] : null));
                const profil = valides.length === trace.length ? analyserProfil(trace) : null;
                v3d.pentes = profil ? profil.pentes : [];
                v3d.pts = pts;
                v3d.cum = [0];
                for (let i = 1; i < pts.length; i++) {
                    v3d.cum.push(v3d.cum[i - 1] + distanceM(pts[i - 1], pts[i]));
                }
                v3d.total = v3d.cum[v3d.cum.length - 1];
                v3d.dureeBase = Math.min(180, Math.max(45, (v3d.total / 1000) * 5));
            }

            // Distance (en mètres, le long du parcours) correspondant à un instant.
            function distanceALHeure(heureIso, debutIso) {
                const t0 = Date.parse(debutIso);
                const t = Date.parse(heureIso);
                if (isNaN(t0) || isNaN(t) || v3d.tps.some(x => x === null)) return null;

                const sec = (t - t0) / 1000;
                const tps = v3d.tps;
                const marge = 600;
                if (sec < -marge || sec > tps[tps.length - 1] + marge) return null;
                if (sec <= tps[0]) return 0;
                if (sec >= tps[tps.length - 1]) return v3d.total;

                for (let i = 1; i < tps.length; i++) {
                    if (tps[i] >= sec) {
                        const duree = tps[i] - tps[i - 1];
                        const part = duree > 0 ? (sec - tps[i - 1]) / duree : 0;
                        return v3d.cum[i - 1] + (v3d.cum[i] - v3d.cum[i - 1]) * part;
                    }
                }
                return null;
            }

            // Calcule où chaque photo se place sur le parcours (GPS de la photo, sinon son heure).
            function placerPhotos(sortie) {
                const resultat = [];
                const infos = sortie.photos_pos || [];

                (sortie.photos || []).forEach((mini, numero) => {
                    const info = infos[numero];
                    if (!info) return;

                    let d = null;
                    let coord = null;

                    if (typeof info.lat === 'number' && typeof info.lon === 'number') {
                        const gps = [info.lon, info.lat];
                        let meilleur = 0;
                        let ecart = Infinity;
                        v3d.pts.forEach((p, i) => {
                            const e = distanceM(p, gps);
                            if (e < ecart) { ecart = e; meilleur = i; }
                        });
                        if (ecart < 800) {
                            d = v3d.cum[meilleur];
                            coord = v3d.pts[meilleur];
                        }
                    }

                    if (d === null && info.heure) {
                        d = distanceALHeure(info.heure, sortie.debut);
                        if (d !== null) coord = pointA(d);
                    }

                    if (d !== null && coord) {
                        resultat.push({ numero: numero, mini: mini, d: d, coord: coord });
                    }
                });

                resultat.sort((a, b) => a.d - b.d);
                return resultat;
            }

            function cacherPhotoV3d() {
                clearTimeout(v3d.minuteur);
                document.getElementById('v3dPhoto').style.display = 'none';
            }

            async function montrerPhotoV3d(photo) {
                const cadre = document.getElementById('v3dPhoto');
                const img = document.getElementById('v3dPhotoImg');
                const jeton = ++jetonV3d;
                img.src = photo.mini;
                cadre.style.display = 'block';
                clearTimeout(v3d.minuteur);
                v3d.minuteur = setTimeout(cacherPhotoV3d, 5000);

                const cle = `${v3d.sortieId}-${photo.numero}`;
                try {
                    if (!photosCompletes[cle]) {
                        const res = await fetch(`/api/sorties/${v3d.sortieId}/photos/${photo.numero}`, {
                            headers: getHeaders()
                        });
                        if (res.ok) {
                            photosCompletes[cle] = (await res.json()).image;
                        }
                    }
                    if (photosCompletes[cle] && jeton === jetonV3d) {
                        img.src = photosCompletes[cle];
                    }
                } catch (e) {
                    console.error(e);
                }
            }

            // Affiche les photos dont le point de prise de vue vient d'être dépassé.
            function verifierPhotosV3d(avant, apres) {
                const liste = v3d.photos;
                let derniere = null;
                for (let i = 0; i < liste.length; i++) {
                    if (liste[i].d > avant && liste[i].d <= apres) derniere = liste[i];
                }
                if (derniere) montrerPhotoV3d(derniere);
            }

            function poserMarqueursV3d() {
                v3d.marqueurs.forEach(m => m.remove());
                v3d.marqueurs = [];
                viderReperesV3d();
                v3d.photos.forEach(photo => {
                    const el = document.createElement('button');
                    el.type = 'button';
                    el.className = 'v3d-marqueur';
                    el.textContent = '📷';
                    el.setAttribute('aria-label', 'Voir la photo');
                    el.addEventListener('click', (e) => {
                        e.stopPropagation();
                        montrerPhotoV3d(photo);
                    });
                    const marqueur = new window.maplibregl.Marker({ element: el })
                        .setLngLat(photo.coord)
                        .addTo(v3d.carte);
                    v3d.marqueurs.push(marqueur);
                });
            }


            // Repères du parcours : panneaux de pente et noms de lieux OpenStreetMap.
            function tronconsRaidesV3d(seuil) {
                const p = v3d.pentes, liste = [];
                if (!p.length || v3d.cum.length < p.length + 1) return liste;
                let i = 0;
                while (i < p.length) {
                    if (Math.abs(p[i]) >= seuil) {
                        let j = i, max = 0, somme = 0;
                        while (j < p.length && Math.abs(p[j]) >= seuil) { max = Math.max(max, Math.abs(p[j])); somme += p[j++]; }
                        const debut = v3d.cum[i], fin = v3d.cum[j];
                        if (fin - debut >= 60) liste.push({ valeur: max, signe: somme >= 0 ? 1 : -1, debut, fin, d: (debut + fin) / 2 });
                        i = j;
                    } else i++;
                }
                return liste;
            }
            function elementPanneauV3d(valeur, signe) {
                const el = document.createElement('div'); el.className = 'v3d-panneau';
                el.setAttribute('aria-label', 'Pente de ' + Math.round(valeur) + ' %');
                el.innerHTML = '<svg viewBox="0 0 48 44" aria-hidden="true"><polygon points="24,3 45,40 3,40" fill="white" stroke="#dc2626" stroke-width="4.5" stroke-linejoin="round"/><path d="' + (signe > 0 ? 'M17 27 L31 27 L31 18 Z' : 'M17 18 L17 27 L31 27 Z') + '" fill="#111827"/><text x="24" y="37" text-anchor="middle" font-size="11" font-weight="800" fill="#111827">' + Math.round(valeur) + '%</text></svg>';
                return el;
            }
            async function chercherLieuxV3d() {
                if (v3d.pts.length < 2) return [];
                const lons = v3d.pts.map(p => p[0]), lats = v3d.pts.map(p => p[1]), marge = 0.012;
                const sud = Math.min(...lats)-marge, nord = Math.max(...lats)+marge, ouest = Math.min(...lons)-marge, est = Math.max(...lons)+marge;
                if (nord-sud > 0.6 || est-ouest > 0.8) return [];
                const zone = '('+sud+','+ouest+','+nord+','+est+')';
                const q = '[out:json][timeout:35];(nwr["place"~"^(city|town|village|hamlet|isolated_dwelling|suburb|locality|neighbourhood)$"]["name"]'+zone+';nwr["natural"="peak"]["name"]'+zone+';nwr["mountain_pass"="yes"]["name"]'+zone+';nwr["natural"="saddle"]["name"]'+zone+';);out center tags;';
                let data = null;
                let derniereErreur = null;
                for (const base of ['https://overpass-api.de/api/interpreter','https://overpass.private.coffee/api/interpreter','https://overpass.kumi.systems/api/interpreter','https://maps.mail.ru/osm/tools/overpass/api/interpreter']) {
                    try {
                        const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 20000);
                        const res = await fetch(base, {
                            method:'POST',
                            headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},
                            body:'data='+encodeURIComponent(q),
                            signal:ctl.signal
                        });
                        clearTimeout(timer);
                        if (res.ok) { data = await res.json(); break; }
                        derniereErreur = 'HTTP '+res.status+' sur '+base;
                        console.warn('Recherche de lieux OSM :', derniereErreur);
                    } catch(e) { derniereErreur = String(e); console.warn('Repères OpenStreetMap indisponibles', e); }
                }
                if (!data || !Array.isArray(data.elements)) {
                    console.warn('Aucun résultat exploitable pour les lieux du survol 3D.', derniereErreur || 'réponse vide');
                    v3d.lieuxEtat = 'panne';
                    return [];
                }
                v3d.lieuxEtat = data.elements.length ? 'ok' : 'vide';
                console.info('Survol 3D : objets OpenStreetMap reçus', data.elements.length);
                const ref = v3d.pts.filter((_,i)=>i%Math.max(1,Math.floor(v3d.pts.length/500))===0);
                const dist = (lon,lat) => Math.min(...ref.map(p => {
                    const x=(lon-p[0])*111320*Math.cos(lat*Math.PI/180), y=(lat-p[1])*110540; return Math.hypot(x,y);
                }));
                const out=[], seen=new Set();
                for (const e of data.elements) {
                    if (!e.tags||!e.tags.name) continue;
                    const lon = Number.isFinite(e.lon) ? e.lon : e.center && e.center.lon;
                    const lat = Number.isFinite(e.lat) ? e.lat : e.center && e.center.lat;
                    if (!Number.isFinite(lon)||!Number.isFinite(lat)) continue;
                    let genre='',rang=9,portee=5000;
                    if (['city','town','village','hamlet','isolated_dwelling','suburb','locality'].includes(e.tags.place)) {
                        genre=['hamlet','isolated_dwelling','locality'].includes(e.tags.place)?'village':(e.tags.place==='village'?'village':'ville');
                        rang={city:1,town:2,suburb:3,village:4,hamlet:5,isolated_dwelling:6,locality:7}[e.tags.place] ?? 4;
                        portee=5000;
                    }
                    else if(e.tags.natural==='peak') {genre='sommet';rang=4;portee=1500;}
                    else if(e.tags.mountain_pass==='yes'||e.tags.natural==='saddle') {genre='col';rang=2;portee=500;}
                    else continue;
                    const ec=dist(lon,lat), key=e.tags.name.toLowerCase()+genre;
                    if(ec>portee||seen.has(key)) continue;
                    seen.add(key); out.push({nom:e.tags.name,genre,rang,ec,coord:[lon,lat],alt:parseInt(e.tags.ele,10)});
                }
                out.sort((a,b)=>a.rang-b.rang||a.ec-b.ec);
                const gardes=[];
                for(const l of out) { if(gardes.length>=18) break; if(gardes.every(g=>distanceM(g.coord,l.coord)>=600)) gardes.push(l); }
                return gardes;
            }
            function viderReperesV3d() {
                v3d.reperes.forEach(m=>m.remove()); v3d.reperes=[];
                v3d.panneauxPente=[];
            }
            function majVisibiliteReperesV3d() {
                const d = v3d.f * v3d.total;
                const avanceParSeconde = v3d.total / Math.max(1, v3d.dureeBase) * v3d.vitesse;
                v3d.reperes.forEach(m=>m.getElement().classList.toggle('v3d-cache',!v3d.repereVisibles));
                v3d.panneauxPente.forEach(p=>{
                    const visible = v3d.repereVisibles && d >= Math.max(0,p.debut-avanceParSeconde) && d <= p.fin;
                    p.marqueur.getElement().classList.toggle('v3d-cache',!visible);
                });
            }
            async function poserReperesV3d() {
                const carte=v3d.carte; viderReperesV3d();
                const raides=tronconsRaidesV3d(15), liste=raides.length?raides:tronconsRaidesV3d(10).sort((a,b)=>b.valeur-a.valeur).slice(0,3);
                liste.sort((a,b)=>b.valeur-a.valeur).slice(0,8).forEach(t=>{
                    const m=new window.maplibregl.Marker({element:elementPanneauV3d(t.valeur,t.signe),anchor:'bottom'}).setLngLat(pointA(t.debut)).addTo(carte);
                    v3d.reperes.push(m);
                    v3d.panneauxPente.push({marqueur:m,debut:t.debut,fin:t.fin});
                });
                majVisibiliteReperesV3d(); majBoutonsV3d();
                let lieux=[];
                const rech=document.getElementById('v3dRecherche');
                if(rech) rech.style.display='flex';
                try { lieux=await chercherLieuxV3d(); } catch(e) { console.warn(e); }
                if(rech) rech.style.display='none';
                if(v3d.carte!==carte) return;
                if(v3d.lieuxEtat==='panne') { messageV3d('Noms de lieux indisponibles : le serveur OpenStreetMap ne répond pas.'); setTimeout(()=>messageV3d(''),6000); }
                lieux.forEach(l=>{
                    const el=document.createElement('div'); el.className='v3d-lieu'+(['sommet','col'].includes(l.genre)?' v3d-lieu-haut':'');
                    const ic=l.genre==='sommet'?'⛰️':l.genre==='col'?'🏔️':'🏘️';
                    el.textContent=ic+' '+l.nom+(Number.isFinite(l.alt)&&['sommet','col'].includes(l.genre)?' · '+fmtAltitude(l.alt).complet:'');
                    v3d.reperes.push(new window.maplibregl.Marker({element:el,anchor:'bottom'}).setLngLat(l.coord).addTo(carte));
                });
                majVisibiliteReperesV3d(); majBoutonsV3d();
            }

            function messageV3d(texte) {
                const m = document.getElementById('v3dMessage');
                if (!texte) {
                    m.style.display = 'none';
                    return;
                }
                m.textContent = texte;
                m.style.display = 'block';
            }

            function majBoutonsV3d() {
                remplirBouton(document.getElementById('v3dLecture'),
                    v3d.jouer ? 'pause' : 'lecture', v3d.jouer ? 'Pause' : 'Lecture');
                document.getElementById('v3dLecture').classList.toggle('actif', !v3d.jouer);
                remplirBouton(document.getElementById('v3dVitesse'), 'vitesse', `×${v3d.vitesse}`);
                remplirBouton(document.getElementById('v3dInclinaison'), 'incl', `${v3d.inclinaison}°`);
                remplirBouton(document.getElementById('v3dFond'), 'fond',
                    v3d.fond === 'sat' ? 'Satellite' : 'Plan');
                const couleur = document.getElementById('v3dCouleur');
                const aTerrain = v3d.surfaces && v3d.surfaces.length === v3d.pts.length;
                couleur.style.display = (v3d.pentes.length && aTerrain) ? '' : 'none';
                remplirBouton(couleur, 'incl', v3d.mode === 'pente' ? 'Couleurs : pente' : 'Couleurs : terrain');
                const autres = document.getElementById('v3dAutres');
                const rep = document.getElementById('v3dReperes');
                remplirBouton(rep, 'montagne', 'Repères');
                rep.classList.toggle('actif', v3d.repereVisibles);
                rep.style.display = (v3d.reperes.length || v3d.prete) ? '' : 'none';
                remplirBouton(autres, 'incl', 'Mes parcours');
                autres.classList.toggle('actif', v3d.voirAutres);
                autres.style.display = sortiesConnues.some(o => o.id !== v3d.sortieId && Array.isArray(o.trace) && o.trace.length >= 2) ? '' : 'none';
                const enr = document.getElementById('v3dEnreg');
                const en_cours = !!v3d.enregistreur;
                remplirBouton(enr, 'enreg', en_cours ? 'Arrêter et enregistrer' : 'Enregistrer');
                enr.classList.toggle('enreg', en_cours);
            }

            function dessinerAvancement() {
                const carte = v3d.carte;
                if (!carte || !v3d.prete) return;

                const d = v3d.f * v3d.total;
                const pos = pointA(d);
                const fait = [];
                for (let i = 0; i < v3d.pts.length && v3d.cum[i] <= d; i++) {
                    fait.push(v3d.pts[i]);
                }
                fait.push(pos);

                const srcFait = carte.getSource('v3d-fait');
                if (srcFait) {
                    srcFait.setData({
                        type: 'Feature',
                        geometry: { type: 'LineString', coordinates: fait.length > 1 ? fait : [pos, pos] }
                    });
                }
                const srcPos = carte.getSource('v3d-pos');
                if (srcPos) {
                    srcPos.setData({
                        type: 'Feature',
                        geometry: { type: 'Point', coordinates: pos }
                    });
                }
                document.getElementById('v3dProgres').value = String(Math.round(v3d.f * 1000));
                document.getElementById('v3dInfo').textContent =
                    `${fmtDistance(d / 1000, 1).complet} sur ${fmtDistance(v3d.total / 1000, 1).complet}`;
                majVisibiliteReperesV3d();
            }

            // Place la caméra derrière le point courant, orientée vers la suite du parcours.
            function placerCamera(dt, brusque) {
                const carte = v3d.carte;
                if (!carte || !v3d.prete) return;

                const d = v3d.f * v3d.total;
                const centre = pointA(d);
                const avance = Math.min(600, Math.max(120, v3d.total * 0.03));
                const devant = pointA(Math.min(v3d.total, d + avance));
                const arriere = pointA(Math.max(0, d - avance * 0.5));
                let cible = v3d.cap;
                if (distanceM(arriere, devant) > 5) {
                    cible = capEntre(arriere, devant);
                }

                if (brusque) {
                    v3d.cap = cible;
                } else {
                    const facteur = 1 - Math.exp(-dt * 2.5);
                    v3d.cap = (v3d.cap + ecartAngle(cible, v3d.cap) * facteur + 360) % 360;
                }

                const hauteur = document.getElementById('carte3d').clientHeight || 600;

                // La correction de cadrage via project() est coûteuse avec le relief 3D.
                // On la calcule périodiquement, sans réduire les points GPS ni la fréquence
                // de déplacement de la caméra.
                v3d.compteCadrage = (v3d.compteCadrage || 0) + 1;
                if (typeof carte.project === 'function' && !brusque && v3d.compteCadrage % 6 === 0) {
                    try {
                        const ecran = carte.project(centre);
                        if (ecran && isFinite(ecran.y)) {
                            const erreur = hauteur * 0.52 - ecran.y;
                            if (Math.abs(erreur) > 3) {
                                v3d.decalage = Math.max(-hauteur, Math.min(hauteur * 1.6, v3d.decalage + erreur * 0.8));
                            }
                        }
                    } catch (e) {
                        console.warn('Correction du cadrage 3D indisponible', e);
                    }
                }

                carte.jumpTo({
                    center: centre,
                    bearing: v3d.cap,
                    pitch: v3d.inclinaison,
                    zoom: 14.6,
                    padding: { top: v3d.decalage, bottom: 0, left: 0, right: 0 }
                });

                // Mesure juste après le déplacement : sert de correction immédiate à l'arrêt ou au curseur.
                if (brusque && typeof carte.project === 'function') {
                    try {
                        const ecran = carte.project(centre);
                        if (ecran && isFinite(ecran.y)) {
                            const erreur = hauteur * 0.52 - ecran.y;
                            v3d.decalage = Math.max(-hauteur, Math.min(hauteur * 1.6, v3d.decalage + erreur * 0.8));
                        }
                    } catch (e) {
                        console.error(e);
                    }
                }
            }

            function imageV3d(t) {
                v3d.raf = 0;
                if (!v3d.jouer) return;

                const dt = Math.min(0.1, Math.max(0, (t - v3d.dernier) / 1000));
                v3d.dernier = t;
                const avant = v3d.f * v3d.total;
                v3d.f = Math.min(1, v3d.f + (dt * v3d.vitesse) / v3d.dureeBase);
                verifierPhotosV3d(avant, v3d.f * v3d.total);

                placerCamera(dt, false);
                v3d.compteImages++;
                // La caméra avance à chaque image, mais la ligne GeoJSON du parcours
                // est plus coûteuse à recalculer : on la rafraîchit moins souvent.
                if (v3d.compteImages % 4 === 0 || v3d.f >= 1) dessinerAvancement();

                if (v3d.f >= 1) {
                    v3d.jouer = false;
                    if (v3d.enregistreur) arreterEnregistrement(true);
                    majBoutonsV3d();
                    return;
                }
                v3d.raf = requestAnimationFrame(imageV3d);
            }

            function lectureV3d(jouer) {
                if (!v3d.prete) return;
                if (jouer && v3d.f >= 1) v3d.f = 0;
                v3d.jouer = jouer;
                if (jouer) {
                    messageV3d('');
                    v3d.dernier = performance.now();
                    placerCamera(0, v3d.f === 0);
                    dessinerAvancement();
                    if (!v3d.raf) v3d.raf = requestAnimationFrame(imageV3d);
                } else if (v3d.raf) {
                    cancelAnimationFrame(v3d.raf);
                    v3d.raf = 0;
                }
                majBoutonsV3d();
            }

            function arreterEnregistrement(sauver) {
                const rec = v3d.enregistreur;
                if (!rec) return;
                v3d.enregistreur = null;
                v3d.sauverVideo = sauver;
                try {
                    if (rec.state !== 'inactive') rec.stop();
                } catch (e) {
                    console.error(e);
                }
                majBoutonsV3d();
            }

            function demarrerEnregistrement() {
                const canvas = v3d.carte && v3d.carte.getCanvas ? v3d.carte.getCanvas() : null;
                const types = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
                const type = window.MediaRecorder
                    ? types.find(t => MediaRecorder.isTypeSupported(t))
                    : null;

                if (!canvas || !canvas.captureStream || !type) {
                    messageV3d("L'enregistrement n'est pas pris en charge par ce navigateur. Utilise l'enregistrement d'écran de ton téléphone pendant le survol.");
                    return;
                }

                const flux = canvas.captureStream(30);
                const rec = new MediaRecorder(flux, { mimeType: type, videoBitsPerSecond: 6000000 });
                v3d.morceaux = [];
                v3d.sauverVideo = false;
                rec.ondataavailable = (e) => {
                    if (e.data && e.data.size > 0) v3d.morceaux.push(e.data);
                };
                rec.onstop = () => {
                    if (!v3d.sauverVideo || v3d.morceaux.length === 0) return;
                    const blob = new Blob(v3d.morceaux, { type: type });
                    const a = document.createElement('a');
                    a.href = URL.createObjectURL(blob);
                    a.download = `survol-3d-${v3d.titre}.webm`;
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
                    messageV3d('Vidéo enregistrée dans tes téléchargements.');
                    setTimeout(() => messageV3d(''), 3500);
                };

                v3d.enregistreur = rec;
                v3d.f = 0;
                rec.start(1000);
                lectureV3d(true);
            }

            function creerCarte(conteneur) {
                const maplibregl = window.maplibregl;
                const carte = new maplibregl.Map({
                    container: conteneur,
                    style: {
                        version: 8,
                        sources: {
                            sat: {
                                type: 'raster',
                                tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
                                tileSize: 256,
                                maxzoom: 19,
                                attribution: 'Imagerie © Esri, Maxar, Earthstar Geographics'
                            },
                            plan: {
                                type: 'raster',
                                tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
                                tileSize: 256,
                                maxzoom: 19,
                                attribution: '© contributeurs OpenStreetMap'
                            }
                        },
                        layers: [
                            { id: 'fond-sat', type: 'raster', source: 'sat' },
                            { id: 'fond-plan', type: 'raster', source: 'plan', layout: { visibility: 'none' } }
                        ]
                    },
                    center: v3d.pts[0],
                    zoom: 12,
                    pitch: 45,
                    maxPitch: 85,
                    attributionControl: true
                });
                return carte;
            }

            function couleurSegment(i) {
                if (v3d.mode === 'terrain' && v3d.surfaces && v3d.surfaces.length === v3d.pts.length) {
                    return (TYPES_CHEMIN[v3d.surfaces[i]] || TYPES_CHEMIN.u).couleur;
                }
                if (v3d.pentes.length > i) return couleurPente(v3d.pentes[i]);
                return '#ffffff';
            }

            function donneesTrace() {
                const features = [];
                for (let i = 0; i < v3d.pts.length - 1; i++) {
                    features.push({
                        type: 'Feature',
                        properties: { c: couleurSegment(i) },
                        geometry: { type: 'LineString', coordinates: [v3d.pts[i], v3d.pts[i + 1]] }
                    });
                }
                return { type: 'FeatureCollection', features: features };
            }

            function majLegendeV3d() {
                const el = document.getElementById('v3dLegende');
                el.textContent = '';
                let lignes = [];
                if (v3d.mode === 'terrain') {
                    lignes = Object.keys(TYPES_CHEMIN)
                        .filter(k => v3d.surfaces.includes(k))
                        .map(k => ({ couleur: TYPES_CHEMIN[k].couleur, texte: TYPES_CHEMIN[k].nom }));
                } else if (v3d.pentes.length) {
                    lignes = COULEURS_PENTE.map(c => ({ couleur: c.couleur, texte: c.nom }));
                }
                el.style.display = lignes.length ? 'flex' : 'none';
                lignes.forEach(l => {
                    const sp = document.createElement('span');
                    const i = document.createElement('i');
                    i.style.background = l.couleur;
                    sp.appendChild(i);
                    sp.appendChild(document.createTextNode(l.texte));
                    el.appendChild(sp);
                });
            }

            function installerCouches() {
                const carte = v3d.carte;
                carte.addSource('dem', {
                    type: 'raster-dem',
                    tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
                    encoding: 'terrarium',
                    tileSize: 256,
                    maxzoom: 15,
                    attribution: 'Relief : AWS Terrain Tiles'
                });
                carte.setTerrain({ source: 'dem', exaggeration: 1.1 });

                // Les autres parcours sont chargés uniquement au premier clic sur « Mes parcours ».

                carte.addSource('v3d-trace', { type: 'geojson', data: donneesTrace() });
                carte.addLayer({
                    id: 'v3d-trace-contour', type: 'line', source: 'v3d-trace',
                    layout: { 'line-cap': 'round', 'line-join': 'round' },
                    paint: { 'line-color': '#04231f', 'line-width': 7, 'line-opacity': 0.55 }
                });
                carte.addLayer({
                    id: 'v3d-trace-ligne', type: 'line', source: 'v3d-trace',
                    layout: { 'line-cap': 'round', 'line-join': 'round' },
                    paint: { 'line-color': ['get', 'c'], 'line-width': 5, 'line-opacity': 1 }
                });

                carte.addSource('v3d-fait', {
                    type: 'geojson',
                    data: { type: 'Feature', geometry: { type: 'LineString', coordinates: [v3d.pts[0], v3d.pts[0]] } }
                });
                carte.addLayer({
                    id: 'v3d-fait-ligne', type: 'line', source: 'v3d-fait',
                    layout: { 'line-cap': 'round', 'line-join': 'round' },
                    paint: { 'line-color': '#ffffff', 'line-width': 1.6, 'line-opacity': 0.95 }
                });

                carte.addSource('v3d-pos', {
                    type: 'geojson',
                    data: { type: 'Feature', geometry: { type: 'Point', coordinates: v3d.pts[0] } }
                });
                carte.addLayer({
                    id: 'v3d-pos-point', type: 'circle', source: 'v3d-pos',
                    paint: {
                        'circle-radius': 8, 'circle-color': '#ffffff',
                        'circle-stroke-color': '#0a8f9c', 'circle-stroke-width': 4
                    }
                });
            }

            function vueEnsemble() {
                const carte = v3d.carte;
                const bornes = new window.maplibregl.LngLatBounds(v3d.pts[0], v3d.pts[0]);
                v3d.pts.forEach(p => bornes.extend(p));
                carte.fitBounds(bornes, { padding: 60, pitch: 45, bearing: 0, duration: 0 });
            }

            function fermerV3d() {
                if (v3d.enregistreur) arreterEnregistrement(false);
                v3d.jouer = false;
                if (v3d.raf) {
                    cancelAnimationFrame(v3d.raf);
                    v3d.raf = 0;
                }
                if (v3d.carte) {
                    try { v3d.carte.remove(); } catch (e) { console.error(e); }
                    v3d.carte = null;
                }
                v3d.prete = false;
                v3d.marqueurs.forEach(m => m.remove());
                v3d.marqueurs = [];
                cacherPhotoV3d();
                document.getElementById('vue3d').style.display = 'none';
                messageV3d('');
            }

            async function ouvrirV3d(sortie) {
                if (!sortie.trace || sortie.trace.length < 2) {
                    alert("Cette sortie n'a pas de parcours GPS pour le survol 3D.");
                    return;
                }

                fermerV3d();
                preparerParcours(sortie.trace);
                v3d.f = 0;
                v3d.vitesse = 1;
                v3d.fond = 'sat';
                v3d.compteImages = 0;
                v3d.titre = (sortie.debut || '').slice(0, 10);
                v3d.sortieId = sortie.id;
                v3d.photos = placerPhotos(sortie);
                v3d.voirAutres = false;
                v3d.autres = [];
                v3d.repereVisibles = true;
                v3d.decalage = 0;
                v3d.mode = 'pente';
                v3d.surfaces = sortie.surfaces || '';
                const jour = new Date(sortie.debut).toLocaleDateString('fr-FR');
                document.getElementById('v3dTitre').textContent = `${sortie.nom} · ${jour}`;
                document.getElementById('v3dProgres').value = '0';
                document.getElementById('v3dInfo').textContent = '';
                document.getElementById('vue3d').style.display = 'block';
                majBoutonsV3d();
                messageV3d('Chargement de la carte 3D…');

                try {
                    await chargerMapLibre();
                } catch (e) {
                    messageV3d("Impossible de charger la carte 3D. Vérifie ta connexion internet puis réessaie.");
                    return;
                }

                if (document.getElementById('vue3d').style.display === 'none') return;

                try {
                    v3d.carte = creerCarte(document.getElementById('carte3d'));
                    v3d.carte.on('load', () => {
                        if (!v3d.carte) return;
                        installerCouches();
                        poserMarqueursV3d();
                        poserReperesV3d();
                        majLegendeV3d();
                        majBoutonsV3d();
                        vueEnsemble();
                        v3d.prete = true;
                        v3d.cap = capEntre(v3d.pts[0], pointA(Math.min(v3d.total, 300)));
                        messageV3d(v3d.photos.length
                            ? `Appuie sur Lecture pour lancer le survol (${v3d.photos.length} photo(s) placée(s) sur le parcours).`
                            : 'Appuie sur Lecture pour lancer le survol.');
                        dessinerAvancement();
                    });
                    v3d.carte.on('error', (e) => console.error(e && e.error ? e.error : e));
                } catch (e) {
                    console.error(e);
                    messageV3d("Ton navigateur ne peut pas afficher la carte 3D (WebGL indisponible).");
                }
            }

            (function brancherV3d() {
                const bouton = (id, fn) => document.getElementById(id).addEventListener('click', fn);
                remplirBouton(document.getElementById('v3dFermer'), 'fermer', '');
                majBoutonsV3d();

                bouton('v3dFermer', fermerV3d);
                bouton('v3dReperes', () => { v3d.repereVisibles = !v3d.repereVisibles; majVisibiliteReperesV3d(); majBoutonsV3d(); });
                bouton('v3dLecture', () => lectureV3d(!v3d.jouer));
                bouton('v3dVitesse', () => {
                    v3d.vitesse = VITESSES[(VITESSES.indexOf(v3d.vitesse) + 1) % VITESSES.length];
                    majBoutonsV3d();
                });
                bouton('v3dInclinaison', () => {
                    v3d.inclinaison = INCLINAISONS[(INCLINAISONS.indexOf(v3d.inclinaison) + 1) % INCLINAISONS.length];
                    if (!v3d.jouer) placerCamera(0, true);
                    majBoutonsV3d();
                });
                bouton('v3dFond', () => {
                    v3d.fond = v3d.fond === 'sat' ? 'plan' : 'sat';
                    if (v3d.carte && v3d.prete) {
                        v3d.carte.setLayoutProperty('fond-sat', 'visibility', v3d.fond === 'sat' ? 'visible' : 'none');
                        v3d.carte.setLayoutProperty('fond-plan', 'visibility', v3d.fond === 'plan' ? 'visible' : 'none');
                    }
                    majBoutonsV3d();
                });
                bouton('v3dCouleur', () => {
                    v3d.mode = v3d.mode === 'pente' ? 'terrain' : 'pente';
                    const src = v3d.carte && v3d.carte.getSource('v3d-trace');
                    if (src) src.setData(donneesTrace());
                    majLegendeV3d();
                    majBoutonsV3d();
                });
                bouton('v3dAutres', () => {
                    v3d.voirAutres = !v3d.voirAutres;
                    if (v3d.carte && v3d.prete && v3d.voirAutres) {
                        if (!v3d.carte.getSource('v3d-autres')) {
                            v3d.autres = sortiesConnues
                                .filter(o => o.id !== v3d.sortieId && Array.isArray(o.trace) && o.trace.length >= 2)
                                .map(o => o.trace.map(p => [p[1], p[0]]));
                            if (v3d.autres.length) {
                                v3d.carte.addSource('v3d-autres', {
                                    type: 'geojson',
                                    data: { type: 'Feature', geometry: { type: 'MultiLineString', coordinates: v3d.autres } }
                                });
                                v3d.carte.addLayer({
                                    id: 'v3d-autres-ligne', type: 'line', source: 'v3d-autres',
                                    layout: { 'line-cap': 'round', 'line-join': 'round', visibility: 'visible' },
                                    paint: { 'line-color': '#ffffff', 'line-width': 3, 'line-opacity': 0.9 }
                                });
                            } else {
                                v3d.voirAutres = false;
                            }
                        } else if (v3d.carte.getLayer('v3d-autres-ligne')) {
                            v3d.carte.setLayoutProperty('v3d-autres-ligne', 'visibility', 'visible');
                        }
                    } else if (v3d.carte && v3d.prete && v3d.carte.getLayer('v3d-autres-ligne')) {
                        v3d.carte.setLayoutProperty('v3d-autres-ligne', 'visibility', 'none');
                    }
                    majBoutonsV3d();
                });
                bouton('v3dEnreg', () => {
                    if (!v3d.prete) return;
                    if (v3d.enregistreur) {
                        arreterEnregistrement(true);
                    } else {
                        demarrerEnregistrement();
                    }
                });
                document.getElementById('v3dProgres').addEventListener('input', (e) => {
                    if (!v3d.prete) return;
                    v3d.f = Number(e.target.value) / 1000;
                    messageV3d('');
                    placerCamera(0, true);
                    dessinerAvancement();
                });
                document.addEventListener('keydown', (e) => {
                    if (document.getElementById('vue3d').style.display === 'none') return;
                    if (e.key === 'Escape') fermerV3d();
                    else if (e.key === ' ' && e.target === document.body) {
                        e.preventDefault();
                        lectureV3d(!v3d.jouer);
                    }
                });
            })();

            // Identifiant d'une vidéo YouTube (youtu.be, watch?v=, shorts, embed), sinon null.
            function idYoutube(adresse) {
                try {
                    const u = new URL(adresse);
                    const hote = u.hostname.replace('www.', '').replace('m.', '');
                    let id = null;
                    if (hote === 'youtu.be') {
                        id = u.pathname.split('/')[1];
                    } else if (hote === 'youtube.com' || hote === 'music.youtube.com') {
                        const parties = u.pathname.split('/');
                        if (parties[1] === 'shorts' || parties[1] === 'embed' || parties[1] === 'live') {
                            id = parties[2];
                        } else {
                            id = u.searchParams.get('v');
                        }
                    }
                    return id && /^[A-Za-z0-9_-]{6,15}$/.test(id) ? id : null;
                } catch (e) {
                    return null;
                }
            }

            function nomDeSite(adresse) {
                try {
                    return new URL(adresse).hostname.replace('www.', '');
                } catch (e) {
                    return adresse;
                }
            }

            function carteVideo(sortieId, v, numero) {
                const bloc = document.createElement('div');
                bloc.className = 'video-carte';

                const lien = document.createElement('a');
                lien.href = v.url;
                lien.target = '_blank';
                lien.rel = 'noopener noreferrer';
                lien.className = 'video-lien';

                const vignette = document.createElement('span');
                vignette.className = 'video-vignette';
                const id = idYoutube(v.url);
                if (id) {
                    const img = document.createElement('img');
                    img.alt = '';
                    img.loading = 'lazy';
                    img.src = 'https://i.ytimg.com/vi/' + id + '/hqdefault.jpg';
                    img.addEventListener('error', () => img.remove());
                    vignette.appendChild(img);
                }
                const play = document.createElement('span');
                play.className = 'video-play';
                play.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>';
                vignette.appendChild(play);

                const texte = document.createElement('span');
                texte.className = 'video-texte';
                const titre = document.createElement('strong');
                titre.textContent = v.titre || 'Vidéo de la sortie';
                const site = document.createElement('small');
                site.textContent = nomDeSite(v.url);
                texte.appendChild(titre);
                texte.appendChild(site);

                lien.appendChild(vignette);
                lien.appendChild(texte);

                const retirer = document.createElement('button');
                retirer.type = 'button';
                retirer.className = 'video-retirer';
                retirer.setAttribute('aria-label', 'Retirer cette vidéo');
                retirer.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
                retirer.addEventListener('click', () => retirerVideo(sortieId, numero));

                bloc.appendChild(lien);
                bloc.appendChild(retirer);
                return bloc;
            }

            async function ajouterVideo(sortieId) {
                const url = prompt(
                    "Colle le lien de la vidéo (YouTube, Google Photos, Drive...) :"
                );
                if (url === null || url.trim() === '') return;

                const titre = prompt("Titre (facultatif) :", '');
                if (titre === null) return;

                try {
                    const res = await fetch(`/api/sorties/${sortieId}/videos`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ url: url.trim(), titre: titre.trim() })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        const err = await res.json().catch(() => ({}));
                        alert(typeof err.detail === 'string' ? err.detail : "Lien refusé.");
                        return;
                    }

                    await chargerEtat();

                } catch (e) {
                    alert("Impossible d'ajouter la vidéo.");
                }
            }

            function echapperXml(valeur) {
                return String(valeur || '')
                    .replaceAll('&', '&amp;')
                    .replaceAll('<', '&lt;')
                    .replaceAll('>', '&gt;')
                    .replaceAll(String.fromCharCode(34), '&quot;')
                    .replaceAll(String.fromCharCode(39), '&apos;');
            }

            function exporterSortieGpx(sortie) {
                const trace = Array.isArray(sortie.trace) ? sortie.trace : [];
                const points = trace.filter(p => Array.isArray(p) && p.length >= 2
                    && p[0] !== null && p[0] !== ''
                    && p[1] !== null && p[1] !== ''
                    && Number.isFinite(Number(p[0]))
                    && Number.isFinite(Number(p[1])));

                if (points.length < 2) {
                    alert("Cette sortie ne contient pas assez de points GPS pour créer un fichier GPX.");
                    return;
                }

                const xmlPoints = points.map(p => {
                    const latitude = Number(p[0]);
                    const longitude = Number(p[1]);
                    // Les points sont enregistrés [latitude, longitude, secondes depuis le départ, altitude].
                    let altitude = '';
                    if (p[3] !== null && p[3] !== undefined && p[3] !== '' && Number.isFinite(Number(p[3]))) {
                        altitude = `<ele>${Number(p[3])}</ele>`;
                    }
                    let heure = '';
                    const depart = Date.parse(sortie.debut);
                    if (Number.isFinite(depart) && p[2] !== null && p[2] !== undefined && p[2] !== '' && Number.isFinite(Number(p[2]))) {
                        heure = `<time>${new Date(depart + Number(p[2]) * 1000).toISOString()}</time>`;
                    }
                    return `<trkpt lat="${latitude}" lon="${longitude}">${altitude}${heure}</trkpt>`;
                });

                const xml = [
                    '<?xml version="1.0" encoding="UTF-8"?>',
                    '<gpx version="1.1" creator="Maillons" xmlns="http://www.topografix.com/GPX/1/1">',
                    '<trk>',
                    `<name>${echapperXml(sortie.nom || 'Sortie VTT')}</name>`,
                    '<trkseg>',
                    ...xmlPoints,
                    '</trkseg>',
                    '</trk>',
                    '</gpx>'
                ].join(String.fromCharCode(10));

                const blob = new Blob([xml], { type: 'application/gpx+xml;charset=utf-8' });
                const url = URL.createObjectURL(blob);
                const lien = document.createElement('a');
                const nomFichier = String(sortie.nom || 'sortie-vtt')
                    .replace(/[^a-z0-9]+/gi, '-')
                    .replace(/^-+|-+$/g, '') || 'sortie-vtt';
                lien.href = url;
                lien.download = `${nomFichier}.gpx`;
                lien.style.display = 'none';
                document.body.appendChild(lien);
                lien.click();
                lien.remove();
                window.setTimeout(() => URL.revokeObjectURL(url), 1000);
            }

            async function modifierNotesSortie(sortie) {
                const notes = prompt("Notes de la sortie (laisser vide pour effacer) :", sortie.notes || '');
                if (notes === null || notes.trim() === (sortie.notes || '').trim()) return;

                try {
                    const res = await fetch(`/api/sorties/${sortie.id}/modifier`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ notes: notes.trim() || null })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) throw new Error();
                    await chargerEtat();

                } catch (e) {
                    alert("Impossible de modifier les notes de cette sortie.");
                }
            }


            async function renommerSortie(sortie) {
                const nom = prompt("Nouveau nom de la sortie :", sortie.nom);
                if (nom === null || nom.trim() === '' || nom.trim() === sortie.nom) return;

                try {
                    const res = await fetch(`/api/sorties/${sortie.id}/nom`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ nom: nom.trim() })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    await chargerEtat();

                } catch (e) {
                    alert("Impossible de renommer la sortie (120 caractères maximum).");
                }
            }

            async function retirerVideo(sortieId, numero) {
                if (!confirm("Retirer cette vidéo de la sortie ? (la vidéo elle-même n'est pas supprimée)")) {
                    return;
                }
                try {
                    const res = await fetch(
                        `/api/sorties/${sortieId}/videos/${numero}/supprimer`,
                        { method: 'POST', headers: getHeaders() }
                    );
                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }
                    if (!res.ok) throw new Error();
                    await chargerEtat();
                } catch (e) {
                    alert("Impossible de retirer la vidéo.");
                }
            }

            document.getElementById('lightboxRetirer')
                .addEventListener('click', retirerPhotoOuverte);


            // Réduit une photo dans le navigateur avant l'envoi :
            // une version moyenne (affichée au clic) et une miniature.
            // Lit le lieu (GPS) et l'heure de prise de vue dans l'en-tête d'une photo JPEG.
            async function lireExif(fichier) {
                const vide = { lat: null, lon: null, heure: null };

                try {
                    const buf = await fichier.slice(0, 262144).arrayBuffer();
                    const v = new DataView(buf);

                    if (v.byteLength < 12 || v.getUint16(0) !== 0xFFD8) {
                        return vide;
                    }

                    let pos = 2;
                    let debutTiff = -1;

                    while (pos + 4 < v.byteLength) {
                        const marqueur = v.getUint16(pos);
                        const taille = v.getUint16(pos + 2);

                        if (marqueur === 0xFFE1 && v.getUint32(pos + 4) === 0x45786966) {
                            debutTiff = pos + 10;
                            break;
                        }
                        if ((marqueur & 0xFF00) !== 0xFF00 || marqueur === 0xFFDA) {
                            break;
                        }
                        pos += 2 + taille;
                    }

                    if (debutTiff < 0) {
                        return vide;
                    }

                    const petit = v.getUint16(debutTiff) === 0x4949;
                    const u16 = (o) => v.getUint16(debutTiff + o, petit);
                    const u32 = (o) => v.getUint32(debutTiff + o, petit);
                    const tailleType = { 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1 };

                    const lireIfd = (decalage) => {
                        const entrees = {};
                        const n = u16(decalage);

                        for (let i = 0; i < n && i < 200; i++) {
                            const e = decalage + 2 + i * 12;
                            const tag = u16(e);
                            const type = u16(e + 2);
                            const nombre = u32(e + 4);
                            const octets = (tailleType[type] || 1) * nombre;
                            const donnees = octets <= 4 ? e + 8 : u32(e + 8);
                            entrees[tag] = { type: type, nombre: nombre, donnees: donnees };
                        }
                        return entrees;
                    };

                    const texte = (e) => {
                        let t = '';
                        for (let i = 0; i < e.nombre; i++) {
                            const c = v.getUint8(debutTiff + e.donnees + i);
                            if (c === 0) {
                                break;
                            }
                            t += String.fromCharCode(c);
                        }
                        return t;
                    };

                    const rationnels = (e) => {
                        const r = [];
                        for (let i = 0; i < e.nombre; i++) {
                            const num = u32(e.donnees + i * 8);
                            const den = u32(e.donnees + i * 8 + 4);
                            r.push(den ? num / den : 0);
                        }
                        return r;
                    };

                    const ifd0 = lireIfd(u32(4));
                    const resultat = { lat: null, lon: null, heure: null };

                    if (ifd0[0x8769]) {
                        const exif = lireIfd(u32(ifd0[0x8769].donnees));
                        const brut = exif[0x9003] || exif[0x9004];
                        if (brut) {
                            const parties = texte(brut).trim().split(' ');
                            const jour = (parties[0] || '').split(':');
                            const heure = (parties[1] || '').split(':');
                            if (jour.length === 3 && heure.length === 3) {
                                const n = jour.concat(heure).map(Number);
                                const decalage = exif[0x9011] ? texte(exif[0x9011]).trim() : '';
                                const decalageOk = decalage.length === 6
                                    && (decalage[0] === '+' || decalage[0] === '-')
                                    && decalage[3] === ':';
                                const iso = `${jour[0]}-${jour[1]}-${jour[2]}T${heure[0]}:${heure[1]}:${heure[2]}`;
                                const date = decalageOk
                                    ? new Date(iso + decalage)
                                    : new Date(n[0], n[1] - 1, n[2], n[3], n[4], n[5]);
                                if (!isNaN(date.getTime())) {
                                    resultat.heure = date.toISOString().slice(0, 19) + 'Z';
                                }
                            }
                        }
                    }

                    if (ifd0[0x8825]) {
                        const gps = lireIfd(u32(ifd0[0x8825].donnees));
                        if (gps[2] && gps[4] && gps[1] && gps[3]) {
                            const dms = (e) => {
                                const r = rationnels(e);
                                return r[0] + (r[1] || 0) / 60 + (r[2] || 0) / 3600;
                            };
                            let lat = dms(gps[2]);
                            let lon = dms(gps[4]);
                            if (texte(gps[1]).startsWith('S')) lat = -lat;
                            if (texte(gps[3]).startsWith('W')) lon = -lon;
                            if (isFinite(lat) && isFinite(lon) && (lat !== 0 || lon !== 0)
                                && Math.abs(lat) <= 90 && Math.abs(lon) <= 180) {
                                resultat.lat = Math.round(lat * 1e6) / 1e6;
                                resultat.lon = Math.round(lon * 1e6) / 1e6;
                            }
                        }
                    }

                    return resultat;
                } catch (e) {
                    return vide;
                }
            }

            async function preparerPhoto(fichier) {
                const exif = await lireExif(fichier);
                const image = await createImageBitmap(fichier);

                const encoder = (cote, qualite) => {
                    const echelle = Math.min(
                        1,
                        cote / Math.max(image.width, image.height)
                    );
                    const canvas = document.createElement('canvas');
                    canvas.width = Math.max(1, Math.round(image.width * echelle));
                    canvas.height = Math.max(1, Math.round(image.height * echelle));
                    canvas.getContext('2d').drawImage(
                        image, 0, 0, canvas.width, canvas.height
                    );
                    return canvas.toDataURL('image/jpeg', qualite);
                };

                let photo = '';

                // On essaie des réglages de plus en plus légers
                // jusqu'à passer sous la limite du serveur.
                const essais = [[1280, 0.75], [1280, 0.6], [1024, 0.55], [800, 0.5]];

                for (const [cote, qualite] of essais) {
                    photo = encoder(cote, qualite);

                    if (photo.length <= 650000) {
                        break;
                    }
                }

                const mini = encoder(240, 0.7);

                if (image.close) {
                    image.close();
                }

                if (
                    !photo.startsWith('data:image/jpeg') ||
                    photo.length > 700000
                ) {
                    throw new Error('photo');
                }

                const sortie = { mini: mini, photo: photo };

                if (exif.lat !== null) {
                    sortie.lat = exif.lat;
                    sortie.lon = exif.lon;
                }
                if (exif.heure) {
                    sortie.heure = exif.heure;
                }

                return sortie;
            }


            let photoOuverte = null;
            let jetonPhoto = 0;
            const photosCompletes = {};


            async function retirerPhotoOuverte(evenement) {
                // Ne pas fermer la fenêtre par le clic sur le bouton lui-même.
                evenement.stopPropagation();

                if (!photoOuverte) {
                    return;
                }

                const { sortieId, numero } = photoOuverte;

                if (!confirm("Retirer cette photo de la sortie ?")) {
                    return;
                }

                try {
                    const res = await fetch(
                        `/api/sorties/${sortieId}/photos/${numero}/supprimer`,
                        { method: 'POST', headers: getHeaders() }
                    );

                    if (res.status === 401) {
                        fermerPhoto();
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    Object.keys(photosCompletes).forEach(k => {
                        if (k.startsWith(sortieId + '-')) delete photosCompletes[k];
                    });

                    fermerPhoto();
                    await chargerEtat();

                } catch (e) {
                    alert("Impossible de retirer la photo.");
                }
            }


            async function ouvrirPhoto(sortieId, numero, miniatures) {
                const liste = Array.isArray(miniatures) ? miniatures : [miniatures];
                photoOuverte = { sortieId: sortieId, numero: numero, miniatures: liste };
                document.getElementById('lightbox').style.display = 'flex';
                await afficherPhotoOuverte();
            }

            async function afficherPhotoOuverte() {
                if (!photoOuverte) return;

                const fenetre = document.getElementById('lightbox');
                const image = document.getElementById('lightboxImg');
                const { sortieId, numero, miniatures } = photoOuverte;
                const total = miniatures.length;
                const jeton = ++jetonPhoto;
                const cle = sortieId + '-' + numero;

                document.getElementById('lightboxPrec').style.display = total > 1 ? 'flex' : 'none';
                document.getElementById('lightboxSuiv').style.display = total > 1 ? 'flex' : 'none';
                const compteur = document.getElementById('lightboxCompteur');
                compteur.style.display = total > 1 ? 'block' : 'none';
                compteur.textContent = `${numero + 1} / ${total}`;

                // La photo déjà chargée ou la miniature s'affiche tout de suite.
                image.src = photosCompletes[cle] || miniatures[numero];

                if (photosCompletes[cle]) return;

                try {
                    const res = await fetch(
                        `/api/sorties/${sortieId}/photos/${numero}`,
                        { headers: getHeaders() }
                    );

                    if (res.ok) {
                        const data = await res.json();

                        if (data.image) {
                            photosCompletes[cle] = data.image;

                            if (jeton === jetonPhoto && fenetre.style.display === 'flex') {
                                image.src = data.image;
                            }
                        }
                    }
                } catch (e) {
                    console.error(e);
                }
            }

            function changerPhoto(pas, evenement) {
                if (evenement) evenement.stopPropagation();
                if (!photoOuverte) return;

                const total = photoOuverte.miniatures.length;
                if (total < 2) return;

                photoOuverte.numero = (photoOuverte.numero + pas + total) % total;
                afficherPhotoOuverte();
            }

            function fermerPhoto() {
                photoOuverte = null;
                jetonPhoto++;
                document.getElementById('lightbox').style.display = 'none';
                document.getElementById('lightboxImg').removeAttribute('src');
            }

            (function brancherNavigationPhotos() {
                const prec = document.getElementById('lightboxPrec');
                const suiv = document.getElementById('lightboxSuiv');
                const chevron = (d) => '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="' + d + '"/></svg>';
                prec.innerHTML = chevron('M15 5l-7 7 7 7');
                suiv.innerHTML = chevron('M9 5l7 7-7 7');
                prec.addEventListener('click', (e) => changerPhoto(-1, e));
                suiv.addEventListener('click', (e) => changerPhoto(1, e));

                document.addEventListener('keydown', (e) => {
                    if (!photoOuverte) return;
                    if (e.key === 'ArrowLeft') changerPhoto(-1);
                    else if (e.key === 'ArrowRight') changerPhoto(1);
                    else if (e.key === 'Escape') fermerPhoto();
                });

                // Balayage du doigt : vers la gauche = photo suivante.
                const fenetre = document.getElementById('lightbox');
                let departX = null;
                let departY = null;
                fenetre.addEventListener('touchstart', (e) => {
                    if (e.touches.length !== 1) { departX = null; return; }
                    departX = e.touches[0].clientX;
                    departY = e.touches[0].clientY;
                }, { passive: true });
                fenetre.addEventListener('touchend', (e) => {
                    if (departX === null || !e.changedTouches.length) return;
                    const dx = e.changedTouches[0].clientX - departX;
                    const dy = e.changedTouches[0].clientY - departY;
                    departX = null;
                    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) {
                        changerPhoto(dx < 0 ? 1 : -1);
                        // Le balayage ne doit pas fermer la fenêtre.
                        fenetre.dataset.balayage = String(Date.now());
                    }
                });
            })();


            async function telechargerSauvegarde() {
                try {
                    const res = await fetch('/api/sauvegarde', {
                        headers: getHeaders()
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    const blob = await res.blob();
                    const lien = document.createElement('a');
                    const adresse = URL.createObjectURL(blob);

                    lien.href = adresse;
                    lien.download =
                        `sauvegarde_velo_${new Date().toISOString().slice(0, 10)}.json`;

                    document.body.appendChild(lien);
                    lien.click();
                    lien.remove();

                    setTimeout(() => URL.revokeObjectURL(adresse), 2000);

                } catch (e) {
                    alert("Sauvegarde impossible.");
                }
            }


            const MAX_FICHIERS_GPX = 30;


            // Affiche des lignes de texte dans un élément (sans HTML : sûr).
            function afficherLignes(element, lignes) {
                element.replaceChildren();

                lignes.forEach(texte => {
                    const ligne = document.createElement('div');
                    ligne.textContent = texte;
                    element.appendChild(ligne);
                });
            }


            function nombre(n, singulier, pluriel) {
                return `${n} ${n > 1 ? pluriel : singulier}`;
            }


            function texteErreur(data, defaut) {
                return data && typeof data.detail === 'string'
                    ? data.detail
                    : defaut;
            }


            // Envoie un fichier GPX au serveur et rend le statut et la réponse.
            async function envoyerSortie(gpx, photos) {
                return envoyerCharge(
                    { gpx: gpx, photos: photos },
                    {
                        terrain: document.getElementById('gpxTerrain').value,
                        notes: document.getElementById('gpxNotes').value.trim() || null
                    }
                );
            }


            // charge : { gpx } ou { fit } (+ nom, photos) ; reglages : { terrain, notes }
            async function envoyerCharge(charge, reglages) {
                const res = await fetch('/api/sorties/import', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify(Object.assign({}, charge, {
                        terrain: reglages.terrain,
                        notes: reglages.notes
                    }))
                });

                let data = null;

                try {
                    data = await res.json();
                } catch (e) {
                    data = null;
                }

                return { status: res.status, data: data };
            }


            async function importerGpx() {
                const fichiersGpx = Array.from(document.getElementById('gpxFile').files);
                const fichiersPhotos = document.getElementById('gpxPhotos').files;
                const message = document.getElementById('gpxMessage');
                const bouton = document.getElementById('gpxBouton');

                if (fichiersGpx.length === 0) {
                    afficherLignes(message, ["Choisissez d'abord un fichier GPX."]);
                    return;
                }

                if (fichiersGpx.length > MAX_FICHIERS_GPX) {
                    afficherLignes(message, [
                        `${MAX_FICHIERS_GPX} fichiers maximum à la fois.`
                    ]);
                    return;
                }

                if (fichiersGpx.length > 1 && fichiersPhotos.length > 0) {
                    afficherLignes(message, [
                        "Les photos se joignent à une seule sortie à la fois : " +
                        "retirez-les ou importez les sorties une par une."
                    ]);
                    return;
                }

                // Le bouton est bloqué pendant l'import : pas de double envoi.
                bouton.disabled = true;

                try {
                    if (fichiersGpx.length === 1) {
                        await importerUneSortie(fichiersGpx[0], fichiersPhotos, message);
                    } else {
                        await importerPlusieursSorties(fichiersGpx, message);
                    }
                } finally {
                    bouton.disabled = false;
                }
            }


            async function importerUneSortie(fichierGpx, fichiersPhotos, message) {
                afficherLignes(message, ['Import en cours...']);

                try {
                    const gpx = await fichierGpx.text();
                    const photosList = [];

                    if (fichiersPhotos.length > MAX_PHOTOS) {
                        afficherLignes(message, [
                            `${MAX_PHOTOS} photos maximum par sortie.`
                        ]);
                        return;
                    }

                    for (let i = 0; i < fichiersPhotos.length; i++) {
                        afficherLignes(message, [
                            `Préparation des photos (${i + 1}/${fichiersPhotos.length})...`
                        ]);

                        try {
                            photosList.push(await preparerPhoto(fichiersPhotos[i]));
                        } catch (e) {
                            afficherLignes(message, [
                                `La photo « ${fichiersPhotos[i].name} » ne peut pas être lue ` +
                                `(format non pris en charge). Retirez-la ou choisissez un JPEG.`
                            ]);
                            return;
                        }
                    }

                    afficherLignes(message, ['Import en cours...']);

                    const r = await envoyerSortie(gpx, photosList);

                    if (r.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (r.status < 200 || r.status >= 300) {
                        throw new Error(texteErreur(r.data, "Fichier refusé."));
                    }

                    const data = r.data;

                    afficherLignes(message, [
                        `✅ Sortie importée : ${fmtDistance(data.distance_km).complet}, ` +
                        `${unites.altitude === 'm' ? String(data.denivele_pos) + ' m' : fmtAltitude(data.denivele_pos).complet} D+. ` +
                        (data.ajoute_au_total
                            ? `Kilométrage total : ${data.km_total} km.`
                            : "Cette sortie date d'avant votre dernière mise à jour " +
                              "du kilométrage : elle n'a pas été ajoutée au total.")
                    ]);

                    document.getElementById('gpxFile').value = '';
                    document.getElementById('gpxPhotos').value = '';
                    document.getElementById('gpxNotes').value = '';

                    await chargerEtat();

                } catch (e) {
                    afficherLignes(message, [e.message || "Import impossible."]);
                }
            }


            async function importerPlusieursSorties(fichiersGpx, message) {
                const elements = fichiersGpx.map(fichier => ({
                    nom: fichier.name,
                    charger: async () => ({ gpx: await fichier.text() })
                }));

                await importerLot(
                    elements,
                    message,
                    {
                        terrain: document.getElementById('gpxTerrain').value,
                        notes: document.getElementById('gpxNotes').value.trim() || null
                    },
                    'fichiers',
                    () => {
                        document.getElementById('gpxFile').value = '';
                        document.getElementById('gpxNotes').value = '';
                    }
                );
            }


            // elements : [{ nom, charger: async () => charge }]
            async function importerLot(elements, message, reglages, mot, terminer) {
                const bilan = {
                    traitees: 0,
                    importees: 0,
                    ajoutees: 0,
                    anterieures: 0,
                    deja: 0,
                    refusees: [],
                    kmTotal: null,
                    interrompu: null
                };

                for (let i = 0; i < elements.length; i++) {
                    const element = elements[i];

                    afficherLignes(message, [
                        `Import de la sortie ${i + 1}/${elements.length}...`
                    ]);

                    let charge;

                    try {
                        charge = await element.charger();
                    } catch (e) {
                        bilan.traitees++;
                        bilan.refusees.push(`${element.nom} : fichier illisible`);
                        continue;
                    }

                    let r;

                    try {
                        r = await envoyerCharge(charge, reglages);
                    } catch (e) {
                        bilan.interrompu = "connexion perdue";
                        break;
                    }

                    if (r.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        bilan.interrompu = "code d'accès à saisir de nouveau";
                        break;
                    }

                    if (r.status === 503) {
                        bilan.interrompu = "code d'accès non réglé sur le serveur";
                        break;
                    }

                    bilan.traitees++;

                    if (r.status === 409) {
                        bilan.deja++;
                    } else if (r.status >= 200 && r.status < 300) {
                        bilan.importees++;
                        bilan.kmTotal = r.data.km_total;

                        if (r.data.ajoute_au_total) {
                            bilan.ajoutees++;
                        } else {
                            bilan.anterieures++;
                        }
                    } else {
                        const raison = r.status >= 500
                            ? 'erreur du serveur'
                            : texteErreur(r.data, 'fichier refusé');

                        bilan.refusees.push(`${element.nom} : ${raison}`);
                    }
                }

                const lignes = [];

                if (bilan.importees > 0) {
                    let ligne =
                        `✅ ${nombre(bilan.importees, 'sortie importée', 'sorties importées')}.`;

                    if (bilan.ajoutees > 0) {
                        ligne +=
                            ` ${nombre(bilan.ajoutees, 'ajoutée', 'ajoutées')} ` +
                            `au kilométrage total (${bilan.kmTotal} km).`;
                    }

                    if (bilan.anterieures > 0) {
                        ligne +=
                            ` ${nombre(bilan.anterieures, 'antérieure', 'antérieures')} ` +
                            `à votre dernière mise à jour du kilométrage, ` +
                            `donc déjà comptée${bilan.anterieures > 1 ? 's' : ''}.`;
                    }

                    lignes.push(ligne);
                }

                if (bilan.deja > 0) {
                    lignes.push(
                        `ℹ️ ${nombre(bilan.deja, 'sortie déjà présente', 'sorties déjà présentes')} ` +
                        `(ignorée${bilan.deja > 1 ? 's' : ''}).`
                    );
                }

                if (bilan.refusees.length > 0) {
                    lignes.push(
                        `⚠️ ${nombre(bilan.refusees.length, 'sortie refusée', 'sorties refusées')} :`
                    );
                    bilan.refusees.forEach(texte => lignes.push(`• ${texte}`));
                }

                if (bilan.interrompu) {
                    lignes.push(
                        `⛔ Import interrompu (${bilan.interrompu}) après ` +
                        `${bilan.traitees}/${elements.length} ${mot}. ` +
                        `Vous pouvez relancer : les sorties déjà importées seront ignorées.`
                    );
                }

                if (lignes.length === 0) {
                    lignes.push('Aucune sortie importée.');
                }

                afficherLignes(message, lignes);

                // En cas d'interruption, la sélection est gardée pour relancer.
                if (!bilan.interrompu && terminer) {
                    terminer();
                }

                await chargerEtat();
            }


            async function changerTerrain(id, terrain, menu, ancien) {
                menu.disabled = true;

                try {
                    const res = await fetch(`/api/sorties/${id}/terrain`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ terrain: terrain })
                    });

                    if (res.status === 401) {
                        menu.value = ancien;
                        menu.disabled = false;
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    // Recharge tout : l'usure des pièces change avec le terrain.
                    await chargerEtat();

                } catch (e) {
                    menu.value = ancien;
                    menu.disabled = false;
                    alert("Changement de terrain impossible.");
                }
            }


            async function ajouterPhotos(id, dejaLa, champ, bouton) {
                const fichiers = Array.from(champ.files || []);
                champ.value = '';

                if (fichiers.length === 0) {
                    return;
                }

                if (dejaLa + fichiers.length > MAX_PHOTOS) {
                    alert(
                        `${MAX_PHOTOS} photos maximum par sortie ` +
                        `(il y en a déjà ${dejaLa}).`
                    );
                    return;
                }

                const texteBouton = bouton.textContent;
                bouton.disabled = true;

                try {
                    const photos = [];

                    for (let i = 0; i < fichiers.length; i++) {
                        bouton.textContent =
                            `Préparation ${i + 1}/${fichiers.length}...`;
                        photos.push(await preparerPhoto(fichiers[i]));
                    }

                    bouton.textContent = 'Envoi...';

                    const res = await fetch(`/api/sorties/${id}/photos`, {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ photos: photos })
                    });

                    if (res.status === 401) {
                        bouton.textContent = texteBouton;
                        bouton.disabled = false;
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) {
                        throw new Error();
                    }

                    await chargerEtat();

                } catch (e) {
                    bouton.textContent = texteBouton;
                    bouton.disabled = false;
                    alert("Ajout des photos impossible (photo illisible ou trop lourde ?).");
                }
            }


            // ============================================================
            // Import de l'archive Strava (fichier .zip lu dans le navigateur)
            // ============================================================

            const MAX_SORTIES_STRAVA = 1000;
            const TAILLE_MAX_ACTIVITE = 8 * 1024 * 1024;
            const TAILLE_MAX_CSV = 80 * 1024 * 1024;
            let archiveStrava = null;


            async function lireOctets(fichier, debut, fin) {
                return new Uint8Array(await fichier.slice(debut, fin).arrayBuffer());
            }


            async function decompresser(source, format) {
                const blob = source instanceof Blob ? source : new Blob([source]);
                const flux = blob.stream().pipeThrough(new DecompressionStream(format));
                return new Uint8Array(await new Response(flux).arrayBuffer());
            }


            // Lit la liste des fichiers d'un zip sans le charger en entier.
            async function lireEntreesZip(fichier) {
                const tailleQueue = Math.min(fichier.size, 65535 + 22 + 20);
                const queue = await lireOctets(
                    fichier, fichier.size - tailleQueue, fichier.size
                );
                const vq = new DataView(queue.buffer);
                let fin = -1;

                for (let i = queue.length - 22; i >= 0; i--) {
                    if (vq.getUint32(i, true) === 0x06054b50) {
                        fin = i;
                        break;
                    }
                }

                if (fin < 0) {
                    throw new Error('zip');
                }

                let total = vq.getUint16(fin + 10, true);
                let tailleCd = vq.getUint32(fin + 12, true);
                let debutCd = vq.getUint32(fin + 16, true);

                if (total === 0xFFFF || tailleCd === 0xFFFFFFFF || debutCd === 0xFFFFFFFF) {
                    const loc = fin - 20;

                    if (loc < 0 || vq.getUint32(loc, true) !== 0x07064b50) {
                        throw new Error('zip');
                    }

                    const decalage64 = Number(vq.getBigUint64(loc + 8, true));
                    const e64 = await lireOctets(fichier, decalage64, decalage64 + 56);
                    const v64 = new DataView(e64.buffer);

                    if (v64.getUint32(0, true) !== 0x06064b50) {
                        throw new Error('zip');
                    }

                    total = Number(v64.getBigUint64(32, true));
                    tailleCd = Number(v64.getBigUint64(40, true));
                    debutCd = Number(v64.getBigUint64(48, true));
                }

                const cd = await lireOctets(fichier, debutCd, debutCd + tailleCd);
                const vc = new DataView(cd.buffer);
                const decodeur = new TextDecoder('utf-8');
                const entrees = new Map();
                let p = 0;

                while (p + 46 <= cd.length && vc.getUint32(p, true) === 0x02014b50) {
                    const drapeaux = vc.getUint16(p + 8, true);
                    const methode = vc.getUint16(p + 10, true);
                    let tailleComp = vc.getUint32(p + 20, true);
                    let tailleReelle = vc.getUint32(p + 24, true);
                    const lNom = vc.getUint16(p + 28, true);
                    const lExtra = vc.getUint16(p + 30, true);
                    const lCom = vc.getUint16(p + 32, true);
                    let decalage = vc.getUint32(p + 42, true);
                    const nom = decodeur.decode(cd.subarray(p + 46, p + 46 + lNom));

                    let q = p + 46 + lNom;
                    const finExtra = q + lExtra;

                    while (q + 4 <= finExtra) {
                        const id = vc.getUint16(q, true);
                        const longueur = vc.getUint16(q + 2, true);

                        if (id === 1) {
                            let r = q + 4;

                            if (tailleReelle === 0xFFFFFFFF) {
                                tailleReelle = Number(vc.getBigUint64(r, true));
                                r += 8;
                            }

                            if (tailleComp === 0xFFFFFFFF) {
                                tailleComp = Number(vc.getBigUint64(r, true));
                                r += 8;
                            }

                            if (decalage === 0xFFFFFFFF) {
                                decalage = Number(vc.getBigUint64(r, true));
                            }
                        }

                        q += 4 + longueur;
                    }

                    entrees.set(nom, {
                        nom: nom,
                        methode: methode,
                        tailleComp: tailleComp,
                        tailleReelle: tailleReelle,
                        decalage: decalage,
                        chiffre: (drapeaux & 1) === 1
                    });

                    p += 46 + lNom + lExtra + lCom;
                }

                return entrees;
            }


            async function lireEntreeZip(fichier, entree, maximum) {
                if (entree.chiffre || entree.tailleReelle > maximum) {
                    throw new Error('entree');
                }

                const entete = await lireOctets(
                    fichier, entree.decalage, entree.decalage + 30
                );
                const ve = new DataView(entete.buffer);

                if (ve.getUint32(0, true) !== 0x04034b50) {
                    throw new Error('zip');
                }

                const debut =
                    entree.decalage + 30 +
                    ve.getUint16(26, true) + ve.getUint16(28, true);
                const blob = fichier.slice(debut, debut + entree.tailleComp);
                let octets;

                if (entree.methode === 0) {
                    octets = new Uint8Array(await blob.arrayBuffer());
                } else if (entree.methode === 8) {
                    octets = await decompresser(blob, 'deflate-raw');
                } else {
                    throw new Error('methode');
                }

                if (octets.length > maximum) {
                    throw new Error('entree');
                }

                return octets;
            }


            // Lit un fichier CSV (champs entre guillemets, retours à la ligne inclus).
            function lireCsv(texte) {
                const NL = String.fromCharCode(10);
                const RC = String.fromCharCode(13);

                if (texte.charCodeAt(0) === 0xFEFF) {
                    texte = texte.slice(1);
                }

                const premiere = texte.slice(0, 2000).split(NL)[0];
                const separateur =
                    premiere.split(';').length > premiere.split(',').length ? ';' : ',';

                const lignes = [];
                let ligne = [];
                let champ = '';
                let guillemets = false;

                for (let i = 0; i < texte.length; i++) {
                    const c = texte[i];

                    if (guillemets) {
                        if (c === '"') {
                            if (texte[i + 1] === '"') {
                                champ += '"';
                                i++;
                            } else {
                                guillemets = false;
                            }
                        } else {
                            champ += c;
                        }
                    } else if (c === '"') {
                        guillemets = true;
                    } else if (c === separateur) {
                        ligne.push(champ);
                        champ = '';
                    } else if (c === NL || c === RC) {
                        if (c === RC && texte[i + 1] === NL) {
                            i++;
                        }

                        ligne.push(champ);
                        champ = '';

                        if (ligne.length > 1 || ligne[0] !== '') {
                            lignes.push(ligne);
                        }

                        ligne = [];
                    } else {
                        champ += c;
                    }
                }

                if (champ !== '' || ligne.length > 0) {
                    ligne.push(champ);
                    lignes.push(ligne);
                }

                return lignes;
            }


            function ressembleActivite(valeur) {
                const v = valeur.trim().toLowerCase();

                return ['.gpx', '.fit', '.tcx', '.gpx.gz', '.fit.gz', '.tcx.gz']
                    .some(fin => v.endsWith(fin));
            }


            function trouverEntree(entrees, chemin) {
                if (entrees.has(chemin)) {
                    return entrees.get(chemin);
                }

                const fin = '/' + chemin;

                for (const [nom, entree] of entrees) {
                    if (nom.endsWith(fin)) {
                        return entree;
                    }
                }

                return null;
            }


            // Repère les colonnes (même si l'export est dans une autre langue)
            // et rend la liste des activités dont le fichier est dans l'archive.
            function lireActivitesStrava(lignes, entrees) {
                if (lignes.length < 2) {
                    return [];
                }

                const entete = lignes[0].map(t => t.toLowerCase());
                const rangees = lignes.slice(1);
                let colFichier = -1;
                let meilleur = 0;

                for (let j = 0; j < entete.length; j++) {
                    let n = 0;

                    for (const r of rangees) {
                        if (ressembleActivite(r[j] || '')) {
                            n++;
                        }
                    }

                    if (n > meilleur) {
                        meilleur = n;
                        colFichier = j;
                    }
                }

                if (colFichier < 0) {
                    return [];
                }

                const trouverCol = (mots, exclusions, defaut) => {
                    for (let j = 0; j < entete.length; j++) {
                        if (
                            mots.some(m => entete[j].includes(m)) &&
                            !exclusions.some(m => entete[j].includes(m))
                        ) {
                            return j;
                        }
                    }

                    return defaut;
                };

                const colType = trouverCol(['type'], [], 3);
                const colVelo = trouverCol(
                    ['gear', 'équipement', 'equipement', 'ausrüstung', 'equipo',
                     'attrezzatura', 'matériel', 'materiel'], [], 11
                );
                const colNom = trouverCol(
                    ['name', 'nom'], ['file', 'fichier', 'dateiname'], 2
                );

                const activites = [];

                for (const r of rangees) {
                    const chemin = (r[colFichier] || '').trim();

                    if (!ressembleActivite(chemin)) {
                        continue;
                    }

                    const entree = trouverEntree(entrees, chemin);

                    if (!entree) {
                        continue;
                    }

                    activites.push({
                        nom: (r[colNom] || '').trim(),
                        type: (r[colType] || '').trim(),
                        velo: (r[colVelo] || '').trim(),
                        chemin: chemin,
                        entree: entree
                    });
                }

                return activites;
            }


            const SANS_VELO = '__sans_velo__';
            const TOUT = '__tout__';


            function remplirChoix(select, compteurs, libelleTout, libelleVide, preferee) {
                select.replaceChildren();

                const ajouter = (valeur, texte) => {
                    const option = document.createElement('option');
                    option.value = valeur;
                    option.textContent = texte;
                    select.appendChild(option);
                };

                const noms = Array.from(compteurs.keys())
                    .filter(nom => nom !== '')
                    .sort((a, b) => compteurs.get(b) - compteurs.get(a));

                noms.forEach(nom => ajouter(nom, `${nom} (${compteurs.get(nom)})`));

                if (compteurs.has('')) {
                    ajouter(SANS_VELO, `${libelleVide} (${compteurs.get('')})`);
                }

                ajouter(TOUT, libelleTout);

                const voulue = preferee ? noms.find(preferee) : null;
                select.value = voulue || (noms.length > 0 ? noms[0] : TOUT);
            }


            function compter(activites, champ) {
                const compteurs = new Map();

                activites.forEach(a => {
                    compteurs.set(a[champ], (compteurs.get(a[champ]) || 0) + 1);
                });

                return compteurs;
            }


            function selectionStrava() {
                if (!archiveStrava) {
                    return [];
                }

                const velo = document.getElementById('stravaVelo').value;
                const type = document.getElementById('stravaType').value;

                return archiveStrava.activites.filter(a => {
                    const veloOk =
                        velo === TOUT ||
                        (velo === SANS_VELO ? a.velo === '' : a.velo === velo);
                    const typeOk =
                        type === TOUT ||
                        (type === SANS_VELO ? a.type === '' : a.type === type);

                    return veloOk && typeOk;
                });
            }


            function majCompteStrava() {
                const choisies = selectionStrava();
                const total = archiveStrava ? archiveStrava.activites.length : 0;
                const bouton = document.getElementById('stravaBouton');
                let texte =
                    `${nombre(choisies.length, 'sortie sera importée', 'sorties seront importées')} ` +
                    `(sur ${total} dans l'archive).`;

                if (choisies.length > MAX_SORTIES_STRAVA) {
                    texte +=
                        ` Seules les ${MAX_SORTIES_STRAVA} dernières seront importées ; ` +
                        `relancez ensuite pour les suivantes.`;
                }

                document.getElementById('stravaCompte').textContent = texte;
                bouton.disabled = choisies.length === 0;
            }


            async function chargerArchiveStrava() {
                const message = document.getElementById('stravaMessage');
                const choix = document.getElementById('stravaChoix');
                const fichier = document.getElementById('stravaZip').files[0];

                archiveStrava = null;
                choix.style.display = 'none';
                afficherLignes(message, []);

                if (!fichier) {
                    return;
                }

                if (typeof DecompressionStream === 'undefined') {
                    afficherLignes(message, [
                        "Ce navigateur est trop ancien pour lire l'archive. " +
                        "Utilisez une version récente de Chrome, Safari ou Firefox."
                    ]);
                    return;
                }

                afficherLignes(message, ["Lecture de l'archive..."]);

                try {
                    const entrees = await lireEntreesZip(fichier);
                    let csv = null;

                    for (const [nom, entree] of entrees) {
                        if (nom.toLowerCase().endsWith('activities.csv')) {
                            csv = entree;
                            break;
                        }
                    }

                    if (!csv) {
                        afficherLignes(message, [
                            "Ce fichier ne ressemble pas à une archive Strava " +
                            "(activities.csv introuvable)."
                        ]);
                        return;
                    }

                    const octets = await lireEntreeZip(fichier, csv, TAILLE_MAX_CSV);
                    const lignes = lireCsv(new TextDecoder('utf-8').decode(octets));
                    const activites = lireActivitesStrava(lignes, entrees);

                    if (activites.length === 0) {
                        afficherLignes(message, [
                            "Aucune sortie avec fichier GPS trouvée dans cette archive."
                        ]);
                        return;
                    }

                    archiveStrava = { fichier: fichier, activites: activites };

                    remplirChoix(
                        document.getElementById('stravaVelo'),
                        compter(activites, 'velo'),
                        'Tous les vélos et sorties',
                        'Sans vélo associé',
                        nom => nom.toLowerCase().includes('cube')
                    );

                    remplirChoix(
                        document.getElementById('stravaType'),
                        compter(activites, 'type'),
                        "Tous les types d'activité",
                        'Sans type',
                        null
                    );

                    // Par défaut : tous les types (le vélo choisi suffit à filtrer).
                    document.getElementById('stravaType').value = TOUT;

                    majCompteStrava();
                    afficherLignes(message, []);
                    choix.style.display = 'block';

                } catch (e) {
                    afficherLignes(message, [
                        "Impossible de lire ce fichier : est-ce bien l'archive .zip de Strava ?"
                    ]);
                }
            }


            function versBase64(octets) {
                let texte = '';

                for (let i = 0; i < octets.length; i += 0x8000) {
                    texte += String.fromCharCode.apply(
                        null, octets.subarray(i, i + 0x8000)
                    );
                }

                return btoa(texte);
            }


            async function chargerActiviteStrava(activite) {
                let octets = await lireEntreeZip(
                    archiveStrava.fichier, activite.entree, TAILLE_MAX_ACTIVITE
                );
                let nom = activite.chemin.toLowerCase();

                if (octets.length > 2 && octets[0] === 0x1f && octets[1] === 0x8b) {
                    octets = await decompresser(octets, 'gzip');
                }

                if (nom.endsWith('.gz')) {
                    nom = nom.slice(0, -3);
                }

                const charge = { nom: activite.nom ? activite.nom.slice(0, 120) : null };

                if (nom.endsWith('.fit')) {
                    charge.fit = versBase64(octets);
                } else {
                    charge.gpx = new TextDecoder('utf-8').decode(octets);
                }

                return charge;
            }


            async function importerStrava() {
                const message = document.getElementById('stravaMessage');
                const bouton = document.getElementById('stravaBouton');
                let choisies = selectionStrava();

                if (choisies.length === 0) {
                    afficherLignes(message, ["Aucune sortie à importer avec ces choix."]);
                    return;
                }

                if (choisies.length > MAX_SORTIES_STRAVA) {
                    choisies = choisies.slice(choisies.length - MAX_SORTIES_STRAVA);
                }

                const elements = choisies.map(activite => ({
                    nom: activite.nom || activite.chemin,
                    charger: () => chargerActiviteStrava(activite)
                }));

                bouton.disabled = true;
                document.getElementById('stravaZip').disabled = true;

                try {
                    await importerLot(
                        elements,
                        message,
                        {
                            terrain: document.getElementById('stravaTerrain').value,
                            notes: null
                        },
                        'sorties',
                        () => {
                            document.getElementById('stravaZip').value = '';
                            archiveStrava = null;
                            document.getElementById('stravaChoix').style.display = 'none';
                        }
                    );
                } finally {
                    document.getElementById('stravaZip').disabled = false;
                    bouton.disabled = archiveStrava === null;
                }
            }


            document.getElementById('stravaZip')
                .addEventListener('change', chargerArchiveStrava);
            document.getElementById('stravaVelo')
                .addEventListener('change', majCompteStrava);
            document.getElementById('stravaType')
                .addEventListener('change', majCompteStrava);


            async function confirmerVeloSortie(sortie) {
                try {
                    const res = await fetch('/api/sorties/' + sortie.id + '/verifier', {
                        method: 'POST',
                        headers: getHeaders()
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Confirmation impossible.');
                    await rechargerDonneesVelo();
                } catch (e) {
                    alert(e.message || 'Confirmation impossible.');
                }
            }

            async function changerVeloSortie(sortie) {
                const autres = velosDisponibles.filter(v => Number(v.id) !== veloActifId);
                if (!autres.length) return;
                const saut = String.fromCharCode(10);
                const choix = prompt(
                    'Choisissez le vélo de destination en saisissant son numéro :' + saut +
                    autres.map((v, i) => (i + 1) + '. ' + v.nom).join(saut)
                );
                if (choix === null) return;
                const index = Number.parseInt(choix, 10) - 1;
                if (!Number.isInteger(index) || index < 0 || index >= autres.length) {
                    alert('Numéro de vélo non valide.');
                    return;
                }
                const cible = autres[index];
                if (!confirm('Affecter « ' + sortie.nom + ' » à « ' + cible.nom + ' » ? Le kilométrage et l’usure seront recalculés.')) return;
                try {
                    const res = await fetch('/api/sorties/' + sortie.id + '/velo', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ nouveau_velo_id: Number(cible.id) })
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Changement de vélo impossible.');
                    await rechargerDonneesVelo();
                } catch (e) {
                    alert(e.message || 'Changement de vélo impossible.');
                }
            }


            async function supprimerSortie(id) {
                if (!confirm("Supprimer cette sortie ?")) {
                    return;
                }

                try {
                    const res = await fetch(`/api/sorties/${id}/supprimer`, {
                        method: 'POST',
                        headers: getHeaders()
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) throw new Error();
                    await chargerEtat();

                } catch (e) {
                    alert("Suppression impossible.");
                }
            }


            // Un champ de coefficient par élément d'entretien (pièces, puis vérifications).
            function afficherCoefsPieces(composants) {
                const zone = document.getElementById('coefsPiecesListe');
                zone.textContent = '';

                const pieces = composants
                    .filter(c => !c.verification)
                    .sort((a, b) => (a.numero || 0) - (b.numero || 0));
                const verifs = composants.filter(c => c.verification);

                const groupe = (titre, liste) => {
                    if (liste.length === 0) return;

                    const bloc = document.createElement('div');
                    bloc.className = 'mr-groupe';
                    const h = document.createElement('h3');
                    h.textContent = titre;
                    bloc.appendChild(h);

                    liste.forEach(c => {
                        const ligne = document.createElement('div');
                        ligne.className = 'mr-ligne';

                        const nom = document.createElement('span');
                        nom.textContent = c.numero ? `${c.numero}. ${c.nom}` : c.nom;
                        ligne.appendChild(nom);

                        const champ = document.createElement('input');
                        champ.type = 'text';
                        champ.inputMode = 'decimal';
                        champ.maxLength = 6;
                        champ.dataset.coefId = String(c.id);
                        champ.setAttribute('aria-label', `Coefficient d'usure : ${c.nom}`);
                        champ.value = String(c.coef_usure === undefined ? 1 : c.coef_usure)
                            .replace('.', ',');
                        ligne.appendChild(champ);

                        bloc.appendChild(ligne);
                    });

                    zone.appendChild(bloc);
                };

                groupe('Pièces et entretiens', pieces);
                groupe('Vérifications', verifs);
            }

            async function enregistrerCoefsPieces() {
                const message = document.getElementById('coefsPiecesMessage');
                const valeurs = {};
                let erreur = false;

                document.querySelectorAll('#coefsPiecesListe input[data-coef-id]').forEach(champ => {
                    const v = parseFloat(champ.value.replace(',', '.'));

                    if (isNaN(v) || v < 0.2 || v > 5) {
                        erreur = true;
                        champ.style.borderColor = 'var(--danger)';
                    } else {
                        champ.style.borderColor = '';
                        valeurs[champ.dataset.coefId] = v;
                    }
                });

                if (erreur) {
                    message.textContent = 'Chaque coefficient doit être un nombre entre 0,2 et 5.';
                    return;
                }

                try {
                    const res = await fetch('/api/composants/coefficients', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify({ valeurs: valeurs })
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) throw new Error();

                    message.textContent = '✅ Coefficients enregistrés.';
                    await chargerEtat();

                } catch (e) {
                    message.textContent = 'Enregistrement impossible.';
                }
            }


            const CHAMPS_REGLAGES = {
                terrain_route: 'regRoute',
                terrain_mixte: 'regMixte',
                terrain_chemin: 'regChemin',
                terrain_sentier: 'regSentier',
                poids_denivele: 'regDenivele'
            };


            async function chargerReglages() {
                try {
                    const res = await fetch('/api/reglages', { headers: getHeaders() });
                    if (!res.ok) return;

                    const reglages = await res.json();

                    Object.entries(CHAMPS_REGLAGES).forEach(([cle, id]) => {
                        const champ = document.getElementById(id);
                        if (champ && reglages[cle] !== undefined) {
                            champ.value = reglages[cle];
                        }
                    });

                } catch (e) {
                    console.error(e);
                }
            }


            async function enregistrerReglages() {
                const message = document.getElementById('reglagesMessage');
                const corps = {};

                for (const [cle, id] of Object.entries(CHAMPS_REGLAGES)) {
                    const valeur = parseFloat(
                        document.getElementById(id).value.replace(',', '.')
                    );

                    if (isNaN(valeur)) {
                        message.textContent = "Vérifiez les nombres saisis.";
                        return;
                    }

                    corps[cle] = valeur;
                }

                try {
                    const res = await fetch('/api/reglages', {
                        method: 'POST',
                        headers: getHeaders(),
                        body: JSON.stringify(corps)
                    });

                    if (res.status === 401) {
                        document.getElementById('authScreen').style.display = 'flex';
                        return;
                    }

                    if (!res.ok) throw new Error();

                    message.textContent = '✅ Réglages enregistrés.';
                    await chargerEtat();

                } catch (e) {
                    message.textContent = "Enregistrement impossible.";
                }
            }
        </script>
    </body>
    </html>
    """

    return html_content


# ============================================================
# API : REGISTRE DES VÉLOS
# ============================================================

@app.get("/api/velos")
def obtenir_velos(x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT velo_id, COUNT(*) FROM sorties
            WHERE a_verifier = 1 GROUP BY velo_id
        """)
        a_verifier = {r[0]: r[1] for r in cursor.fetchall()}
        cursor.execute("""
            SELECT id, nom, type, marque, modele, annee, taille, actif,
                   km_total, km_frac, km_maj_le, date_achat, moteur, batterie, freins, suspension, tubeless, transmission,
                   CASE WHEN photo IS NULL OR photo = '' THEN 0 ELSE 1 END AS a_photo
            FROM velos
            WHERE actif = 1
            ORDER BY id
        """)
        return [
            {
                "sorties_a_verifier": a_verifier.get(row["id"], 0),
                "a_photo": row["a_photo"],
                "id": row["id"],
                "nom": row["nom"],
                "type": row["type"],
                "marque": row["marque"],
                "modele": row["modele"],
                "annee": row["annee"],
                "taille": row["taille"],
                "actif": row["actif"],
                "km_total": row["km_total"],
                "km_frac": row["km_frac"],
                "km_maj_le": row["km_maj_le"],
                "date_achat": row["date_achat"],
                "moteur": row["moteur"],
                "batterie": row["batterie"],
                "freins": row["freins"],
                "suspension": row["suspension"],
                "tubeless": row["tubeless"],
                "transmission": row["transmission"]
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


class NouveauVeloRequest(BaseModel):
    nom: str = Field(min_length=1, max_length=80)
    type: str = Field(min_length=1, max_length=40)
    marque: Optional[str] = Field(default=None, max_length=80)
    modele: Optional[str] = Field(default=None, max_length=120)
    annee: Optional[int] = Field(default=None, ge=1950, le=2100)
    taille: Optional[str] = Field(default=None, max_length=30)
    date_achat: Optional[str] = Field(default=None, max_length=10)
    moteur: Optional[str] = Field(default=None, max_length=120)
    batterie: Optional[str] = Field(default=None, max_length=120)
    freins: Optional[str] = Field(default="inconnu", max_length=20)
    suspension: Optional[str] = Field(default="inconnu", max_length=20)
    tubeless: Optional[str] = Field(default="inconnu", max_length=20)
    transmission: Optional[str] = Field(default="inconnu", max_length=20)


class ModifierFicheVeloRequest(BaseModel):
    nom: str = Field(min_length=1, max_length=80)
    marque: Optional[str] = Field(default=None, max_length=80)
    modele: Optional[str] = Field(default=None, max_length=120)
    annee: Optional[int] = Field(default=None, ge=1950, le=2100)
    taille: Optional[str] = Field(default=None, max_length=30)
    date_achat: Optional[str] = Field(default=None, max_length=10)
    moteur: Optional[str] = Field(default=None, max_length=120)
    batterie: Optional[str] = Field(default=None, max_length=120)
    freins: Optional[str] = Field(default="inconnu", max_length=20)
    suspension: Optional[str] = Field(default="inconnu", max_length=20)
    tubeless: Optional[str] = Field(default="inconnu", max_length=20)
    transmission: Optional[str] = Field(default="inconnu", max_length=20)


def adapter_freins_velo(cursor, velo_id, freins, suspension=None, tubeless=None, transmission=None):
    """Adapte les noms des pièces de frein sans supprimer leurs identifiants ni historique."""
    changements = {}
    if freins == "patins":
        changements = {
            "Plaquettes de frein (avant)": "Patins de frein (avant)",
            "Plaquettes de frein (arrière)": "Patins de frein (arrière)",
            "Disque de frein (avant)": "Non concerné — disque de frein (avant)",
            "Disque de frein (arrière)": "Non concerné — disque de frein (arrière)",
            "Purge du liquide de frein": "Non concerné — purge du liquide de frein",
            "Freins - contrôle général (type à confirmer)": "Non concerné — Freins - contrôle général (type à confirmer)",
        }
    elif freins in ("disque", "disque_hydraulique", "disque_mecanique"):
        changements = {
            "Patins de frein (avant)": "Plaquettes de frein (avant)",
            "Patins de frein (arrière)": "Plaquettes de frein (arrière)",
            "Non concerné — disque de frein (avant)": "Disque de frein (avant)",
            "Non concerné — disque de frein (arrière)": "Disque de frein (arrière)",
            "Freins - contrôle général (type à confirmer)": "Non concerné — Freins - contrôle général (type à confirmer)",
        }
        if freins == "disque_hydraulique":
            changements["Non concerné — purge du liquide de frein"] = "Purge du liquide de frein"
        else:
            changements["Purge du liquide de frein"] = "Non concerné — purge du liquide de frein"
    if suspension == "rigide":
        changements.update({
            "Fourche - entretien (50 h)": "Non concerné — Fourche - entretien (50 h)",
            "Fourche - révision complète (200 h)": "Non concerné — Fourche - révision complète (200 h)",
            "Fourche - pression et SAG (contrôle)": "Non concerné — Fourche - pression et SAG (contrôle)",
            "Amortisseur - entretien (50 h)": "Non concerné — Amortisseur - entretien (50 h)",
            "Amortisseur - révision complète (200 h)": "Non concerné — Amortisseur - révision complète (200 h)",
            "Amortisseur - pression et SAG (contrôle)": "Non concerné — Amortisseur - pression et SAG (contrôle)",
            "Roulements de suspension (biellettes)": "Non concerné — Roulements de suspension (biellettes)",
        })
    elif suspension == "avant":
        changements.update({
            "Non concerné — Fourche - entretien (50 h)": "Fourche - entretien (50 h)",
            "Non concerné — Fourche - révision complète (200 h)": "Fourche - révision complète (200 h)",
            "Non concerné — Fourche - pression et SAG (contrôle)": "Fourche - pression et SAG (contrôle)",
            "Amortisseur - entretien (50 h)": "Non concerné — Amortisseur - entretien (50 h)",
            "Amortisseur - révision complète (200 h)": "Non concerné — Amortisseur - révision complète (200 h)",
            "Amortisseur - pression et SAG (contrôle)": "Non concerné — Amortisseur - pression et SAG (contrôle)",
            "Roulements de suspension (biellettes)": "Non concerné — Roulements de suspension (biellettes)",
        })
    elif suspension == "double":
        changements.update({
            "Non concerné — Fourche - entretien (50 h)": "Fourche - entretien (50 h)",
            "Non concerné — Fourche - révision complète (200 h)": "Fourche - révision complète (200 h)",
            "Non concerné — Fourche - pression et SAG (contrôle)": "Fourche - pression et SAG (contrôle)",
            "Non concerné — Amortisseur - entretien (50 h)": "Amortisseur - entretien (50 h)",
            "Non concerné — Amortisseur - révision complète (200 h)": "Amortisseur - révision complète (200 h)",
            "Non concerné — Amortisseur - pression et SAG (contrôle)": "Amortisseur - pression et SAG (contrôle)",
            "Non concerné — Roulements de suspension (biellettes)": "Roulements de suspension (biellettes)",
        })
    if tubeless == "chambre":
        changements["Liquide préventif tubeless (2 pneus)"] = "Non concerné — Liquide préventif tubeless (2 pneus)"
    elif tubeless == "tubeless":
        changements["Non concerné — Liquide préventif tubeless (2 pneus)"] = "Liquide préventif tubeless (2 pneus)"
    noms_transmission = (
        "Chaîne (remplacement)", "Nettoyage & Lubrification chaîne",
        "Cassette (remplacement)", "Plateau (remplacement)", "Galets de dérailleur",
        "Câble et gaine de dérailleur"
    )
    if transmission == "courroie":
        for nom in noms_transmission:
            changements[nom] = "Non concerné — " + nom
        changements["Non concerné — Courroie (contrôle et remplacement)"] = "Courroie (contrôle et remplacement)"
    elif transmission == "chaine":
        for nom in noms_transmission:
            changements["Non concerné — " + nom] = nom
        changements["Courroie (contrôle et remplacement)"] = "Non concerné — Courroie (contrôle et remplacement)"
    for ancien, nouveau in changements.items():
        cursor.execute(
            "UPDATE composants SET nom = ? WHERE velo_id = ? AND nom = ?",
            (nouveau, velo_id, ancien)
        )
    cursor.execute("SELECT nom FROM composants WHERE velo_id = ?", (velo_id,))
    noms = {r[0] for r in cursor.fetchall()}
    cursor.execute("SELECT type FROM velos WHERE id = ?", (velo_id,))
    ligne = cursor.fetchone()
    type_velo = ligne[0] if ligne else "vtt"
    liste = entretiens_du_type(type_velo, freins, suspension, tubeless, transmission)
    if freins in ("patins", "disque", "disque_hydraulique", "disque_mecanique"):
        a_ajouter = [ligne for ligne in liste if "frein" in ligne[0].lower() or "patin" in ligne[0].lower()]
    else:
        a_ajouter = [ligne for ligne in liste if "freins - contrôle général" in ligne[0].lower()]
    if suspension in ("rigide", "avant", "double"):
        a_ajouter.extend(ligne for ligne in liste if any(mot in ligne[0].lower() for mot in ("fourche -", "amortisseur -", "roulements de suspension")))
    if tubeless in ("chambre", "tubeless"):
        a_ajouter.extend(ligne for ligne in liste if "liquide préventif tubeless" in ligne[0].lower())
    if transmission in ("chaine", "courroie"):
        a_ajouter.extend(ligne for ligne in liste if any(mot in ligne[0].lower() for mot in ("chaîne", "cassette", "plateau", "galets", "courroie", "câble et gaine de dérailleur")))
    for nom, km, jours, categorie, x, y, verification in a_ajouter:
        if nom not in noms:
            cursor.execute("""
                INSERT INTO composants (
                    velo_id, nom, km_depart, seuil_alerte, seuil_jours,
                    categorie, pos_x, pos_y, verification, coef_usure
                ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, 1)
            """, (velo_id, nom, km, jours, categorie, x, y, verification))
            noms.add(nom)


@app.put("/api/velos/{velo_id}")
def modifier_fiche_velo(
    velo_id: int,
    data: ModifierFicheVeloRequest,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    nom = " ".join(data.nom.split())
    if not nom:
        raise HTTPException(status_code=400, detail="Donnez un nom au vélo.")
    date_achat = data.date_achat.strip() if data.date_achat else None
    if date_achat:
        try:
            date.fromisoformat(date_achat)
        except ValueError:
            raise HTTPException(status_code=400, detail="Date d'achat non valide.")

    conn = db.connect()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT type FROM velos WHERE id = ? AND actif = 1",
            (velo_id,)
        )
        velo = cursor.fetchone()
        if velo is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")
        electrique = velo[0] in {"vtt_electrique", "gravel_electrique", "route_electrique", "electrique"}
        cursor.execute("""
            UPDATE velos
            SET nom = ?, marque = ?, modele = ?, annee = ?, taille = ?,
                date_achat = ?, moteur = ?, batterie = ?, freins = COALESCE(?, freins),
                suspension = COALESCE(?, suspension), tubeless = COALESCE(?, tubeless),
                transmission = COALESCE(?, transmission)
            WHERE id = ? AND actif = 1
        """, (
            nom, (data.marque or "").strip() or None,
            (data.modele or "").strip() or None, data.annee,
            (data.taille or "").strip() or None, date_achat,
            ((data.moteur or "").strip() or None) if electrique else None,
            ((data.batterie or "").strip() or None) if electrique else None,
            data.freins if data.freins in ("disque", "disque_hydraulique", "disque_mecanique", "patins") else None,
            data.suspension if data.suspension in ("rigide", "avant", "double") else None,
            data.tubeless if data.tubeless in ("chambre", "tubeless") else None,
            data.transmission if data.transmission in ("chaine", "courroie") else None,
            velo_id
        ))
        adapter_freins_velo(
            cursor, velo_id,
            data.freins if data.freins in ("disque", "disque_hydraulique", "disque_mecanique", "patins") else None,
            data.suspension if data.suspension in ("rigide", "avant", "double") else None,
            data.tubeless if data.tubeless in ("chambre", "tubeless") else None,
            data.transmission if data.transmission in ("chaine", "courroie") else None
        )
        conn.commit()
        return {"statut": "success", "velo_id": velo_id, "nom": nom}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/velos")
def creer_velo(
    data: NouveauVeloRequest,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    types_autorises = {
        "vtt_electrique", "vtt", "enduro", "descente",
        "gravel", "gravel_electrique", "route", "route_electrique"
    }
    type_velo = data.type.strip().lower()
    nom = " ".join(data.nom.split())
    if type_velo not in types_autorises:
        raise HTTPException(status_code=400, detail="Type de vélo inconnu.")
    if not nom:
        raise HTTPException(status_code=400, detail="Donnez un nom au vélo.")
    date_achat = data.date_achat.strip() if data.date_achat else None
    if date_achat:
        try:
            date.fromisoformat(date_achat)
        except ValueError:
            raise HTTPException(status_code=400, detail="Date d'achat non valide.")

    electrique = type_velo in {"vtt_electrique", "gravel_electrique", "route_electrique"}
    conn = db.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO velos (
                nom, type, marque, modele, annee, taille, actif, km_total,
                km_frac, km_maj_le, date_achat, suivi_depuis, moteur, batterie, freins, suspension, tubeless, transmission
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 0, 0, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            nom, type_velo, (data.marque or "").strip() or None,
            (data.modele or "").strip() or None, data.annee,
            (data.taille or "").strip() or None,
            date_achat or date.today().isoformat(),
            date.today().isoformat(),
            ((data.moteur or "").strip() or None) if electrique else None,
            ((data.batterie or "").strip() or None) if electrique else None,
            data.freins if data.freins in ("disque", "disque_hydraulique", "disque_mecanique", "patins") else None,
            data.suspension if data.suspension in ("rigide", "avant", "double") else None,
            data.tubeless if data.tubeless in ("chambre", "tubeless") else None,
            data.transmission if data.transmission in ("chaine", "courroie") else None
        ))
        cursor.execute("SELECT id FROM velos WHERE nom = ? ORDER BY id DESC LIMIT 1", (nom,))
        velo_id = cursor.fetchone()[0]

        cursor.executemany("""
            INSERT INTO composants (
                velo_id, nom, km_depart, seuil_alerte, seuil_jours,
                categorie, pos_x, pos_y, verification, coef_usure
            )
            VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, 1)
        """, [
            (velo_id, nom_comp, km, jours, categorie, x, y, verification)
            for nom_comp, km, jours, categorie, x, y, verification
            in entretiens_du_type(type_velo, data.freins, data.suspension, data.tubeless, data.transmission)
        ])
        # Ce vélo a déjà sa liste complète : le démarrage ne la retouchera pas.
        poser_marqueur_entretiens(cursor, velo_id)
        conn.commit()
        return {"statut": "success", "velo_id": velo_id, "nom": nom, "type": type_velo}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/velos/{velo_id}/contenu")
def contenu_velo(velo_id: int, x_code_acces: Optional[str] = Header(default=None)):
    """Ce qui serait effacé avec ce vélo (affiché avant la confirmation)."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM velos WHERE id = ?", (velo_id,))
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")
        return compter_contenu_velo(cursor, velo_id)
    finally:
        conn.close()


def compter_contenu_velo(cursor, velo_id):
    cursor.execute("SELECT COUNT(*) FROM composants WHERE velo_id = ?", (velo_id,))
    composants = cursor.fetchone()[0]
    cursor.execute(
        "SELECT COUNT(*) FROM historique WHERE velo_id = ? "
        "OR composant_id IN (SELECT id FROM composants WHERE velo_id = ?)",
        (velo_id, velo_id)
    )
    historique = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM sorties WHERE velo_id = ?", (velo_id,))
    sorties = cursor.fetchone()[0]
    return {"composants": composants, "historique": historique, "sorties": sorties}


@app.post("/api/velos/{velo_id}/supprimer")
def supprimer_velo(velo_id: int, x_code_acces: Optional[str] = Header(default=None)):
    """Supprime pour de bon un vélo, ses pièces, son historique et ses sorties.
    Le vélo principal (CUBE, n° 1) ne peut jamais être supprimé."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, legacy_velo_id FROM velos WHERE id = ?", (velo_id,))
        velo = cursor.fetchone()
        if velo is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")
        if velo[0] == 1 or velo[1] is not None:
            raise HTTPException(
                status_code=400,
                detail="Le vélo principal (CUBE) ne peut pas être supprimé."
            )

        efface = compter_contenu_velo(cursor, velo_id)
        # Dans cet ordre : l'historique dépend des pièces, les pièces dépendent du vélo.
        cursor.execute(
            "DELETE FROM historique WHERE velo_id = ? "
            "OR composant_id IN (SELECT id FROM composants WHERE velo_id = ?)",
            (velo_id, velo_id)
        )
        cursor.execute("DELETE FROM composants WHERE velo_id = ?", (velo_id,))
        cursor.execute("DELETE FROM sorties WHERE velo_id = ?", (velo_id,))
        cursor.execute("DELETE FROM mes_reglages_velo WHERE velo_id = ?", (velo_id,))
        cursor.execute("DELETE FROM velos WHERE id = ?", (velo_id,))
        conn.commit()
        return {"statut": "success", "velo_id": velo_id, "efface": efface}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class PhotoVeloRequest(BaseModel):
    photo: str = Field(min_length=30, max_length=2500000)


PHOTO_DEBUTS = ("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")


@app.get("/api/velos/{velo_id}/photo")
def lire_photo_velo(velo_id: int, x_code_acces: Optional[str] = Header(default=None)):
    """Photo enregistrée du vélo (null s'il n'y en a pas)."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT photo FROM velos WHERE id = ?", (velo_id,))
        ligne = cursor.fetchone()
        if ligne is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")
        return {"photo": ligne[0] or None}
    finally:
        conn.close()


@app.put("/api/velos/{velo_id}/photo")
def enregistrer_photo_velo(velo_id: int, data: PhotoVeloRequest, x_code_acces: Optional[str] = Header(default=None)):
    """Enregistre (ou remplace) la photo du vélo, déjà réduite par le téléphone."""
    verifier_auth(x_code_acces)
    photo = data.photo.strip()
    debut = next((d for d in PHOTO_DEBUTS if photo.startswith(d)), None)
    if debut is None:
        raise HTTPException(status_code=400, detail="Format de photo non reconnu (JPEG, PNG ou WebP).")
    try:
        base64.b64decode(photo[len(debut):], validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="La photo est illisible.")
    conn = db.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM velos WHERE id = ?", (velo_id,))
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")
        cursor.execute("UPDATE velos SET photo = ? WHERE id = ?", (photo, velo_id))
        conn.commit()
        return {"statut": "success", "velo_id": velo_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.delete("/api/velos/{velo_id}/photo")
def supprimer_photo_velo(velo_id: int, x_code_acces: Optional[str] = Header(default=None)):
    """Retire la photo enregistrée (le CUBE retrouve alors sa photo d'origine)."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM velos WHERE id = ?", (velo_id,))
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")
        cursor.execute("UPDATE velos SET photo = NULL WHERE id = ?", (velo_id,))
        conn.commit()
        return {"statut": "success", "velo_id": velo_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class PositionRepere(BaseModel):
    composant_id: int
    pos_x: int = Field(ge=0, le=100)
    pos_y: int = Field(ge=0, le=100)


class PositionsRequest(BaseModel):
    positions: List[PositionRepere] = Field(min_length=1, max_length=300)


@app.post("/api/velos/{velo_id}/positions")
def enregistrer_positions_velo(velo_id: int, data: PositionsRequest, x_code_acces: Optional[str] = Header(default=None)):
    """Déplace d'un coup les repères (pastilles) d'un vélo : tout ou rien."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM velos WHERE id = ?", (velo_id,))
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")
        for position in data.positions:
            cursor.execute(
                "SELECT id FROM composants WHERE id = ? AND velo_id = ?",
                (position.composant_id, velo_id)
            )
            if cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail="Une pièce n'appartient pas à ce vélo.")
        for position in data.positions:
            cursor.execute(
                "UPDATE composants SET pos_x = ?, pos_y = ? WHERE id = ? AND velo_id = ?",
                (position.pos_x, position.pos_y, position.composant_id, velo_id)
            )
        conn.commit()
        return {"statut": "success", "deplaces": len(data.positions)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class LienPhotoRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2000)


LIEN_TAILLE_MAX = 6000000
LIEN_DELAI = 10
LIEN_PORTS = (None, 80, 443)
LIEN_TYPES_IMAGE = ("image/jpeg", "image/png", "image/webp")
LIEN_NAVIGATEUR = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Mobile Safari/537.36"
)


def adresse_publique(hote):
    """Vrai seulement si le nom de domaine mène uniquement à des adresses publiques d'Internet
    (jamais le réseau interne du serveur)."""
    try:
        infos = socket.getaddrinfo(hote, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def telecharger_lien(url, accepter):
    """Télécharge un lien (4 redirections au plus, adresse revérifiée à chaque fois).
    Retourne (type, octets, adresse finale)."""
    ouvreur = urllib.request.build_opener(_SansRedirection)
    for _ in range(5):
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            raise ValueError("Ce lien n'est pas valide.")
        if p.port not in LIEN_PORTS:
            raise ValueError("Ce lien n'est pas autorisé.")
        if not adresse_publique(p.hostname):
            raise ValueError("Ce lien n'est pas autorisé.")
        demande = urllib.request.Request(url, headers={
            "User-Agent": LIEN_NAVIGATEUR,
            "Accept": accepter,
            "Accept-Language": "fr-FR,fr;q=0.9",
        })
        try:
            reponse = ouvreur.open(demande, timeout=LIEN_DELAI)
        except urllib.error.HTTPError as e:
            suite = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308) and suite:
                url = urllib.parse.urljoin(url, suite)
                continue
            raise ValueError("Le site a refusé la demande (code %d)." % e.code)
        except (urllib.error.URLError, OSError, ValueError):
            raise ValueError("Le site ne répond pas.")
        with reponse:
            type_contenu = (reponse.headers.get_content_type() or "").lower()
            octets = reponse.read(LIEN_TAILLE_MAX + 1)
        if len(octets) > LIEN_TAILLE_MAX:
            raise ValueError("Le fichier est trop gros.")
        return type_contenu, octets, url
    raise ValueError("Le lien fait trop de détours.")


def images_de_la_page(html, base):
    """Adresses des images « principales » d'une page (comme l'aperçu d'un lien) :
    og:image, twitter:image, image_src, puis données structurées (JSON-LD)."""
    trouvees = []

    def ajouter(valeur):
        valeur = (valeur or "").strip().replace("&amp;", "&")
        if valeur and not valeur.startswith("data:"):
            adresse = urllib.parse.urljoin(base, valeur)
            if adresse not in trouvees:
                trouvees.append(adresse)

    noms_meta = ("og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src")
    for balise in re.findall(r"<meta\b[^>]*>", html, re.I):
        nom = re.search(r"""(?:property|name)\s*=\s*["']([^"']+)["']""", balise, re.I)
        valeur = re.search(r"""content\s*=\s*["']([^"']+)["']""", balise, re.I)
        if nom and valeur and nom.group(1).strip().lower() in noms_meta:
            ajouter(valeur.group(1))
    for balise in re.findall(r"<link\b[^>]*>", html, re.I):
        rel = re.search(r"""rel\s*=\s*["']image_src["']""", balise, re.I)
        lien = re.search(r"""href\s*=\s*["']([^"']+)["']""", balise, re.I)
        if rel and lien:
            ajouter(lien.group(1))

    def parcourir(noeud):
        if isinstance(noeud, dict):
            for cle, valeur in noeud.items():
                if cle == "image":
                    elements = valeur if isinstance(valeur, list) else [valeur]
                    for element in elements:
                        if isinstance(element, str):
                            ajouter(element)
                        elif isinstance(element, dict):
                            ajouter(element.get("url") or element.get("contentUrl"))
                else:
                    parcourir(valeur)
        elif isinstance(noeud, list):
            for element in noeud:
                parcourir(element)

    ouvrant = "<scr" + "ipt"
    fermant = "</scr" + "ipt>"
    modele = ouvrant + r"""[^>]+application/ld\+json[^>]*>(.*?)""" + fermant
    for bloc in re.findall(modele, html, re.I | re.S):
        try:
            parcourir(json.loads(bloc))
        except ValueError:
            pass
    return trouvees[:6]


class RechercheFicheVeloRequest(BaseModel):
    recherche: Optional[str] = Field(default=None, max_length=200)
    url: Optional[str] = Field(default=None, max_length=2000)


def texte_page_fiche(html_page):
    """Extrait du texte lisible sans exécuter le contenu de la page."""
    texte = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", html_page, flags=re.I | re.S)
    texte = re.sub(r"<[^>]+>", " ", texte)
    texte = html_lib.unescape(texte)
    return re.sub(r"\s+", " ", texte)[:30000]


def produits_structures(html_page):
    produits = []
    ouvrant = "<scr" + "ipt"
    fermant = "</scr" + "ipt>"
    modele = re.escape(ouvrant) + r'''[^>]*type=["']application/ld[+]json["'][^>]*>(.*?)''' + re.escape(fermant)
    for bloc in re.findall(modele, html_page, re.I | re.S):
        try:
            noeud = json.loads(bloc)
        except ValueError:
            continue
        a_visiter = noeud if isinstance(noeud, list) else [noeud]
        while a_visiter:
            element = a_visiter.pop(0)
            if isinstance(element, dict):
                type_objet = element.get("@type", [])
                if isinstance(type_objet, str):
                    type_objet = [type_objet]
                if any(str(t).lower() == "product" for t in type_objet):
                    produits.append(element)
                for valeur in element.values():
                    if isinstance(valeur, (dict, list)):
                        a_visiter.append(valeur)
            elif isinstance(element, list):
                a_visiter.extend(element)
    return produits


LIBELLES_COMPOSANTS_VELO = {
    "cadre": ("cadre", ("frame", "cadre")),
    "fourche": ("fourche", ("fork", "fourche")),
    "amortisseur": ("amortisseur", ("shock", "rear shock", "amortisseur", "suspension arriere")),
    "tige_de_selle": ("tige_de_selle", ("seatpost", "seat post", "tige de selle")),
    "freins": ("freins", ("brake", "brakes", "frein", "freins", "brakeset")),
    "plaquettes": ("plaquettes", ("brake pad", "brake pads", "plaquette", "plaquettes")),
    "disques": ("disques", ("brake rotor", "rotor", "disque", "disques")),
    "transmission": ("transmission", ("drivetrain", "transmission")),
    "derailleur": ("derailleur", ("rear derailleur", "derailleur", "dérailleur")),
    "cassette": ("cassette", ("cassette", "sprocket")),
    "chaine": ("chaine", ("chain", "chaine", "chaîne")),
    "pedalier": ("pedalier", ("crankset", "crank", "pedalier", "pédalier")),
    "roues": ("roues", ("wheelset", "wheel", "wheels", "roue", "roues")),
    "pneus": ("pneus", ("tire", "tires", "tyre", "tyres", "pneu", "pneus")),
    "selle": ("selle", ("saddle", "selle")),
    "cintre": ("cintre", ("handlebar", "handlebars", "cintre", "guidon")),
    "potence": ("potence", ("stem", "potence")),
    "moteur": ("moteur", ("motor", "moteur")),
    "batterie": ("batterie", ("battery", "batterie")),
}


def composants_techniques_de_page(html_page):
    """Extrait les couples libellé/valeur des tableaux et propriétés structurées."""
    trouves = {}

    def categorie_pour(libelle):
        cle = re.sub(r"[^a-z0-9]+", " ", html_lib.unescape(str(libelle or "")).lower()).strip()
        for categorie, (nom, synonymes) in LIBELLES_COMPOSANTS_VELO.items():
            for synonyme in synonymes:
                terme = re.sub(r"[^a-z0-9]+", " ", synonyme.lower()).strip()
                if cle == terme or cle.startswith(terme + " ") or cle.endswith(" " + terme):
                    return nom
        return None

    def ajouter(libelle, valeur):
        categorie = categorie_pour(libelle)
        valeur = re.sub(r"\s+", " ", html_lib.unescape(str(valeur or ""))).strip(" :;|")
        valeur = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", valeur)).strip()
        if not categorie or not valeur or len(valeur) > 500:
            return
        if valeur.lower() in ("n/a", "not applicable", "non applicable", "-", "—"):
            return
        trouves.setdefault(categorie, [])
        if valeur not in [item["nom"] for item in trouves[categorie]]:
            trouves[categorie].append({"nom": valeur, "source": "fiche"})

    # Tableaux de spécifications classiques (label dans la première cellule).
    for ligne in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html_page, re.I | re.S):
        cellules = re.findall(r"<(?:th|td)\b[^>]*>(.*?)</(?:th|td)>", ligne, re.I | re.S)
        if len(cellules) >= 2:
            ajouter(cellules[0], cellules[1])

    # Données structurées Schema.org : additionalProperty et propriétés produit.
    def parcourir(noeud):
        if isinstance(noeud, dict):
            nom = noeud.get("name") or noeud.get("propertyID")
            valeur = noeud.get("value") or noeud.get("description")
            if nom and valeur and not isinstance(valeur, (dict, list)):
                ajouter(nom, valeur)
            for cle, element in noeud.items():
                if cle in ("additionalProperty", "properties", "specifications", "specs"):
                    parcourir(element)
                elif isinstance(element, (dict, list)):
                    parcourir(element)
        elif isinstance(noeud, list):
            for element in noeud:
                parcourir(element)

    ouvrant = "<scr" + "ipt"
    fermant = "</scr" + "ipt>"
    modele = re.escape(ouvrant) + r'''[^>]*type=["']application/ld[+]json["'][^>]*>(.*?)''' + re.escape(fermant)
    for bloc in re.findall(modele, html_page, re.I | re.S):
        try:
            parcourir(json.loads(bloc))
        except (ValueError, TypeError):
            continue

    resultat = []
    for categorie, valeurs in trouves.items():
        statut = "trouve" if len(valeurs) == 1 else "a_confirmer"
        for valeur in valeurs:
            resultat.append({
                "categorie": categorie,
                "nom": valeur["nom"],
                "statut": statut,
                "origine": "Fiche consultée"
            })

    categories_connues = [item[0] for item in LIBELLES_COMPOSANTS_VELO.values()]
    presentes = {item["categorie"] for item in resultat}
    for categorie in categories_connues:
        if categorie not in presentes:
            resultat.append({
                "categorie": categorie,
                "nom": None,
                "statut": "non_trouve",
                "origine": None
            })
    return resultat


@app.post("/api/recherche-fiche-velo")
def recherche_fiche_velo(data: RechercheFicheVeloRequest, x_code_acces: Optional[str] = Header(default=None)):
    """Recherche une page de vélo ou extrait les caractéristiques publiées par une fiche."""
    verifier_auth(x_code_acces)
    recherche = (data.recherche or "").strip()
    lien = (data.url or "").strip()
    if not lien and not recherche:
        raise HTTPException(status_code=400, detail="Saisis un modèle ou un lien constructeur.")
    if not lien:
        requete = urllib.parse.urlencode({"q": recherche + " vélo fiche technique constructeur"})
        try:
            type_contenu, octets, finale = telecharger_lien("https://html.duckduckgo.com/html/?" + requete, "text/html")
        except ValueError as e:
            raise HTTPException(status_code=502, detail="La recherche web n'a pas abouti. Tu peux coller directement le lien constructeur. " + str(e))
        page = octets.decode("utf-8", errors="replace")
        candidats = []
        for balise in re.findall(r"<a\b[^>]*class=[\"'][^\"']*result__a[^\"']*[\"'][^>]*>.*?</a>", page, re.I | re.S):
            href = re.search(r"href=[\"']([^\"']+)[\"']", balise, re.I)
            titre = texte_page_fiche(balise)
            if not href or not titre:
                continue
            url_candidate = html_lib.unescape(href.group(1))
            parsed = urllib.parse.urlparse(url_candidate)
            if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
                cible = urllib.parse.parse_qs(parsed.query).get("uddg", [None])[0]
                if cible:
                    url_candidate = cible
            if urllib.parse.urlparse(url_candidate).scheme in ("http", "https") and all(c["url"] != url_candidate for c in candidats):
                candidats.append({"titre": titre[:180], "url": url_candidate})
            if len(candidats) >= 5:
                break
        if not candidats:
            raise HTTPException(status_code=404, detail="Aucune fiche trouvée. Essaie avec le nom exact ou colle le lien constructeur.")
        return {"candidats": candidats}

    if "://" not in lien:
        lien = "https://" + lien
    try:
        type_contenu, octets, finale = telecharger_lien(lien, "text/html,application/xhtml+xml")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if type_contenu not in ("text/html", "application/xhtml+xml"):
        raise HTTPException(status_code=400, detail="Le lien ne mène pas à une page web lisible.")
    page = octets[:2000000].decode("utf-8", errors="replace")
    texte = texte_page_fiche(page)
    produits = produits_structures(page)
    produit = produits[0] if produits else {}
    composants_techniques = composants_techniques_de_page(page)
    marque = produit.get("brand")
    if isinstance(marque, dict):
        marque = marque.get("name")
    modele = produit.get("model") or produit.get("name")
    annee_source = produit.get("releaseDate") or produit.get("datePublished") or ""
    annee_match = re.search(r"\b(19[5-9][0-9]|20[0-9]{2}|2100)\b", str(annee_source) + " " + str(modele or ""))
    bas = texte.lower()
    freins = None
    patins_trouves = bool(re.search(r"freins? (?:à|a) patins|freinage sur jante|rim brakes?|v-brakes?", bas))
    disques_trouves = bool(re.search(r"freins? (?:à|a) disques?|freinage à disque|disc brakes?", bas))
    hydrauliques_trouves = bool(re.search(r"freins? (?:à|a) disques? hydrauliques?|freinage hydraulique", bas))
    mecaniques_trouves = bool(re.search(r"freins? (?:à|a) disques? mécaniques?|mechanical disc brakes?", bas))
    if patins_trouves and not disques_trouves:
        freins = "patins"
    elif disques_trouves and not patins_trouves:
        if hydrauliques_trouves and not mecaniques_trouves:
            freins = "disque_hydraulique"
        elif mecaniques_trouves and not hydrauliques_trouves:
            freins = "disque_mecanique"
        else:
            freins = "disque"
    type_velo = None
    if re.search(r"\bvtt\b", bas) and re.search(r"électrique|electrique|e-bike", bas):
        type_velo = "vtt_electrique"
    elif re.search(r"\bgravel\b", bas) and re.search(r"électrique|electrique|e-bike", bas):
        type_velo = "gravel_electrique"
    elif re.search(r"\bgravel\b", bas):
        type_velo = "gravel"
    elif re.search(r"\broute\b", bas) and re.search(r"électrique|electrique|e-bike", bas):
        type_velo = "route_electrique"
    elif re.search(r"\broute\b|\broad bike\b", bas):
        type_velo = "route"
    elif re.search(r"\benduro\b", bas):
        type_velo = "enduro"
    elif re.search(r"\bdescente\b|\bdownhill\b", bas):
        type_velo = "descente"
    elif re.search(r"\bvtt\b|\bmountain bike\b", bas):
        type_velo = "vtt"
    moteur_match = re.search(r"(?:Bosch\s+(?:Performance Line|Performance|CX|SX|PX)[^.;,]{0,35}|Shimano\s+EP[0-9]{1,2}[^.;,]{0,25}|Yamaha\s+(?:PW|PW-X)[^.;,]{0,25}|Brose[^.;,]{0,25})", texte, re.I)
    batterie_match = re.search(r"(?:Bosch\s+Power(?:Tube|Pack)[^.;,]{0,30}|PowerTube\s*[0-9]{2,4}\s?Wh|[0-9]{2,4}\s?Wh(?:\s+battery|\s+batterie)?)", texte, re.I)
    suspension_detectee = None
    if re.search(r"full suspension|suspension intégrale|double suspension|tout suspendu", bas):
        suspension_detectee = "double"
    elif re.search(r"hardtail|semi-rigide|semi rigide|fourche suspendue|front suspension", bas):
        suspension_detectee = "avant"
    elif re.search(r"fourche rigide|rigid fork|sans suspension", bas):
        suspension_detectee = "rigide"
    tubeless_detecte = None
    if re.search(r"montage tubeless|pneus? tubeless|tubeless setup|tubeless tyres|tubeless tires|tubeless installé|sans chambre à air", bas) and not re.search(r"tubeless ready|compatible tubeless", bas):
        tubeless_detecte = "tubeless"
    elif re.search(r"chambre à air|chambre a air|inner tube", bas):
        tubeless_detecte = "chambre"
    transmission_detectee = None
    if re.search(r"courroie|belt drive", bas):
        transmission_detectee = "courroie"
    elif re.search(r"chaîne|chaine|chain drive", bas):
        transmission_detectee = "chaine"
    return {
        "marque": str(marque)[:80] if marque else None,
        "modele": str(modele)[:120] if modele else None,
        "annee": int(annee_match.group(1)) if annee_match else None,
        "type": type_velo,
        "freins": freins,
        "suspension": suspension_detectee,
        "tubeless": tubeless_detecte,
        "transmission": transmission_detectee,
        "moteur": moteur_match.group(0).strip()[:120] if moteur_match else None,
        "batterie": batterie_match.group(0).strip()[:120] if batterie_match else None,
        "composants_techniques": composants_techniques,
        "source": finale,
        "message": "Les données extraites doivent être vérifiées sur la fiche constructeur."
    }


@app.post("/api/photo-depuis-lien")
def photo_depuis_lien(data: LienPhotoRequest, x_code_acces: Optional[str] = Header(default=None)):
    """Va chercher la photo d'un vélo à partir du lien d'une fiche (Décathlon, Opticycles…)
    ou d'une image. Ne modifie rien : renvoie la photo, que la page réduit puis enregistre."""
    verifier_auth(x_code_acces)
    lien = data.url.strip()
    if "://" not in lien:
        lien = "https://" + lien
    accepte = "text/html,application/xhtml+xml,image/webp,image/jpeg,image/png;q=0.9,*/*;q=0.5"
    try:
        type_contenu, octets, finale = telecharger_lien(lien, accepte)
        image = None
        if type_contenu in LIEN_TYPES_IMAGE:
            image = (type_contenu, octets, finale)
        elif type_contenu in ("text/html", "application/xhtml+xml"):
            html = octets[:2000000].decode("utf-8", errors="replace")
            for candidate in images_de_la_page(html, finale):
                try:
                    t, o, f = telecharger_lien(candidate, "image/webp,image/jpeg,image/png,*/*;q=0.5")
                except ValueError:
                    continue
                if t in LIEN_TYPES_IMAGE and len(o) > 5000:
                    image = (t, o, f)
                    break
        else:
            raise ValueError("Ce lien ne mène ni à une page ni à une image.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if image is None:
        raise HTTPException(
            status_code=404,
            detail="Aucune photo trouvée sur cette page. Choisis plutôt une photo de ton téléphone."
        )
    t, o, f = image
    return {"photo": "data:" + t + ";base64," + base64.b64encode(o).decode("ascii"), "source": f}


# ============================================================
# API : ÉTAT DU VÉLO ET POURCENTAGES D'USURE
# ============================================================

@app.get("/api/etat")
def obtenir_etat(
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT id, km_total, date_achat, legacy_velo_id, suivi_depuis
            FROM velos
            WHERE id = ? AND actif = 1
        """, (velo_id,))
        velo = cursor.fetchone()

        if velo is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")

        km_global = velo["km_total"] if velo["km_total"] is not None else 0
        date_achat = velo["date_achat"] or (
            DATE_ACHAT_PAR_DEFAUT
            if velo["legacy_velo_id"] == 1
            else date.today().isoformat()
        )
        # Point de départ du compteur de jours : le jour où le vélo a été ajouté
        # dans Maillons s'il est connu, sinon la date d'achat (CUBE, anciens vélos).
        date_depart_suivi = velo["suivi_depuis"] or date_achat

        cursor.execute(
            "SELECT * FROM composants WHERE velo_id = ? ORDER BY id",
            (velo_id,)
        )
        rows = cursor.fetchall()
        cursor.execute("SELECT freins, suspension, tubeless, transmission FROM velos WHERE id = ?", (velo_id,))
        equipement = cursor.fetchone()
        freins_velo = equipement["freins"] if equipement else None
        suspension_velo = equipement["suspension"] if equipement else None
        tubeless_velo = equipement["tubeless"] if equipement else None
        transmission_velo = equipement["transmission"] if equipement else None
        if velo_id != 1:
            rows = [r for r in rows if not str(r["nom"]).startswith("Non concerné")]
            if freins_velo == "patins":
                rows = [r for r in rows if not any(mot in str(r["nom"]).lower() for mot in ("plaquettes de frein", "disque de frein", "purge du liquide de frein"))]
            elif freins_velo in ("disque", "disque_hydraulique", "disque_mecanique"):
                rows = [r for r in rows if "patins de frein" not in str(r["nom"]).lower()]
                if freins_velo != "disque_hydraulique":
                    rows = [r for r in rows if "purge du liquide de frein" not in str(r["nom"]).lower()]
            if suspension_velo == "rigide":
                rows = [r for r in rows if not any(mot in str(r["nom"]).lower() for mot in ("fourche -", "amortisseur -", "roulements de suspension"))]
            elif suspension_velo == "avant":
                rows = [r for r in rows if not any(mot in str(r["nom"]).lower() for mot in ("amortisseur -", "roulements de suspension"))]
            if tubeless_velo == "chambre":
                rows = [r for r in rows if "liquide préventif tubeless" not in str(r["nom"]).lower()]
            if transmission_velo == "courroie":
                rows = [r for r in rows if not any(mot in str(r["nom"]).lower() for mot in ("chaîne", "cassette", "plateau", "galets", "câble et gaine de dérailleur"))]
            elif transmission_velo == "chaine":
                rows = [r for r in rows if "courroie (contrôle et remplacement)" not in str(r["nom"]).lower()]


        reglages = lire_reglages(cursor)
        aujourdhui = date.today()

        cursor.execute("""
            SELECT debut, distance_km, denivele_pos, denivele_neg, terrain,
                   trace, surfaces
            FROM sorties
            WHERE velo_id = ?
        """, (velo_id,))
        sorties = cursor.fetchall()
        coefs_sorties = [
            coef_terrain_sortie(s[5], s[6], s[4] or "mixte", reglages)[0]
            for s in sorties
        ]

        cursor.execute("""
            SELECT h.composant_id, MAX(h.date_remplacement)
            FROM historique h
            JOIN composants c ON c.id = h.composant_id
            WHERE h.velo_id = ? AND c.velo_id = ?
            GROUP BY h.composant_id
        """, (velo_id, velo_id))
        dernier_entretien = {r[0]: r[1] for r in cursor.fetchall()}

        resultat = []

        for row in rows:
            km_depart = row["km_depart"] or 0
            seuil = row["seuil_alerte"] or 0

            km = max(0, km_global - km_depart)

            famille = famille_de(row["nom"])
            depuis = dernier_entretien.get(row["id"])
            surplus = 0.0

            for s, coef_s in zip(sorties, coefs_sorties):
                if depuis is None or s[0][:10] >= depuis:
                    facteur = facteur_usure(
                        famille, s[1], s[2], s[3], s[4] or "mixte", reglages,
                        coef_s
                    )
                    surplus += float(s[1] or 0) * (facteur - 1)

            coef_piece = row["coef_usure"] if row["coef_usure"] else 1.0

            km_usure = max(0.0, km + surplus) * coef_piece

            pourcentage_km = (
                round((km_usure / seuil) * 100)
                if seuil > 0
                else 0
            )

            seuil_jours = row["seuil_jours"] or 0
            jours_ecoules = None
            pourcentage_jours = 0

            if seuil_jours > 0:
                try:
                    reference = date.fromisoformat((depuis or date_depart_suivi)[:10])
                except ValueError:
                    reference = date.fromisoformat(DATE_ACHAT_PAR_DEFAUT)

                jours_ecoules = max(0, (aujourdhui - reference).days)
                pourcentage_jours = round(
                    jours_ecoules * coef_piece / seuil_jours * 100
                )

            pourcentage = max(pourcentage_km, pourcentage_jours)

            est_verification = bool(row["verification"])
            numero = None

            if not est_verification:
                numero = sum(1 for r in resultat if not r["verification"]) + 1

            resultat.append({
                "id": row["id"],
                "numero": numero,
                "verification": est_verification,
                "nom": row["nom"],
                "km_actuel": km,
                "km_usure": round(km_usure, 1),
                "km_depart": km_depart,
                "seuil_alerte": seuil,
                "pourcentage_usure": pourcentage,
                "seuil_jours": seuil_jours,
                "jours_ecoules": jours_ecoules,
                "jours_usure": (
                    round(jours_ecoules * coef_piece)
                    if jours_ecoules is not None
                    else None
                ),
                "coef_usure": round(coef_piece, 2),
                "alerte_entretien": pourcentage >= 100,
                "categorie": row["categorie"],
                "pos_x": row["pos_x"],
                "pos_y": row["pos_y"]
            })

        # Liste des priorités : la pièce la plus usée en haut.
        # Le numéro (repère sur le schéma du vélo) reste fixe.
        resultat.sort(key=lambda r: (-r["pourcentage_usure"], r["id"]))

        return {
            "km_total": km_global,
            "composants": resultat
        }

    finally:
        conn.close()


# ============================================================
# API : HISTORIQUE DES INTERVENTIONS
# ============================================================

@app.get("/api/historique")
def obtenir_historique(
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT
                h.id,
                h.composant_id,
                c.nom AS composant_nom,
                h.date_remplacement,
                h.cout,
                h.notes,
                h.km_intervention
            FROM historique h
            JOIN composants c ON h.composant_id = c.id
            WHERE h.velo_id = ? AND c.velo_id = ?
            ORDER BY h.id DESC
        """, (velo_id, velo_id))

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    finally:
        conn.close()


@app.post("/api/historique")
def ajouter_historique(
    data: HistoriqueRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM velos WHERE id = ? AND actif = 1",
            (velo_id,)
        )
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")

        cursor.execute(
            "SELECT id FROM composants WHERE id = ? AND velo_id = ?",
            (data.composant_id, velo_id)
        )
        if cursor.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail="Composant introuvable pour ce vélo."
            )

        cursor.execute("SELECT km_total FROM velos WHERE id = ?", (velo_id,))
        velo = cursor.fetchone()
        km_total = velo[0] if velo and velo[0] is not None else 2700

        if data.km_intervention > km_total:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Le kilométrage de l'intervention ne peut pas "
                    "dépasser celui du vélo."
                )
            )

        cursor.execute(
            "SELECT km_depart FROM composants WHERE id = ? AND velo_id = ?",
            (data.composant_id, velo_id)
        )
        ligne_composant = cursor.fetchone()
        km_avant = (ligne_composant[0] or 0) if ligne_composant else 0

        cursor.execute("""
            INSERT INTO historique (
                velo_id,
                composant_id,
                date_remplacement,
                cout,
                notes,
                km_intervention,
                km_avant
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            velo_id,
            data.composant_id,
            data.date_remplacement,
            data.cout or 0.0,
            data.notes or "Ancien entretien",
            data.km_intervention,
            km_avant
        ))

        cursor.execute("""
            UPDATE composants
            SET km_depart = ?
            WHERE id = ? AND velo_id = ?
        """, (
            data.km_intervention,
            data.composant_id,
            velo_id
        ))

        conn.commit()

        return {
            "statut": "success",
            "message": "Intervention enregistrée.",
            "km_intervention": data.km_intervention
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def retirer_intervention(cursor, historique_id, velo_id=1):
    """Supprime une intervention et remet la pièce dans l'état d'avant.

    Retourne (composant_id, km_depart), ou None si elle n'existe pas.
    """
    cursor.execute(
        "SELECT composant_id, km_avant FROM historique WHERE id = ? AND velo_id = ?",
        (historique_id, velo_id)
    )
    ligne = cursor.fetchone()

    if ligne is None:
        return None

    composant_id = ligne[0]
    km_avant = ligne[1]

    cursor.execute(
        "DELETE FROM historique WHERE id = ? AND velo_id = ?",
        (historique_id, velo_id)
    )

    cursor.execute("SELECT km_depart FROM composants WHERE id = ? AND velo_id = ?", (composant_id, velo_id))
    actuel = cursor.fetchone()
    km_depart = (actuel[0] or 0) if actuel else 0

    if composant_id is not None:
        cursor.execute("""
            SELECT id
            FROM historique
            WHERE composant_id = ? AND velo_id = ? AND id > ?
            ORDER BY id
            LIMIT 1
        """, (composant_id, velo_id, historique_id))
        suivante = cursor.fetchone()

        if suivante is None:
            # C'était la dernière intervention saisie : la pièce
            # revient exactement à l'état d'avant cette intervention.
            if km_avant is not None:
                km_depart = km_avant
            else:
                # Ligne ancienne, sans mémoire de l'état d'avant :
                # on reprend l'intervention précédente, ou 0.
                cursor.execute("""
                    SELECT km_intervention
                    FROM historique
                    WHERE composant_id = ? AND velo_id = ? AND km_intervention IS NOT NULL
                    ORDER BY id DESC
                    LIMIT 1
                """, (composant_id, velo_id))
                derniere = cursor.fetchone()
                km_depart = derniere[0] if derniere else 0

            cursor.execute(
                "UPDATE composants SET km_depart = ? WHERE id = ? AND velo_id = ?",
                (km_depart, composant_id, velo_id)
            )
        else:
            # Une intervention plus récente existe : l'état actuel de
            # la pièce ne change pas. On "recolle" la chaîne pour que
            # supprimer la suivante revienne au bon état d'avant.
            cursor.execute(
                "UPDATE historique SET km_avant = ? WHERE id = ? AND velo_id = ?",
                (km_avant, suivante[0], velo_id)
            )

    return composant_id, km_depart


@app.post("/api/historique/{historique_id}/supprimer")
def supprimer_intervention(
    historique_id: int,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        resultat = retirer_intervention(cursor, historique_id, velo_id)

        if resultat is None:
            raise HTTPException(
                status_code=404,
                detail="Intervention introuvable."
            )

        conn.commit()

        return {
            "statut": "success",
            "composant_id": resultat[0],
            "km_depart": resultat[1]
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/historique/supprimer-plusieurs")
def supprimer_plusieurs_interventions(
    data: SupprimerPlusieursRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Supprime plusieurs interventions d'un coup (tout ou rien)."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        supprimees = 0

        # Les plus récentes d'abord : chaque pièce repasse ainsi par
        # ses états successifs, dans le bon ordre.
        for historique_id in sorted(set(data.ids), reverse=True):
            if retirer_intervention(cursor, historique_id, velo_id) is not None:
                supprimees += 1

        conn.commit()

        return {"statut": "success", "supprimees": supprimees}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/historique/{historique_id}/modifier")
def modifier_intervention(
    historique_id: int,
    data: HistoriqueModifRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Modifie la date, le kilométrage, le coût et les notes d'une intervention."""
    verifier_auth(x_code_acces)

    try:
        date.fromisoformat(data.date_remplacement[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail="Date non valide.")

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT composant_id FROM historique WHERE id = ? AND velo_id = ?",
            (historique_id, velo_id)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(
                status_code=404,
                detail="Intervention introuvable."
            )

        composant_id = ligne[0]

        cursor.execute("SELECT km_total FROM velos WHERE id = ?", (velo_id,))
        velo = cursor.fetchone()
        km_total = velo[0] if velo and velo[0] is not None else 2700

        if data.km_intervention > km_total:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Le kilométrage de l'intervention ne peut pas "
                    "dépasser celui du vélo."
                )
            )

        notes = (data.notes or "").strip() or "Ancien entretien"

        cursor.execute("""
            UPDATE historique
            SET date_remplacement = ?, cout = ?, notes = ?, km_intervention = ?
            WHERE id = ? AND velo_id = ?
        """, (
            data.date_remplacement[:10],
            data.cout or 0.0,
            notes,
            data.km_intervention,
            historique_id,
            velo_id
        ))

        if composant_id is not None:
            cursor.execute("""
                SELECT id
                FROM historique
                WHERE composant_id = ? AND velo_id = ? AND id > ?
                ORDER BY id
                LIMIT 1
            """, (composant_id, velo_id, historique_id))
            suivante = cursor.fetchone()

            if suivante is None:
                # C'est la dernière intervention : elle fixe le point de
                # départ actuel de la pièce.
                cursor.execute(
                    "UPDATE composants SET km_depart = ? WHERE id = ? AND velo_id = ?",
                    (data.km_intervention, composant_id, velo_id)
                )
            else:
                # Sinon, c'est l'état « d'avant » de l'intervention suivante.
                cursor.execute(
                    "UPDATE historique SET km_avant = ? WHERE id = ? AND velo_id = ?",
                    (data.km_intervention, suivante[0], velo_id)
                )

        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sync")
def synchroniser_kilometrage(
    data: SynchroRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM velos WHERE id = ? AND actif = 1",
            (velo_id,)
        )
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")

        cursor.execute("""
            UPDATE velos
            SET km_total = ?, km_frac = 0, km_maj_le = ?
            WHERE id = ?
        """, (data.km_total, maintenant_utc(), velo_id))

        conn.commit()

        return {
            "statut": "success",
            "km_total": data.km_total,
            "velo_id": velo_id
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/composants/{composant_id}/remplacer")
def remplacer_composant(
    composant_id: int,
    data: RemplacementRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM composants WHERE id = ? AND velo_id = ?",
            (composant_id, velo_id)
        )

        if cursor.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail="Composant introuvable."
            )

        cursor.execute("SELECT km_total FROM velos WHERE id = ?", (velo_id,))
        velo = cursor.fetchone()

        km_global = (
            velo[0]
            if velo and velo[0] is not None
            else 2700
        )

        date_resp = (
            data.date_remplacement
            if data.date_remplacement
            else str(date.today())
        )

        notes_resp = (
            data.notes
            if data.notes
            else "Intervention standard"
        )

        cout_resp = (
            data.cout
            if data.cout is not None
            else 0.0
        )

        cursor.execute(
            "SELECT km_depart FROM composants WHERE id = ? AND velo_id = ?",
            (composant_id, velo_id)
        )
        km_avant = cursor.fetchone()[0] or 0

        cursor.execute("""
            UPDATE composants
            SET km_depart = ?
            WHERE id = ? AND velo_id = ?
        """, (
            km_global,
            composant_id,
            velo_id
        ))

        cursor.execute("""
            INSERT INTO historique (
                velo_id,
                composant_id,
                date_remplacement,
                cout,
                notes,
                km_intervention,
                km_avant
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            velo_id,
            composant_id,
            date_resp,
            cout_resp,
            notes_resp,
            km_global,
            km_avant
        ))

        conn.commit()

        return {
            "statut": "success",
            "message": "Intervention enregistrée.",
            "km_intervention": km_global
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


TERRAINS = {"mixte", "route", "chemin", "sentier"}


def ajuster_km_total(cursor, delta_km, velo_id=1):
    cursor.execute("SELECT km_total, km_frac FROM velos WHERE id = ?", (velo_id,))
    ligne = cursor.fetchone()

    base = ligne[0] if ligne and ligne[0] is not None else 2700
    frac = ligne[1] if ligne and ligne[1] is not None else 0.0

    total = max(0.0, base + frac + delta_km)
    entier = int(math.floor(total + 1e-9))

    cursor.execute(
        "UPDATE velos SET km_total = ?, km_frac = ? WHERE id = ?",
        (entier, total - entier, velo_id)
    )

    return entier


@app.get("/api/mes-reglages")
def lire_mes_reglages(
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM velos WHERE id = ? AND actif = 1",
            (velo_id,)
        )
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")
        cursor.execute(
            "SELECT cle, valeur, maj_le FROM mes_reglages_velo WHERE velo_id = ?",
            (velo_id,)
        )
        valeurs = {}
        maj = None

        for ligne in cursor.fetchall():
            valeurs[ligne[0]] = ligne[1]

            if ligne[2] and (maj is None or ligne[2] > maj):
                maj = ligne[2]

        return {"valeurs": valeurs, "maj_le": maj}

    finally:
        conn.close()


@app.post("/api/mes-reglages")
def enregistrer_mes_reglages(
    data: MesReglagesRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)

    for cle, valeur in data.valeurs.items():
        if cle not in MES_REGLAGES_CLES:
            raise HTTPException(status_code=400, detail="Réglage inconnu : " + cle)

        if len(valeur.strip()) > 200:
            raise HTTPException(status_code=400, detail="Valeur trop longue.")

    aujourdhui = date.today().isoformat()
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM velos WHERE id = ? AND actif = 1",
            (velo_id,)
        )
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")

        for cle, valeur in data.valeurs.items():
            valeur = valeur.strip()
            if valeur == "":
                cursor.execute(
                    "DELETE FROM mes_reglages_velo WHERE velo_id = ? AND cle = ?",
                    (velo_id, cle)
                )
                continue
            cursor.execute("""
                INSERT INTO mes_reglages_velo (velo_id, cle, valeur, maj_le)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (velo_id, cle) DO UPDATE
                SET valeur = excluded.valeur, maj_le = excluded.maj_le
            """, (velo_id, cle, valeur, aujourdhui))

        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/composants/{composant_id}/seuils")
def modifier_seuils(
    composant_id: int,
    data: SeuilsRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)

    if data.seuil_alerte == 0 and data.seuil_jours == 0:
        raise HTTPException(
            status_code=400,
            detail="Indiquez au moins un seuil (en km ou en jours)."
        )

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM composants WHERE id = ? AND velo_id = ?",
            (composant_id, velo_id)
        )

        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Composant introuvable pour ce vélo.")

        cursor.execute(
            "UPDATE composants SET seuil_alerte = ?, seuil_jours = ? WHERE id = ? AND velo_id = ?",
            (data.seuil_alerte, data.seuil_jours, composant_id, velo_id)
        )
        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/composants/coefficients")
def enregistrer_coefs_pieces(
    data: CoefsPiecesRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Enregistre le coefficient d'usure de chaque élément d'entretien."""
    verifier_auth(x_code_acces)

    a_enregistrer = []

    for cle, valeur in data.valeurs.items():
        try:
            composant_id = int(cle)
        except ValueError:
            raise HTTPException(status_code=400, detail="Pièce inconnue.")

        if not (0.2 <= valeur <= 5):
            raise HTTPException(
                status_code=400,
                detail="Chaque coefficient doit être entre 0,2 et 5."
            )

        a_enregistrer.append((round(valeur, 2), composant_id))

    conn = db.connect()
    cursor = conn.cursor()

    try:
        for valeur, composant_id in a_enregistrer:
            cursor.execute(
                "SELECT id FROM composants WHERE id = ? AND velo_id = ?",
                (composant_id, velo_id)
            )
            if cursor.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="Pièce introuvable pour ce vélo."
                )

            cursor.execute(
                "UPDATE composants SET coef_usure = ? WHERE id = ? AND velo_id = ?",
                (valeur, composant_id, velo_id)
            )

        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/composants/{composant_id}/position")
def deplacer_repere(
    composant_id: int,
    data: PositionRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM composants WHERE id = ? AND velo_id = ?",
            (composant_id, velo_id)
        )

        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Composant introuvable pour ce vélo.")

        cursor.execute(
            "UPDATE composants SET pos_x = ?, pos_y = ? WHERE id = ? AND velo_id = ?",
            (data.pos_x, data.pos_y, composant_id, velo_id)
        )
        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/composants/{composant_id}/supprimer")
def retirer_composant(
    composant_id: int,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Retire une pièce de la liste, avec son historique."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM composants WHERE id = ? AND velo_id = ?",
            (composant_id, velo_id)
        )

        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Composant introuvable pour ce vélo.")

        cursor.execute(
            "DELETE FROM historique WHERE composant_id = ? AND velo_id = ?",
            (composant_id, velo_id)
        )
        cursor.execute(
            "DELETE FROM composants WHERE id = ? AND velo_id = ?",
            (composant_id, velo_id)
        )
        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/import")
def importer_sortie(
    data: ImportGpxRequest,
    velo_id: Optional[int] = None,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)

    if not data.gpx and not data.fit:
        raise HTTPException(status_code=400, detail="Aucun fichier reçu.")

    try:
        if data.fit:
            try:
                octets = base64.b64decode(data.fit, validate=True)
            except ValueError:
                raise gpx_import.GpxError("Fichier FIT illisible.")

            s = gpx_import.analyser_activite(octets=octets)
        else:
            s = gpx_import.analyser_activite(texte=data.gpx)
    except gpx_import.GpxError as e:
        raise HTTPException(status_code=400, detail=str(e))

    terrain = (data.terrain or "mixte").lower()

    if terrain not in TERRAINS:
        terrain = "mixte"

    nom = (data.nom or s["nom"] or "Sortie à vélo")[:120]
    notes = (data.notes or "").strip()[:500] or None
    photos = data.photos or []

    if len(photos) > MAX_PHOTOS_PAR_SORTIE:
        raise HTTPException(
            status_code=400,
            detail=f"{MAX_PHOTOS_PAR_SORTIE} photos maximum par sortie."
        )

    photos_stockees = []

    for p in photos:
        verifier_photo(p.mini, MAX_CHARS_MINIATURE)
        verifier_photo(p.photo, MAX_CHARS_PHOTO)
        photos_stockees.append(photo_a_stocker(p))

    photos_json = json.dumps(photos_stockees) if photos_stockees else None

    conn = db.connect()
    cursor = conn.cursor()

    try:
        # Sans vélo précisé (envoi automatique depuis le PC de Math), la sortie
        # est rangée sur le vélo de la sortie la plus récente, avec « à vérifier ».
        # Depuis l'appli, le vélo choisi en haut est toujours envoyé : rien à vérifier.
        automatique = velo_id is None

        if automatique:
            cursor.execute("""
                SELECT s.velo_id
                FROM sorties s
                JOIN velos v ON v.id = s.velo_id AND v.actif = 1
                ORDER BY s.debut DESC
                LIMIT 1
            """)
            derniere = cursor.fetchone()
            velo_id = derniere[0] if derniere and derniere[0] else 1

        cursor.execute("SELECT id FROM velos WHERE id = ? AND actif = 1", (velo_id,))
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Vélo introuvable.")

        # L'heure de départ est unique dans toute la base, tous vélos confondus.
        cursor.execute(
            "SELECT id FROM sorties WHERE debut = ?",
            (s["debut"],)
        )

        if cursor.fetchone() is not None:
            raise HTTPException(
                status_code=409,
                detail="Cette sortie est déjà importée."
            )

        # Même sortie importée depuis une autre source (Komoot puis Strava) :
        # l'heure de départ peut différer de quelques secondes ou minutes.
        depart = datetime.strptime(s["debut"], "%Y-%m-%dT%H:%M:%SZ")
        format_heure = "%Y-%m-%dT%H:%M:%SZ"
        filtre_velo = "" if automatique else "velo_id = ? AND "
        parametres_proches = (
            (depart - timedelta(minutes=10)).strftime(format_heure),
            (depart + timedelta(minutes=10)).strftime(format_heure),
        )
        if not automatique:
            parametres_proches = (velo_id,) + parametres_proches
        cursor.execute(
            "SELECT distance_km FROM sorties WHERE " + filtre_velo
            + "debut >= ? AND debut <= ?",
            parametres_proches
        )

        for proche in cursor.fetchall():
            ecart = abs((proche[0] or 0) - s["distance_km"])

            if ecart <= max(0.3, 0.1 * s["distance_km"]):
                raise HTTPException(
                    status_code=409,
                    detail="Cette sortie est déjà importée."
                )

        cursor.execute("SELECT km_maj_le FROM velos WHERE id = ?", (velo_id,))
        velo = cursor.fetchone()
        derniere_maj = velo[0] if velo else None

        compte = 1 if (not derniere_maj or s["debut"] > derniere_maj) else 0

        cursor.execute("""
            INSERT INTO sorties (
                velo_id, nom, debut, sport, distance_km, denivele_pos, denivele_neg,
                duree_s, vitesse_moy, alt_max, pente_moy_montee,
                terrain, notes, km_compte, trace, photos, a_verifier
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            velo_id, nom, s["debut"], s["sport"], s["distance_km"],
            s["denivele_pos"], s["denivele_neg"], s["duree_s"],
            s["vitesse_moy"], s["alt_max"], s["pente_moy_montee"],
            terrain, notes, compte, s["trace"], photos_json,
            1 if automatique else 0
        ))

        if compte:
            km_total = ajuster_km_total(cursor, s["distance_km"], velo_id)
        else:
            cursor.execute("SELECT km_total FROM velos WHERE id = ?", (velo_id,))
            km_total = cursor.fetchone()[0]

        conn.commit()

        return {
            "statut": "success",
            "nom": nom,
            "debut": s["debut"],
            "distance_km": s["distance_km"],
            "denivele_pos": s["denivele_pos"],
            "duree_s": s["duree_s"],
            "ajoute_au_total": bool(compte),
            "km_total": km_total,
            "velo_id": velo_id,
            "a_verifier": bool(automatique)
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/verifier")
def confirmer_velo_sortie(
    sortie_id: int,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Math confirme que le vélo de cette sortie est le bon."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable pour ce vélo.")
        cursor.execute(
            "UPDATE sorties SET a_verifier = 0 WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )
        conn.commit()
        return {"statut": "success", "sortie_id": sortie_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class AffecterSortieVeloRequest(BaseModel):
    nouveau_velo_id: int = Field(ge=1)


@app.post("/api/sorties/{sortie_id}/velo")
def affecter_sortie_a_un_autre_velo(
    sortie_id: int,
    data: AffecterSortieVeloRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    nouveau_velo_id = data.nouveau_velo_id
    if nouveau_velo_id == velo_id:
        raise HTTPException(status_code=400, detail="Cette sortie est déjà affectée à ce vélo.")

    conn = db.connect()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT debut, distance_km, km_compte FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )
        sortie = cursor.fetchone()
        if sortie is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable pour ce vélo.")

        cursor.execute(
            "SELECT id FROM velos WHERE id = ? AND actif = 1",
            (nouveau_velo_id,)
        )
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Le vélo de destination est introuvable.")

        debut, distance_km, km_compte_actuel = sortie[0], float(sortie[1] or 0), bool(sortie[2])
        if km_compte_actuel:
            ajuster_km_total(cursor, -distance_km, velo_id)

        cursor.execute("SELECT km_maj_le FROM velos WHERE id = ?", (nouveau_velo_id,))
        ligne_maj = cursor.fetchone()
        derniere_maj = ligne_maj[0] if ligne_maj else None
        nouveau_compte = 1 if (not derniere_maj or debut > derniere_maj) else 0
        if nouveau_compte:
            ajuster_km_total(cursor, distance_km, nouveau_velo_id)

        cursor.execute(
            "UPDATE sorties SET velo_id = ?, km_compte = ?, a_verifier = 0 WHERE id = ? AND velo_id = ?",
            (nouveau_velo_id, nouveau_compte, sortie_id, velo_id)
        )
        conn.commit()
        return {
            "statut": "success",
            "velo_precedent": velo_id,
            "velo_nouveau": nouveau_velo_id,
            "km_compte": bool(nouveau_compte)
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/sorties")
def lister_sorties(
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT * FROM sorties WHERE velo_id = ? ORDER BY debut DESC",
            (velo_id,)
        )
        lignes_sorties = cursor.fetchall()
        reglages_usure = lire_reglages(cursor)

        resultat = []

        for row in lignes_sorties:
            sortie = dict(row)

            try:
                sortie["trace"] = json.loads(sortie["trace"] or "[]")
            except ValueError:
                sortie["trace"] = []

            try:
                brutes = json.loads(sortie["photos"] or "[]")
            except ValueError:
                brutes = []

            if not isinstance(brutes, list):
                brutes = []

            # La liste n'envoie que de petites miniatures :
            # la photo complète se charge au clic.
            sortie["photos"] = [miniature_de(p) for p in brutes]
            sortie["photos_pos"] = [position_de(p) for p in brutes]
            coef, analyse = coef_terrain_sortie(
                json.dumps(sortie["trace"]), sortie.get("surfaces"),
                sortie.get("terrain") or "mixte", reglages_usure
            )
            sortie["coef_usure"] = round(coef, 2)
            sortie["coef_analyse"] = analyse

            sortie["videos"] = lire_videos(sortie.get("videos"))

            resultat.append(sortie)

        return resultat

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/supprimer")
def supprimer_sortie(
    sortie_id: int,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT distance_km, km_compte FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(
                status_code=404,
                detail="Sortie introuvable."
            )

        if ligne[1]:
            ajuster_km_total(cursor, -float(ligne[0] or 0), velo_id)

        cursor.execute("DELETE FROM sorties WHERE id = ? AND velo_id = ?", (sortie_id, velo_id))
        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/terrain")
def changer_terrain(
    sortie_id: int,
    data: TerrainRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    terrain = data.terrain.strip().lower()

    if terrain not in TERRAINS:
        raise HTTPException(status_code=400, detail="Terrain inconnu.")

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )

        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable pour ce vélo.")

        cursor.execute(
            "UPDATE sorties SET terrain = ? WHERE id = ? AND velo_id = ?",
            (terrain, sortie_id, velo_id)
        )
        conn.commit()

        return {"statut": "success", "terrain": terrain}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.get("/api/reglages")
def obtenir_reglages(x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        return lire_reglages(cursor)

    finally:
        conn.close()


@app.post("/api/reglages")
def enregistrer_reglages(data: ReglagesRequest, x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        for cle, valeur in data.model_dump().items():
            cursor.execute("""
                INSERT INTO reglages (cle, valeur)
                VALUES (?, ?)
                ON CONFLICT (cle) DO UPDATE SET valeur = excluded.valeur
            """, (cle, valeur))

        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()



def lire_videos(texte):
    """Liste des liens vidéo d'une sortie (liste vide si rien ou illisible)."""
    try:
        videos = json.loads(texte or "[]")
    except ValueError:
        return []

    if not isinstance(videos, list):
        return []

    return [
        {"url": str(v.get("url", "")), "titre": str(v.get("titre", ""))}
        for v in videos
        if isinstance(v, dict) and v.get("url")
    ]


class RenommerRequest(BaseModel):
    nom: str = Field(min_length=1, max_length=120)


class SortieModifRequest(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=500)


@app.post("/api/sorties/{sortie_id}/modifier")
def modifier_sortie(
    sortie_id: int,
    data: SortieModifRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    # Modifie les notes sans toucher au parcours ni au kilométrage.
    verifier_auth(x_code_acces)
    notes = (data.notes or "").strip()[:500] or None
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable pour ce vélo.")

        cursor.execute(
            "UPDATE sorties SET notes = ? WHERE id = ? AND velo_id = ?",
            (notes, sortie_id, velo_id)
        )
        conn.commit()
        return {"statut": "success", "notes": notes}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/nom")
def renommer_sortie(
    sortie_id: int,
    data: RenommerRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Change le nom d'une sortie."""
    verifier_auth(x_code_acces)
    nom = " ".join(data.nom.split())

    if not nom:
        raise HTTPException(status_code=400, detail="Le nom ne peut pas être vide.")

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )

        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable pour ce vélo.")

        cursor.execute(
            "UPDATE sorties SET nom = ? WHERE id = ? AND velo_id = ?",
            (nom, sortie_id, velo_id)
        )
        conn.commit()

        return {"statut": "success", "nom": nom}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


class SurfacesRequest(BaseModel):
    # Une lettre par point du tracé : r route, v piste cyclable,
    # c chemin/piste, s sentier, u inconnu.
    codes: str = Field(min_length=2, max_length=1000)


@app.post("/api/sorties/{sortie_id}/surfaces")
def enregistrer_surfaces(
    sortie_id: int,
    data: SurfacesRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Enregistre le type de chemin (route, sentier...) le long du tracé."""
    verifier_auth(x_code_acces)

    if any(c not in "rvcsu" for c in data.codes):
        raise HTTPException(status_code=400, detail="Codes non valides.")

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT id FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )

        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable pour ce vélo.")

        cursor.execute(
            "UPDATE sorties SET surfaces = ? WHERE id = ? AND velo_id = ?",
            (data.codes, sortie_id, velo_id)
        )
        conn.commit()

        return {"statut": "success"}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/videos")
def ajouter_video(
    sortie_id: int,
    data: VideoRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Ajoute le lien d'une vidéo (YouTube, Google Photos...) à une sortie."""
    verifier_auth(x_code_acces)

    url = data.url.strip()
    titre = (data.titre or "").strip()

    if not url.lower().startswith("https://") or len(url) < 12:
        raise HTTPException(
            status_code=400,
            detail="Le lien doit commencer par https://"
        )

    if any(c.isspace() for c in url) or "<" in url or ">" in url or '"' in url:
        raise HTTPException(status_code=400, detail="Lien non valide.")

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT videos FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable.")

        videos = lire_videos(ligne[0])

        if len(videos) >= MAX_VIDEOS_PAR_SORTIE:
            raise HTTPException(
                status_code=400,
                detail=f"{MAX_VIDEOS_PAR_SORTIE} vidéos maximum par sortie."
            )

        videos.append({"url": url, "titre": titre})

        cursor.execute(
            "UPDATE sorties SET videos = ? WHERE id = ? AND velo_id = ?",
            (json.dumps(videos), sortie_id, velo_id)
        )
        conn.commit()

        return {"statut": "success", "nombre": len(videos)}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/videos/{numero}/supprimer")
def retirer_video(
    sortie_id: int,
    numero: int,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT videos FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable.")

        videos = lire_videos(ligne[0])

        if not 0 <= numero < len(videos):
            raise HTTPException(status_code=404, detail="Vidéo introuvable.")

        del videos[numero]

        cursor.execute(
            "UPDATE sorties SET videos = ? WHERE id = ? AND velo_id = ?",
            (json.dumps(videos), sortie_id, velo_id)
        )
        conn.commit()

        return {"statut": "success", "nombre": len(videos)}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/photos")
def ajouter_photos(
    sortie_id: int,
    data: AjoutPhotosRequest,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Joint des photos à une sortie déjà importée (8 au total maximum)."""
    verifier_auth(x_code_acces)

    if not data.photos:
        raise HTTPException(status_code=400, detail="Aucune photo reçue.")

    nouvelles = []

    for p in data.photos:
        verifier_photo(p.mini, MAX_CHARS_MINIATURE)
        verifier_photo(p.photo, MAX_CHARS_PHOTO)
        nouvelles.append(photo_a_stocker(p))

    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT photos FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable.")

        try:
            existantes = json.loads(ligne[0] or "[]")
        except ValueError:
            existantes = []

        if not isinstance(existantes, list):
            existantes = []

        if len(existantes) + len(nouvelles) > MAX_PHOTOS_PAR_SORTIE:
            raise HTTPException(
                status_code=400,
                detail=f"{MAX_PHOTOS_PAR_SORTIE} photos maximum par sortie."
            )

        total = existantes + nouvelles

        cursor.execute(
            "UPDATE sorties SET photos = ? WHERE id = ? AND velo_id = ?",
            (json.dumps(total), sortie_id, velo_id)
        )
        conn.commit()

        return {"statut": "success", "nombre": len(total)}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.post("/api/sorties/{sortie_id}/photos/{numero}/supprimer")
def retirer_photo(
    sortie_id: int,
    numero: int,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    """Retire une seule photo d'une sortie (les autres restent)."""
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT photos FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable.")

        try:
            photos = json.loads(ligne[0] or "[]")
        except ValueError:
            photos = []

        if not isinstance(photos, list) or not 0 <= numero < len(photos):
            raise HTTPException(status_code=404, detail="Photo introuvable.")

        del photos[numero]

        cursor.execute(
            "UPDATE sorties SET photos = ? WHERE id = ? AND velo_id = ?",
            (json.dumps(photos) if photos else None, sortie_id, velo_id)
        )
        conn.commit()

        return {"statut": "success", "nombre": len(photos)}

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


@app.get("/api/sorties/{sortie_id}/photos/{numero}")
def obtenir_photo(
    sortie_id: int,
    numero: int,
    velo_id: int = 1,
    x_code_acces: Optional[str] = Header(default=None)
):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT photos FROM sorties WHERE id = ? AND velo_id = ?",
            (sortie_id, velo_id)
        )
        ligne = cursor.fetchone()

        if ligne is None:
            raise HTTPException(status_code=404, detail="Sortie introuvable.")

        try:
            photos = json.loads(ligne[0] or "[]")
        except ValueError:
            photos = []

        if not isinstance(photos, list) or not 0 <= numero < len(photos):
            raise HTTPException(status_code=404, detail="Photo introuvable.")

        return {"image": photo_complete_de(photos[numero])}

    finally:
        conn.close()


@app.get("/api/sauvegarde")
def sauvegarde(x_code_acces: Optional[str] = Header(default=None)):
    verifier_auth(x_code_acces)
    conn = db.connect()
    cursor = conn.cursor()

    try:
        # Le registre multi-vélos contient le kilométrage courant.
        # Garder l'ancienne table comme repli pour les bases non migrées.
        cursor.execute("SELECT km_total FROM velos WHERE id = 1")
        velo = cursor.fetchone()

        if not velo or velo[0] is None:
            cursor.execute("SELECT km_total FROM velo WHERE id = 1")
            velo = cursor.fetchone()

        cursor.execute("SELECT * FROM velos ORDER BY id")
        velos = [dict(r) for r in cursor.fetchall()]

        cursor.execute("SELECT * FROM mes_reglages_velo ORDER BY velo_id, cle")
        reglages_personnels = [dict(r) for r in cursor.fetchall()]

        cursor.execute("SELECT * FROM composants ORDER BY id")
        composants = [dict(r) for r in cursor.fetchall()]

        cursor.execute("SELECT * FROM historique ORDER BY id")
        historique = [dict(r) for r in cursor.fetchall()]

        cursor.execute("SELECT * FROM sorties ORDER BY debut")
        sorties = [dict(r) for r in cursor.fetchall()]

        return {
            "date_sauvegarde": str(date.today()),
            "km_total": velo[0] if velo else None,
            "velos": velos,
            "reglages_personnels_par_velo": reglages_personnels,
            "composants": composants,
            "historique": historique,
            "sorties": sorties,
            "reglages": lire_reglages(cursor)
        }

    finally:
        conn.close()
