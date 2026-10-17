"""
Lecture d'un fichier GPX (export Komoot) : distance, dénivelé, durée, tracé.
Utilise uniquement la bibliothèque standard de Python.
"""

import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

TAILLE_MAX = 3_000_000       # caractères
VITESSE_MAX_KMH = 80         # au-delà : saut GPS, on ignore le point
SEUIL_DENIVELE_M = 3         # on ignore les micro-variations d'altitude
PAUSE_S = 60                 # intervalle plus long : pause, pas du roulage
POINTS_TRACE = 120           # nombre de points gardés pour dessiner le parcours


class GpxError(ValueError):
    """Fichier GPX illisible ou inutilisable."""


def _nom_balise(tag):
    return tag.rsplit("}", 1)[-1]


def _lire_heure(texte):
    texte = re.sub(r"\.\d+", "", texte.strip()).replace("Z", "+00:00")
    heure = datetime.fromisoformat(texte)
    if heure.tzinfo is None:
        heure = heure.replace(tzinfo=timezone.utc)
    return heure.astimezone(timezone.utc)


def _distance_m(a, b):
    rayon = 6371008.8
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * rayon * math.asin(math.sqrt(h))


def _lire_points(racine):
    """Retourne les points bruts : (latitude, longitude, altitude ou None, heure)."""
    points = []

    for element in racine.iter():
        if _nom_balise(element.tag) != "trkpt":
            continue

        try:
            lat = float(element.get("lat"))
            lon = float(element.get("lon"))
        except (TypeError, ValueError):
            continue

        altitude = None
        heure = None

        for enfant in element:
            balise = _nom_balise(enfant.tag)

            if balise == "ele" and enfant.text:
                try:
                    altitude = float(enfant.text)
                except ValueError:
                    pass
            elif balise == "time" and enfant.text:
                try:
                    heure = _lire_heure(enfant.text)
                except ValueError:
                    pass

        if heure is not None:
            points.append((lat, lon, altitude, heure))

    return points


def _points_fiables(points):
    """Enlève les doublons et les sauts GPS (vitesse impossible)."""
    gardes = [points[0]]

    for p in points[1:]:
        dernier = gardes[-1]
        secondes = (p[3] - dernier[3]).total_seconds()

        if secondes <= 0:
            continue

        vitesse = _distance_m(dernier, p) / secondes * 3.6

        if vitesse > VITESSE_MAX_KMH:
            continue

        gardes.append(p)

    return gardes


def _denivele(altitudes):
    """Dénivelé positif et négatif, en ignorant les petites variations."""
    altitudes = [a for a in altitudes if a is not None]

    if not altitudes:
        return 0.0, 0.0

    monte = descend = 0.0
    reference = altitudes[0]

    for a in altitudes[1:]:
        if a - reference >= SEUIL_DENIVELE_M:
            monte += a - reference
            reference = a
        elif reference - a >= SEUIL_DENIVELE_M:
            descend += reference - a
            reference = a

    return monte, descend


def _pente_moyenne_en_montee(points):
    """Pente moyenne (en %) sur les tronçons de 100 m qui montent."""
    pentes = []
    cumul = 0.0
    depart = 0

    for i in range(len(points) - 1):
        cumul += _distance_m(points[i], points[i + 1])

        if cumul >= 100:
            a0, a1 = points[depart][2], points[i + 1][2]

            if a0 is not None and a1 is not None:
                pentes.append((a1 - a0) / cumul * 100)

            cumul = 0.0
            depart = i + 1

    montees = [p for p in pentes if p > 2]

    return sum(montees) / len(montees) if montees else 0.0


def _tracer(points):
    """Version allégée du parcours, pour le dessiner à l'écran."""
    if len(points) <= POINTS_TRACE:
        choisis = points
    else:
        pas = (len(points) - 1) / (POINTS_TRACE - 1)
        choisis = [points[round(i * pas)] for i in range(POINTS_TRACE)]

    return [[round(p[0], 5), round(p[1], 5)] for p in choisis]


def _verifier_texte_xml(texte):
    if len(texte) > TAILLE_MAX:
        raise GpxError("Fichier trop volumineux.")

    minuscules = texte.lower()

    # Protection contre les fichiers XML piégés.
    if "<!doctype" in minuscules or "<!entity" in minuscules:
        raise GpxError("Fichier GPX non valide.")

    try:
        return ET.fromstring(texte.encode("utf-8"))
    except ET.ParseError:
        raise GpxError("Ce fichier n'est pas un GPX lisible.")


def _resumer(brut, nom, sport, format_fichier="GPX"):
    """Statistiques d'une sortie à partir des points (lat, lon, alt, heure)."""
    if len(brut) < 2:
        raise GpxError(
            f"Ce {format_fichier} ne contient pas d'horaires. "
            "Utilisez une sortie réalisée (enregistrée), pas un itinéraire planifié."
        )

    points = _points_fiables(sorted(brut, key=lambda p: p[3]))

    if len(points) < 2:
        raise GpxError("Trace trop courte pour être analysée.")

    distance = 0.0
    roulage = 0.0

    for i in range(len(points) - 1):
        d = _distance_m(points[i], points[i + 1])
        secondes = (points[i + 1][3] - points[i][3]).total_seconds()
        distance += d

        if secondes <= PAUSE_S and d / secondes > 0.5:
            roulage += secondes

    if distance < 100:
        raise GpxError("Sortie trop courte (moins de 100 m).")

    monte, descend = _denivele([p[2] for p in points])
    altitudes = [p[2] for p in points if p[2] is not None]
    duree = roulage if roulage > 0 else (points[-1][3] - points[0][3]).total_seconds()

    return {
        "debut": points[0][3].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nom": nom,
        "sport": sport,
        "distance_km": round(distance / 1000, 2),
        "denivele_pos": round(monte),
        "denivele_neg": round(descend),
        "duree_s": round(duree),
        "vitesse_moy": round(distance / 1000 / (duree / 3600), 1),
        "alt_max": round(max(altitudes)) if altitudes else None,
        "pente_moy_montee": round(_pente_moyenne_en_montee(points), 1),
        "trace": json.dumps(_tracer(points)),
    }


def analyser_gpx(texte):
    """Analyse un fichier GPX (texte) et retourne les statistiques de la sortie."""
    racine = _verifier_texte_xml(texte)

    if _nom_balise(racine.tag) != "gpx":
        raise GpxError("Ce fichier n'est pas un GPX.")

    nom = None
    sport = None

    for trk in racine.iter():
        if _nom_balise(trk.tag) == "trk":
            for enfant in trk:
                balise = _nom_balise(enfant.tag)

                if balise == "name" and enfant.text and nom is None:
                    nom = enfant.text.strip()
                elif balise == "type" and enfant.text and sport is None:
                    sport = enfant.text.strip()

            break

    return _resumer(_lire_points(racine), nom, sport)


# ------------------------------------------------------------
# TCX (export Garmin / Strava)
# ------------------------------------------------------------

def _lire_points_tcx(racine):
    points = []

    for element in racine.iter():
        if _nom_balise(element.tag) != "Trackpoint":
            continue

        lat = lon = altitude = heure = None

        for enfant in element.iter():
            balise = _nom_balise(enfant.tag)
            texte = (enfant.text or "").strip()

            try:
                if balise == "Time" and texte:
                    heure = _lire_heure(texte)
                elif balise == "LatitudeDegrees" and texte:
                    lat = float(texte)
                elif balise == "LongitudeDegrees" and texte:
                    lon = float(texte)
                elif balise == "AltitudeMeters" and texte:
                    altitude = float(texte)
            except ValueError:
                pass

        if lat is not None and lon is not None and heure is not None:
            points.append((lat, lon, altitude, heure))

    return points


def analyser_tcx(texte):
    racine = _verifier_texte_xml(texte)

    if _nom_balise(racine.tag) != "TrainingCenterDatabase":
        raise GpxError("Ce fichier n'est pas un TCX.")

    sport = None

    for element in racine.iter():
        if _nom_balise(element.tag) == "Activity":
            sport = element.get("Sport")
            break

    return _resumer(_lire_points_tcx(racine), None, sport, "TCX")


# ------------------------------------------------------------
# FIT (fichier binaire des compteurs GPS)
# ------------------------------------------------------------

FIT_TAILLE_MAX = 6_000_000      # octets
FIT_EPOCH = 631065600           # 31/12/1989 00:00 UTC, en secondes Unix
FIT_MSG_RECORD = 20
FIT_TAILLES_TYPES = {
    0x00: 1, 0x01: 1, 0x02: 1, 0x07: 1, 0x0A: 1, 0x0D: 1,
    0x83: 2, 0x84: 2, 0x8B: 2,
    0x85: 4, 0x86: 4, 0x88: 4, 0x8C: 4,
    0x89: 8, 0x8E: 8, 0x8F: 8, 0x90: 8,
}


def _fit_entier(octets, signe, gros_boutiste):
    return int.from_bytes(
        octets, "big" if gros_boutiste else "little", signed=signe
    )


def analyser_fit(donnees):
    """Lit un fichier FIT et retourne les statistiques de la sortie."""
    if len(donnees) > FIT_TAILLE_MAX:
        raise GpxError("Fichier trop volumineux.")

    if len(donnees) < 14 or donnees[8:12] != b".FIT":
        raise GpxError("Ce fichier n'est pas un FIT lisible.")

    taille_entete = donnees[0]
    taille_donnees = int.from_bytes(donnees[4:8], "little")
    fin = min(len(donnees), taille_entete + taille_donnees)
    position = taille_entete

    definitions = {}
    dernier_horodatage = 0
    points = []

    try:
        while position < fin:
            entete = donnees[position]
            position += 1

            if entete & 0x80:
                # Horodatage compressé : message de données seul.
                local = (entete >> 5) & 0x03
                decalage = entete & 0x1F
                compresse = True
            else:
                local = entete & 0x0F
                compresse = False

                if entete & 0x40:
                    # Définition d'un type de message.
                    gros = donnees[position + 1] == 1
                    numero = _fit_entier(donnees[position + 2:position + 4], False, gros)
                    nb = donnees[position + 4]
                    position += 5
                    champs = []

                    for _ in range(nb):
                        champs.append((
                            donnees[position],
                            donnees[position + 1],
                            donnees[position + 2],
                        ))
                        position += 3

                    taille_dev = 0

                    if entete & 0x20:
                        nb_dev = donnees[position]
                        position += 1

                        for _ in range(nb_dev):
                            taille_dev += donnees[position + 1]
                            position += 3

                    definitions[local] = (numero, gros, champs, taille_dev)
                    continue

            definition = definitions.get(local)

            if definition is None:
                raise GpxError("Fichier FIT illisible.")

            numero, gros, champs, taille_dev = definition
            valeurs = {}

            for num_champ, taille, type_base in champs:
                brut = donnees[position:position + taille]
                position += taille

                if numero != FIT_MSG_RECORD and num_champ != 253:
                    continue

                if taille != FIT_TAILLES_TYPES.get(type_base, taille) or taille > 4:
                    continue

                signe = type_base in (0x01, 0x83, 0x85)
                valeurs[num_champ] = (
                    _fit_entier(brut, signe, gros),
                    taille,
                    signe,
                )

            position += taille_dev

            if 253 in valeurs:
                dernier_horodatage = valeurs[253][0]
            elif compresse:
                base = dernier_horodatage & ~0x1F
                horodatage = base + decalage

                if horodatage < dernier_horodatage:
                    horodatage += 0x20

                dernier_horodatage = horodatage
                valeurs[253] = (horodatage, 4, False)

            if numero != FIT_MSG_RECORD:
                continue

            lat = valeurs.get(0)
            lon = valeurs.get(1)
            horodatage = valeurs.get(253)

            if not lat or not lon or not horodatage:
                continue

            if lat[0] == 0x7FFFFFFF or lon[0] == 0x7FFFFFFF:
                continue

            if horodatage[0] == 0xFFFFFFFF:
                continue

            altitude = None
            alt_fine = valeurs.get(78)
            alt_simple = valeurs.get(2)

            if alt_fine and alt_fine[0] != 0xFFFFFFFF:
                altitude = alt_fine[0] / 5 - 500
            elif alt_simple and alt_simple[0] != 0xFFFF:
                altitude = alt_simple[0] / 5 - 500

            points.append((
                lat[0] * 180 / 2 ** 31,
                lon[0] * 180 / 2 ** 31,
                altitude,
                datetime.fromtimestamp(horodatage[0] + FIT_EPOCH, timezone.utc),
            ))

    except IndexError:
        # Fin de fichier inattendue : on garde ce qui a pu être lu.
        pass

    if len(points) < 2:
        raise GpxError(
            "Ce fichier FIT ne contient pas de trace GPS "
            "(sortie en intérieur ou sans GPS)."
        )

    return _resumer(points, None, "cycling")


def analyser_activite(texte=None, octets=None):
    """Détecte le format (GPX, TCX ou FIT) et analyse la sortie."""
    if octets is not None:
        return analyser_fit(octets)

    if texte is None:
        raise GpxError("Aucun fichier reçu.")

    debut = texte[:2000].lower()

    if "<trainingcenterdatabase" in debut:
        return analyser_tcx(texte)

    return analyser_gpx(texte)
