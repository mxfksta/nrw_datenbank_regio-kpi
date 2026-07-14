"""Tests des Wahlprofil-Parsers gegen den ECHTEN PDF-Text (Leverkusen, 05316)."""

import pytest

from src.connectors.base import SourceLayoutError
from src.connectors.wahlprofile import WahlprofileConnector


@pytest.fixture
def connector(settings) -> WahlprofileConnector:
    return WahlprofileConnector(settings)


@pytest.fixture
def text(fixtures_dir) -> str:
    return (fixtures_dir / "wahlprofil_text_05316.txt").read_text(encoding="utf-8")


def test_alle_wahlarten_mit_juengster_wahl(connector, region_lev, text):
    obs = connector.observations_from_text(text, region_lev, "https://example.org/wp.pdf")
    beteiligungen = {o.kennzahl: o for o in obs if o.kennzahl.startswith("Wahlbeteiligung")}
    assert set(beteiligungen) == {
        "Wahlbeteiligung Bundestagswahl",
        "Wahlbeteiligung Landtagswahl",
        "Wahlbeteiligung Europawahl",
        "Wahlbeteiligung Kommunalwahl",
    }
    # Jeweils die JÜNGSTE Wahl je Wahlart, Stichtag als ISO-Datum
    assert beteiligungen["Wahlbeteiligung Bundestagswahl"].wert == 81.2
    assert beteiligungen["Wahlbeteiligung Bundestagswahl"].jahr_stichtag == "2025-02-23"
    assert beteiligungen["Wahlbeteiligung Kommunalwahl"].wert == 54.0
    assert beteiligungen["Wahlbeteiligung Kommunalwahl"].jahr_stichtag == "2025-09-14"
    assert beteiligungen["Wahlbeteiligung Landtagswahl"].jahr_stichtag == "2022-05-15"
    assert beteiligungen["Wahlbeteiligung Europawahl"].jahr_stichtag == "2024-06-09"


def test_parteien_ergebnisse(connector, region_lev, text):
    obs = connector.observations_from_text(text, region_lev, "https://example.org/wp.pdf")
    by_kennzahl = {o.kennzahl: o.wert for o in obs}
    assert by_kennzahl["Stimmenanteil CDU Kommunalwahl"] == 31.0
    assert by_kennzahl["Stimmenanteil AfD Kommunalwahl"] == 15.4
    assert by_kennzahl["Stimmenanteil DIE LINKE Bundestagswahl"] == 8.5
    assert by_kennzahl["Stimmenanteil GRÜNE Landtagswahl"] == 18.4
    assert by_kennzahl["Stimmenanteil Sonstige Europawahl"] == 17.8
    # 4 Wahlarten × (Beteiligung + 7 Parteien) — jüngste Wahlen ohne Platzhalter
    assert len(obs) == 4 * 8


def test_einheiten_und_cluster(connector, region_lev, text):
    obs = connector.observations_from_text(text, region_lev, "https://example.org/wp.pdf")
    assert all(o.einheit == "%" for o in obs)
    assert all(o.kpi_cluster == "Wahlprofile" for o in obs)


def test_platzhalter_werden_uebersprungen(connector, region_lev):
    """Historische Zeile mit x/– Platzhaltern: Parteien ohne Wert fehlen einfach."""
    text = (
        "Kommunalwahlen(Wahlen)1994bis1994\n"
        "Stichtag Wahlbeteiligungin CDU SPD GRÜNE FDP AfD DieLinke¹ Sonstige\n"
        "Prozent\n"
        "16.10.1994 81,0 37,1 37,4 10,0 3,9 x – 11,5\n"
    )
    obs = connector.observations_from_text(text, region_lev, "https://example.org/wp.pdf")
    kennzahlen = {o.kennzahl for o in obs}
    assert "Stimmenanteil CDU Kommunalwahl" in kennzahlen
    assert "Stimmenanteil AfD Kommunalwahl" not in kennzahlen       # "x"
    assert "Stimmenanteil DIE LINKE Kommunalwahl" not in kennzahlen  # "–"


def test_layoutfehler_ohne_wahlart_tabelle(connector, region_lev):
    with pytest.raises(SourceLayoutError, match="Wahlart"):
        connector.observations_from_text("Völlig anderer Text", region_lev, "https://example.org/wp.pdf")
