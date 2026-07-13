import pytest

from src.config import Settings
from src.connectors.arbeitsagentur import ArbeitsagenturConnector
from src.connectors.base import NotConfiguredError, SourceLayoutError


@pytest.fixture
def connector(settings) -> ArbeitsagenturConnector:
    return ArbeitsagenturConnector(settings)


def test_ohne_konfiguration_wird_uebersprungen(connector, region_lev):
    """Ohne URL-Templates: SKIPPED (NotConfiguredError), kein harter Fehler."""
    with pytest.raises(NotConfiguredError, match="BA_EINZELHEFT_URL_TEMPLATE"):
        connector.fetch_raw(region_lev)


def test_einzelheft_parsing(connector, region_lev, fixtures_dir):
    xlsx = (fixtures_dir / "ba_einzelheft_05316.xlsx").read_bytes()
    obs = connector.einzelheft_observations(xlsx, region_lev, "https://example.org/eh.xlsx")
    by_kennzahl = {o.kennzahl: o for o in obs}
    assert by_kennzahl["Arbeitslose"].wert == 6512.0
    assert by_kennzahl["Arbeitslose"].einheit == "Anzahl"
    assert by_kennzahl["Arbeitslosenquote"].wert == 7.4
    assert by_kennzahl["Arbeitslosenquote"].einheit == "%"
    # Berichtsmonat "Juni 2025" → Stichtag 2025-06
    assert all(o.jahr_stichtag == "2025-06" for o in obs)


def test_einzelheft_layoutfehler(connector, region_lev):
    import io

    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Anderes Layout", 42])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(SourceLayoutError, match="EINZELHEFT_LABELS"):
        connector.einzelheft_observations(buf.getvalue(), region_lev, "https://example.org/eh.xlsx")


def test_wz_csv_parsing(connector, region_lev, fixtures_dir):
    csv_text = (fixtures_dir / "ba_wz_05316.csv").read_text(encoding="utf-8")
    obs = connector.wz_observations(csv_text, region_lev, "https://example.org/wz.csv")

    bestand = {o.kennzahl: o.wert for o in obs if o.kennzahl.startswith("SV-Beschäftigte")}
    anteil = {o.kennzahl: o.wert for o in obs if o.kennzahl.startswith("Anteil")}

    # alle 21 WZ-Abschnitte A–U, jeweils Bestand + Anteil
    assert len(bestand) == 21
    assert len(anteil) == 21
    assert bestand["SV-Beschäftigte WZ C"] == 18400.0
    assert anteil["Anteil SV-Beschäftigte WZ C"] == pytest.approx(30.4)
    # Anteile summieren sich auf ~100 %
    assert sum(anteil.values()) == pytest.approx(100.0, abs=0.5)
    # Stichtag aus "Stichtag: 30.06.2025"
    assert all(o.jahr_stichtag == "2025-06-30" for o in obs)


def test_wz_csv_layoutfehler(connector, region_lev):
    with pytest.raises(SourceLayoutError, match="WZ-Abschnitt"):
        connector.wz_observations("voellig;anderes;format\n1;2;3\n", region_lev, "https://example.org/wz.csv")


def test_nur_eine_teilquelle_konfiguriert_ist_ok(region_lev, fixtures_dir):
    """Nur WZ-CSV konfiguriert → Einzelheft wird übersprungen, WZ geliefert."""
    import responses

    settings = Settings(
        rate_limit_seconds=0.0,
        ba_wz_csv_url_template="https://example.org/wz/{rs}.csv",
    )
    connector = ArbeitsagenturConnector(settings)
    csv_text = (fixtures_dir / "ba_wz_05316.csv").read_text(encoding="utf-8")

    with responses.RequestsMock() as mock:
        mock.add(responses.GET, "https://example.org/wz/05316.csv", body=csv_text)
        obs = connector.fetch_raw(region_lev)
    assert any(o.kennzahl == "SV-Beschäftigte WZ C" for o in obs)
