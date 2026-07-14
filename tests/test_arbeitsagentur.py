import pytest
import responses

from src.config import BA_EINZELHEFT_ZIP_URL, Region, Settings
from src.connectors.arbeitsagentur import ArbeitsagenturConnector
from src.connectors.base import SourceLayoutError


@pytest.fixture
def connector(settings) -> ArbeitsagenturConnector:
    return ArbeitsagenturConnector(settings)


def test_einzelheft_zip_parsing(connector, region_lev, fixtures_dir):
    """Bundesweite ZIP → Bestand + Quote je Region, jüngster Monat mit Wert."""
    zip_bytes = (fixtures_dir / "ba_einzelheft_dlk.zip").read_bytes()
    with responses.RequestsMock() as mock:
        mock.add(responses.GET, BA_EINZELHEFT_ZIP_URL, body=zip_bytes,
                 content_type="application/zip")
        obs = connector.fetch_raw(region_lev)

    by_kennzahl = {o.kennzahl: o for o in obs}
    # aktuell = 2025-06 (Juli ist Platzhalter 0 bzw. "...")
    assert by_kennzahl["Arbeitslose"].wert == 6817.0
    assert by_kennzahl["Arbeitslose"].jahr_stichtag == "2025-06"
    assert by_kennzahl["Arbeitslose"].einheit == "Anzahl"
    assert by_kennzahl["Arbeitslosenquote"].wert == 7.6
    assert by_kennzahl["Arbeitslosenquote"].einheit == "%"
    assert all(o.kpi_cluster == "Arbeitslosigkeit (BA)" for o in obs)


def test_einzelheft_zip_nur_einmal_geladen(connector, region_lev, fixtures_dir):
    """Die bundesweite Datei wird pro Lauf nur EINMAL geladen (Instanz-Cache)."""
    zip_bytes = (fixtures_dir / "ba_einzelheft_dlk.zip").read_bytes()
    bonn = Region("Bonn", "05314", "krfr. Stadt", "Köln")
    with responses.RequestsMock() as mock:
        mock.add(responses.GET, BA_EINZELHEFT_ZIP_URL, body=zip_bytes,
                 content_type="application/zip")
        connector.fetch_raw(region_lev)
        connector.fetch_raw(bonn)  # zweiter Abruf → aus Cache
        zip_calls = [c for c in mock.calls if "arbeitslose-quoten" in c.request.url]
        assert len(zip_calls) == 1

    # Bonn kommt aus derselben Datei
    bonn_obs = connector.fetch_raw(bonn)
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
            connector.fetch_raw(region_lev)


def test_connector_nur_arbeitslosigkeit_cluster():
    """SV-Beschäftigte nach WZ liegen nicht mehr beim BA-Konnektor."""
    assert ArbeitsagenturConnector.clusters == ("Arbeitslosigkeit (BA)",)
