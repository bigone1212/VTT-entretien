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


def analyser_gpx(texte):
    """Analyse un fichier GPX (texte) et retourne les statistiques de la sortie."""
    if len(texte) > TAILLE_MAX:
        raise GpxError("Fichier trop volumineux.")

    minuscules = texte.lower()

    # Protection contre les fichiers XML piégés.
    if "<!doctype" in minuscules or "<!entity" in minuscules:
        raise GpxError("Fichier GPX non valide.")

    try:
        racine = ET.fromstring(texte.encode("utf-8"))
    except ET.ParseError:
        raise GpxError("Ce fichier n'est pas un GPX lisible.")

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

    brut = _lire_points(racine)

    if len(brut) < 2:
        raise GpxError(
            "Ce GPX ne contient pas d'horaires. "
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
