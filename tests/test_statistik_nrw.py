"""Tests des statistik.nrw-Konnektors gegen ECHTE Quelldateien (Fixtures vom
2026-07-14, Leverkusen 05316) — komplett offline (responses für HTTP)."""

import pytest
import responses

from src.config import (
    CLUSTER_DEMOGRAFIE,
    KOMMUNALPROFIL_PDF_URL,
    ZENSUS_BEVOELKERUNG_XLSX_URL,
)
from src.connectors.base import SourceLayoutError
from src.connectors.statistik_nrw import StatistikNrwConnector


@pytest.fixture
def connector(settings) -> StatistikNrwConnector:
    return StatistikNrwConnector(settings)


@pytest.fixture
def kp_lines(fixtures_dir) -> list[str]:
    text = (fixtures_dir / "kommunalprofil_text_05316.txt").read_text(encoding="utf-8")
    return text.splitlines()


# ------------------------------------------------------------- Zensus-XLSX


def test_zensus_parsing_echte_datei(connector, region_lev, fixtures_dir):
    xlsx = (fixtures_dir / "zensus_05316000_GRUNDINFO_BEVOELKERUNG.xlsx").read_bytes()
    obs = connector.zensus_observations(xlsx, region_lev, "https://example.org/z.xlsx")

    by_kennzahl = {o.kennzahl: o for o in obs}
    assert by_kennzahl["Einwohner"].wert == 166356.0
    assert by_kennzahl["Einwohner"].jahr_stichtag == "2022-05-15"
    assert by_kennzahl["Einwohner"].kpi_cluster == CLUSTER_DEMOGRAFIE
    # Anteile aus Spaltenwerten berechnet: weiblich 84815, Ausländer/-innen 28212
    assert by_kennzahl["Anteil weiblich"].wert == pytest.approx(51.0)
    assert by_kennzahl["Anteil Nichtdeutsche"].wert == pytest.approx(17.0)


def test_zensus_layoutfehler_bei_fremder_datei(connector, region_lev):
    import io

    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Völlig anderes Layout", 1])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(SourceLayoutError, match="Bevölkerung insgesamt"):
        connector.zensus_observations(buf.getvalue(), region_lev, "https://example.org/z.xlsx")


# ------------------------------------------------------ Kommunalprofil-PDF


def test_kommunalprofil_flaeche(connector, region_lev, kp_lines):
    obs = connector.kommunalprofil_observations(kp_lines, region_lev, "https://example.org/l.pdf")
    flaeche = next(o for o in obs if o.kennzahl == "Fläche")
    # Quelle: 7887 ha → 78.87 km²
    assert flaeche.wert == 78.87
    assert flaeche.einheit == "km²"
    assert flaeche.jahr_stichtag == "2024-12-31"


def test_kommunalprofil_einwohner_serie(connector, region_lev, kp_lines):
    obs = connector.kommunalprofil_observations(kp_lines, region_lev, "https://example.org/l.pdf")
    serie = {o.jahr_stichtag: o.wert for o in obs if o.kennzahl == "Einwohner"}
    assert serie == {
        "2018-12-31": 163838.0,
        "2019-12-31": 163729.0,
        "2020-12-31": 163905.0,
        "2021-12-31": 163851.0,
        "2022-12-31": 167174.0,
        "2023-12-31": 167850.0,
        "2024-12-31": 168581.0,
    }


def test_kommunalprofil_bevoelkerungsstruktur(connector, region_lev, kp_lines):
    obs = connector.kommunalprofil_observations(kp_lines, region_lev, "https://example.org/l.pdf")
    by_kennzahl = {o.kennzahl: o for o in obs}

    assert by_kennzahl["Anteil weiblich"].wert == 50.8
    assert by_kennzahl["Anteil Nichtdeutsche"].wert == 19.0
    assert by_kennzahl["Anteil weiblich"].jahr_stichtag == "2024-12-31"

    anteile = {
        o.kennzahl: o.wert for o in obs
        if o.kennzahl.startswith("Anteil") and "Jahre" in o.kennzahl
    }
    assert anteile["Anteil unter 6 Jahre"] == 5.4
    assert anteile["Anteil 65 Jahre und mehr"] == 21.6
    # 9 disjunkte Altersgruppen (Sammelgruppe 18-65 ausgeschlossen), Summe ~100 %
    assert len(anteile) == 9
    assert sum(anteile.values()) == pytest.approx(100.0, abs=0.3)


def test_kommunalprofil_pendler(connector, region_lev, kp_lines):
    obs = connector.kommunalprofil_observations(kp_lines, region_lev, "https://example.org/l.pdf")
    by_kennzahl = {o.kennzahl: o for o in obs if o.kpi_cluster == "Pendler (Ein-/Auspendler)"}
    assert by_kennzahl["Einpendler"].wert == 38276.0
    assert by_kennzahl["Auspendler"].wert == 37901.0
    assert by_kennzahl["Pendlersaldo"].wert == 375.0
    assert by_kennzahl["Einpendler"].jahr_stichtag == "2024-06-30"


def test_kommunalprofil_gewerbe_und_umsatzsteuer(connector, region_lev, kp_lines):
    obs = connector.kommunalprofil_observations(kp_lines, region_lev, "https://example.org/l.pdf")

    def serie(kennzahl):
        return {o.jahr_stichtag: o.wert for o in obs if o.kennzahl == kennzahl}

    assert serie("Gewerbeanmeldungen") == {"2024": 2611.0}
    assert serie("Gewerbeabmeldungen") == {"2024": 2425.0}
    # Reihen über die Jahres-Kopfzeile "Merkmal 2014 2017 2020 2023"
    assert serie("Umsatzsteuerpflichtige") == {
        "2014": 4773.0, "2017": 4660.0, "2020": 4470.0, "2023": 5161.0,
    }
    assert serie("Steuerbarer Umsatz")["2023"] == 40645095.0


def test_kommunalprofil_einkommen(connector, region_lev, kp_lines):
    obs = connector.kommunalprofil_observations(kp_lines, region_lev, "https://example.org/l.pdf")
    by_kennzahl = {o.kennzahl: o for o in obs}
    assert by_kennzahl["Einkommen je Einwohner"].wert == 30567.0
    assert by_kennzahl["Einkommen je Einwohner"].jahr_stichtag == "2022"
    assert by_kennzahl["Verfügbares Einkommen je Einwohner"].wert == 24477.0
    assert by_kennzahl["Verfügbares Einkommen je Einwohner"].kpi_cluster == "Kaufkraft & Konsum"


def test_kommunalprofil_layoutfehler_bei_fremdem_text(connector, region_lev):
    with pytest.raises(SourceLayoutError, match="Datenblock"):
        connector.kommunalprofil_observations(
            ["Gänzlich anderes Dokument", "ohne bekannte Abschnitte 42"],
            region_lev,
            "https://example.org/l.pdf",
        )


def test_pendlersaldo_wird_nicht_doppelt_abgeleitet(connector, region_lev, kp_lines):
    obs = connector.kommunalprofil_observations(kp_lines, region_lev, "https://example.org/l.pdf")
    obs = obs + connector._derive_pendlersaldo(obs, region_lev)
    saldi = [o for o in obs if o.kennzahl == "Pendlersaldo"]
    assert len(saldi) == 1  # Quelle weist den Saldo selbst aus


# ------------------------------------------- fetch_raw mit gemocktem HTTP


@responses.activate
def test_fetch_raw_teilquellen_isolierung(connector, region_lev, fixtures_dir):
    """Zensus liefert, Kommunalprofil-PDF ist 404 → Konnektor liefert trotzdem."""
    xlsx = (fixtures_dir / "zensus_05316000_GRUNDINFO_BEVOELKERUNG.xlsx").read_bytes()
    responses.add(
        responses.GET,
        ZENSUS_BEVOELKERUNG_XLSX_URL.format(rs="05316"),
        body=xlsx,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    responses.add(
        responses.GET,
        KOMMUNALPROFIL_PDF_URL.format(rs="05316"),
        status=404,
    )

    obs = connector.fetch_raw(region_lev)
    kennzahlen = {o.kennzahl for o in obs}
    assert "Einwohner" in kennzahlen
    assert "Anteil weiblich" in kennzahlen


def test_zensus_wird_fuer_kreise_uebersprungen(connector):
    from src.config import Region
    from src.connectors.base import NotConfiguredError

    kreis = Region("Rhein-Sieg-Kreis", "05382", "Kreis", "Köln")
    with pytest.raises(NotConfiguredError, match="kreisfreie Städte"):
        connector._fetch_zensus(kreis)
