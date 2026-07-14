import pytest
import responses

from src.config import BA_EINZELHEFT_ZIP_URL, Settings
from src.connectors.arbeitsagentur import ArbeitsagenturConnector
from src.connectors.base import NotConfiguredError, SourceLayoutError


@pytest.fixture
def connector(settings) -> ArbeitsagenturConnector:
    return ArbeitsagenturConnector(settings)


# ---------------------------------------- Arbeitslose + Quoten (ZIP)


def test_einzelheft_zip_parsing(connector, region_lev, fixtures_dir):
    """Bundesweite ZIP → Bestand + Quote je Region, jüngster Monat mit Wert."""
    zip_bytes = (fixtures_dir / "ba_einzelheft_dlk.zip").read_bytes()
    with responses.RequestsMock() as mock:
        mock.add(responses.GET, BA_EINZELHEFT_ZIP_URL, body=zip_bytes,
                 content_type="application/zip")
        obs = connector._fetch_einzelheft(region_lev)

    by_kennzahl = {o.kennzahl: o for o in obs}
    # aktuell = 2025-06 (Juli ist Platzhalter 0 bzw. "...")
    assert by_kennzahl["Arbeitslose"].wert == 6817.0
    assert by_kennzahl["Arbeitslose"].jahr_stichtag == "2025-06"
    assert by_kennzahl["Arbeitslose"].einheit == "Anzahl"
    assert by_kennzahl["Arbeitslosenquote"].wert == 7.6
    assert by_kennzahl["Arbeitslosenquote"].einheit == "%"


def test_einzelheft_zip_nur_einmal_geladen(connector, region_lev, fixtures_dir):
    """Die bundesweite Datei wird pro Lauf nur EINMAL geladen (Instanz-Cache)."""
    from src.config import Region

    zip_bytes = (fixtures_dir / "ba_einzelheft_dlk.zip").read_bytes()
    bonn = Region("Bonn", "05314", "krfr. Stadt", "Köln")
    with responses.RequestsMock() as mock:
        mock.add(responses.GET, BA_EINZELHEFT_ZIP_URL, body=zip_bytes,
                 content_type="application/zip")
        connector._fetch_einzelheft(region_lev)
        connector._fetch_einzelheft(bonn)  # zweiter Abruf → aus Cache
        zip_calls = [c for c in mock.calls if "arbeitslose-quoten" in c.request.url]
        assert len(zip_calls) == 1

    # Bonn kommt aus derselben Datei
    bonn_obs = connector._fetch_einzelheft(bonn)
    assert next(o.wert for o in bonn_obs if o.kennzahl == "Arbeitslose") == 13710.0


def test_einzelheft_zip_layoutfehler(connector, region_lev):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("irgendwas.txt", "kein xlsx")
    with responses.RequestsMock() as mock:
        mock.add(responses.GET, BA_EINZELHEFT_ZIP_URL, body=buf.getvalue(),
                 content_type="application/zip")
        with pytest.raises(SourceLayoutError, match="XLSX"):
            connector._fetch_einzelheft(region_lev)


# -------------------------------------------------- WZ (noch zu konfigurieren)


def test_wz_ohne_template_wird_uebersprungen(connector, region_lev):
    with pytest.raises(NotConfiguredError, match="BA_WZ_CSV_URL_TEMPLATE"):
        connector._fetch_wz_csv(region_lev)


def test_wz_csv_parsing(connector, region_lev, fixtures_dir):
    csv_text = (fixtures_dir / "ba_wz_05316.csv").read_text(encoding="utf-8")
    obs = connector.wz_observations(csv_text, region_lev, "https://example.org/wz.csv")

    bestand = {o.kennzahl: o.wert for o in obs if o.kennzahl.startswith("SV-Beschäftigte")}
    anteil = {o.kennzahl: o.wert for o in obs if o.kennzahl.startswith("Anteil")}

    assert len(bestand) == 21
    assert len(anteil) == 21
    assert bestand["SV-Beschäftigte WZ C"] == 18400.0
    assert anteil["Anteil SV-Beschäftigte WZ C"] == pytest.approx(30.4)
    assert sum(anteil.values()) == pytest.approx(100.0, abs=0.5)
    assert all(o.jahr_stichtag == "2025-06-30" for o in obs)


def test_wz_csv_layoutfehler(connector, region_lev):
    with pytest.raises(SourceLayoutError, match="WZ-Abschnitt"):
        connector.wz_observations("voellig;anderes;format\n1;2;3\n", region_lev, "https://example.org/wz.csv")


# ------------------------------------------------------- fetch_raw (kombiniert)


def test_fetch_raw_liefert_arbeitslose_ueberspringt_wz(region_lev, fixtures_dir):
    """Default-Settings: Einzelheft-ZIP liefert, WZ ohne Template → SKIPPED."""
    settings = Settings(rate_limit_seconds=0.0)  # ba_einzelheft_zip_url = Default
    connector = ArbeitsagenturConnector(settings)
    zip_bytes = (fixtures_dir / "ba_einzelheft_dlk.zip").read_bytes()
    with responses.RequestsMock() as mock:
        mock.add(responses.GET, BA_EINZELHEFT_ZIP_URL, body=zip_bytes,
                 content_type="application/zip")
        obs = connector.fetch_raw(region_lev)
    kennzahlen = {o.kennzahl for o in obs}
    assert "Arbeitslose" in kennzahlen
    assert "Arbeitslosenquote" in kennzahlen
