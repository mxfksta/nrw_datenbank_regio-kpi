import pytest

from src.connectors.base import SourceLayoutError
from src.connectors.wahlprofile import WahlprofileConnector


@pytest.fixture
def connector(settings) -> WahlprofileConnector:
    return WahlprofileConnector(settings)


@pytest.fixture
def text(fixtures_dir) -> str:
    return (fixtures_dir / "wahlprofil_text_05316.txt").read_text(encoding="utf-8")


def test_alle_wahlarten_erkannt(connector, region_lev, text):
    obs = connector.observations_from_text(text, region_lev, "https://example.org/wp.pdf")
    beteiligungen = {o.kennzahl: o for o in obs if o.kennzahl.startswith("Wahlbeteiligung")}
    assert set(beteiligungen) == {
        "Wahlbeteiligung Bundestagswahl",
        "Wahlbeteiligung Landtagswahl",
        "Wahlbeteiligung Europawahl",
        "Wahlbeteiligung Kommunalwahl",
    }
    assert beteiligungen["Wahlbeteiligung Bundestagswahl"].wert == 79.3
    # Wahldatum wird als ISO-Stichtag erkannt
    assert beteiligungen["Wahlbeteiligung Bundestagswahl"].jahr_stichtag == "2025-02-23"
    assert beteiligungen["Wahlbeteiligung Kommunalwahl"].jahr_stichtag == "2025-09-14"
    assert beteiligungen["Wahlbeteiligung Europawahl"].jahr_stichtag == "2024-06-09"


def test_parteien_ergebnisse(connector, region_lev, text):
    obs = connector.observations_from_text(text, region_lev, "https://example.org/wp.pdf")
    by_kennzahl = {o.kennzahl: o.wert for o in obs}
    assert by_kennzahl["Stimmenanteil CDU Bundestagswahl"] == 28.1
    assert by_kennzahl["Stimmenanteil GRÜNE Landtagswahl"] == 16.2
    assert by_kennzahl["Stimmenanteil AfD Europawahl"] == 15.4
    assert by_kennzahl["Stimmenanteil DIE LINKE Kommunalwahl"] == 5.4
    assert by_kennzahl["Stimmenanteil Sonstige Bundestagswahl"] == 8.7
    # 4 Wahlarten × (Beteiligung + 7 Parteien)
    assert len(obs) == 4 * 8


def test_einheiten_und_cluster(connector, region_lev, text):
    obs = connector.observations_from_text(text, region_lev, "https://example.org/wp.pdf")
    assert all(o.einheit == "%" for o in obs)
    assert all(o.kpi_cluster == "Wahlprofile" for o in obs)


def test_layoutfehler_ohne_wahlart_ueberschrift(connector, region_lev):
    with pytest.raises(SourceLayoutError, match="Wahlart"):
        connector.observations_from_text("Völlig anderer Text", region_lev, "https://example.org/wp.pdf")
