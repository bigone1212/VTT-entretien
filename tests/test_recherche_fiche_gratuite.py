"""Tests ciblés de la recherche gratuite des fiches vélo."""
import main


def test_recherche_initiale_croise_les_deux_moteurs():
    appels = []

    url_trek = "https://www.trekbikes.com/fr/fr_FR/fuel-exe/"
    url_canyon = "https://www.canyon.com/fr-fr/mountain-bikes/"
    url_specialized = "https://www.specialized.com/fr/fr/turbo-levo"

    ddg = (
        '<html><a class="result__a" href="https://html.duckduckgo.com/l/?uddg='
        + main.urllib.parse.quote(url_trek, safe="")
        + '">Trek officiel</a>'
        '<a class="result__a" href="' + url_canyon + '">Canyon officiel</a></html>'
    )
    bing = (
        "<rss><channel>"
        "<item><title>Trek officiel (doublon)</title><link>" + url_trek + "</link></item>"
        "<item><title>Specialized officiel</title><link>" + url_specialized + "</link></item>"
        "</channel></rss>"
    )

    def telecharger(adresse, accepter):
        appels.append(adresse)
        if "duckduckgo.com" in adresse:
            return "text/html", ddg.encode("utf-8"), adresse
        if "bing.com" in adresse:
            return "application/rss+xml", bing.encode("utf-8"), adresse
        raise AssertionError("Adresse inattendue : " + adresse)

    ancien_auth = main.verifier_auth
    ancien_telecharger = main.telecharger_lien
    try:
        main.verifier_auth = lambda *_args, **_kwargs: None
        main.telecharger_lien = telecharger
        resultat = main.recherche_fiche_velo(
            main.RechercheFicheVeloRequest(recherche="Trek Fuel EXe 9.7 2025")
        )
    finally:
        main.verifier_auth = ancien_auth
        main.telecharger_lien = ancien_telecharger

    assert len(appels) == 2, appels
    assert any("duckduckgo.com" in appel for appel in appels), appels
    assert any("bing.com" in appel for appel in appels), appels
    urls = [item["url"] for item in resultat["candidats"]]
    assert urls == [url_trek, url_canyon, url_specialized], urls
    assert all("duckduckgo.com" not in url and "bing.com" not in url for url in urls), urls


def test_extraction_des_composants_complementaires():
    html = """
    <p>Jantes : Bontrager Line Comp 30</p>
    <p>Écran : Bosch Purion 200</p>
    <p>Guide-chaîne : MRP SXg</p>
    <p>Tige de selle télescopique : Bontrager Line Dropper</p>
    """
    pieces = main.composants_techniques_de_page(html, "https://exemple.test/fiche")
    trouves = {(piece["categorie"], piece["nom"]) for piece in pieces if piece.get("nom")}
    attendus = {
        ("jantes", "Bontrager Line Comp 30"),
        ("ecran", "Bosch Purion 200"),
        ("guide_chaine", "MRP SXg"),
        ("tige_de_selle", "Bontrager Line Dropper"),
    }
    assert attendus.issubset(trouves), {"attendus": attendus, "trouves": trouves}


if __name__ == "__main__":
    test_recherche_initiale_croise_les_deux_moteurs()
    test_extraction_des_composants_complementaires()
    print("OK : recherche DuckDuckGo + Bing, doublons et redirections filtrés ; composants complémentaires extraits.")
