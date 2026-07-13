"""Tests des statistik.nrw-Konnektors — komplett offline (Fixtures + responses)."""

import json

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


# ------------------------------------------------------------- Zensus-XLSX


def test_zensus_parsing(connector, region_lev, fixtures_dir):
    xlsx = (fixtures_dir / "zensus_05316000_GRUNDINFO_BEVOELKERUNG.xlsx").read_bytes()
    obs = connector.zensus_observations(xlsx, region_lev, "https://example.org/z.xlsx")

    by_kennzahl = {o.kennzahl: o for o in obs}
    assert by_kennzahl["Einwohner"].wert == 163905.0
    assert by_kennzahl["Einwohner"].jahr_stichtag == "2022-05-15"
    assert by_kennzahl["Einwohner"].kpi_cluster == CLUSTER_DEMOGRAFIE
    # Anteile werden aus absoluten Zahlen berechnet
    assert by_kennzahl["Anteil weiblich"].wert == pytest.approx(51.2)
    assert by_kennzahl["Anteil Nichtdeutsche"].wert == pytest.approx(13.5)
    assert by_kennzahl["Anteil unter 18-Jährige"].wert == pytest.approx(17.5)
    assert by_kennzahl["Anteil 65-Jährige und älter"].wert == pytest.approx(21.5)
    # Altersstruktur-Anteile summieren sich auf ~100 %
    anteile = [o.wert for o in obs if o.kennzahl.startswith("Anteil") and "Jährige" in o.kennzahl]
    assert sum(anteile) == pytest.approx(100.0, abs=0.3)


def test_zensus_layoutfehler_bei_leerer_datei(connector, region_lev):
    import io

    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Völlig anderes Layout", 1])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(SourceLayoutError, match="ZENSUS_LABELS"):
        connector.zensus_observations(buf.getvalue(), region_lev, "https://example.org/z.xlsx")


# ------------------------------------------------------ Kommunalprofil-PDF


def test_kommunalprofil_row_mapping(connector, region_lev, fixtures_dir):
    rows = json.loads((fixtures_dir / "kommunalprofil_rows_05316.json").read_text())
    obs = connector.kommunalprofil_observations(rows, region_lev, "https://example.org/l.pdf")

    def serie(kennzahl):
        return {o.jahr_stichtag: o.wert for o in obs if o.kennzahl == kennzahl}

    # Mehrjährige Reihe über Jahres-Kopfzeile
    assert serie("Einwohner") == {"2021": 163851.0, "2022": 163905.0, "2023": 164202.0}
    # Textzeile mit Datum im Label
    assert serie("Fläche") == {"2023-12-31": 78.85}
    # Falsch-Positiv-Schutz: "Bevölkerung je km²" darf NICHT als Einwohner landen
    assert 2078.0 not in serie("Einwohner").values()

    assert serie("Einkommen je Einwohner")["2023"] == 26001.0
    assert serie("Verfügbares Einkommen je Einwohner")["2023"] == 22118.0
    assert serie("Umsatzsteuerpflichtige")["2021"] == 4512.0
    assert serie("Steuerbarer Umsatz")["2022"] == 13012345.0
    assert serie("Einpendler")["2023"] == 32458.0
    assert serie("Wohnungsbestand")["2021"] == 83112.0
    assert serie("Baugenehmigungen")["2023"] == 298.0
    assert serie("Übernachtungen")["2023"] == 224105.0


def test_kommunalprofil_pendlersaldo_ableitung(connector, region_lev, fixtures_dir):
    rows = json.loads((fixtures_dir / "kommunalprofil_rows_05316.json").read_text())
    obs = connector.kommunalprofil_observations(rows, region_lev, "https://example.org/l.pdf")
    saldi = connector._derive_pendlersaldo(obs, region_lev)
    by_jahr = {o.jahr_stichtag: o.wert for o in saldi}
    assert by_jahr == {"2021": 2343.0, "2022": 2667.0, "2023": 2587.0}


def test_kommunalprofil_layoutfehler_ohne_labels(connector, region_lev):
    with pytest.raises(SourceLayoutError, match="KOMMUNALPROFIL_LABELS"):
        connector.kommunalprofil_observations(
            [["Gänzlich", "anderes"], ["Layout", "42"]], region_lev, "https://example.org/l.pdf"
        )


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
