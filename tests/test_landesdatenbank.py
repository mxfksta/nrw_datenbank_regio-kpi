import pytest
import responses

from src.config import LDB_GENESIS_BASE_URL, Settings
from src.connectors.landesdatenbank import LandesdatenbankClient, parse_ffcsv


def test_parse_ffcsv_2020(fixtures_dir):
    text = (fixtures_dir / "ldb_ffcsv_tourismus.csv").read_text(encoding="utf-8")
    values = parse_ffcsv(text)
    # erste Zeile: Ankünfte insgesamt 2024, Leverkusen
    v0 = values[0]
    assert v0.zeit == "2024"
    assert v0.wert == 98234.0
    assert v0.inhalt == "GAST01"
    assert v0.einheit == "Anzahl"
    assert v0.merkmale == {"KREISE": "05316", "KONNW1": ""}
    # zweite Zeile: Veränderung zum Vorjahr in % (soll später ausgefiltert werden)
    assert values[1].einheit == "%"


def test_parse_ffcsv_ohne_pflichtspalten():
    with pytest.raises(ValueError, match="time.*value|Format"):
        parse_ffcsv("A;B\n1;2\n")


def test_client_konfiguration():
    assert not LandesdatenbankClient(Settings()).is_configured()
    assert LandesdatenbankClient(
        Settings(ldb_user="ich", ldb_pass="geheim")
    ).is_configured()


@responses.activate
def test_zugangsdaten_stehen_in_headern_nicht_in_url_oder_body():
    """Sicherheit + Korrektheit: die NRW-Instanz verlangt Header-Auth; die
    Zugangsdaten dürfen weder in URL noch im Body auftauchen (Logs!)."""
    url = f"{LDB_GENESIS_BASE_URL}/data/tablefile"
    responses.add(responses.POST, url, body="Zeit;X__Y__Anzahl\n2023;1\n")
    client = LandesdatenbankClient(
        Settings(ldb_user="geheim-user", ldb_pass="geheim-pass", rate_limit_seconds=0.0)
    )
    text = client.fetch_tablefile("12411-01i", "05316")
    assert text.startswith("Zeit;")

    call = responses.calls[0]
    assert "geheim-pass" not in call.request.url
    assert "geheim-pass" not in (call.request.body or "")
    # Zugangsdaten im HTTP-Header
    assert call.request.headers["username"] == "geheim-user"
    assert call.request.headers["password"] == "geheim-pass"
    assert "regionalkey=05316" in call.request.body


@responses.activate
def test_job_polling_bei_code_99():
    """Code 99 → catalogue/results pollen, dann resultfile laden."""
    base = LDB_GENESIS_BASE_URL
    job_json = '{"Status":{"Code":99,"Content":"Auftrag ausgelöst","Type":"Information"}}'
    responses.add(responses.POST, f"{base}/data/tablefile", body=job_json,
                  content_type="application/json")
    # erster Poll: leer, zweiter Poll: Ergebnis da
    responses.add(responses.POST, f"{base}/catalogue/results",
                  body='{"List":[]}', content_type="application/json")
    responses.add(responses.POST, f"{base}/catalogue/results",
                  body='{"List":[{"Code":"45412-01i","Content":"Beherbergung"}]}',
                  content_type="application/json")
    responses.add(responses.POST, f"{base}/data/resultfile",
                  body="Zeit;GAST__Ankuenfte__Anzahl\n2024;12345\n")

    client = LandesdatenbankClient(Settings(
        ldb_user="u", ldb_pass="p", rate_limit_seconds=0.0,
        ldb_job_poll_interval_seconds=0.0, ldb_job_poll_attempts=5,
    ))
    text = client.fetch_tablefile("45412-01i", "05316")
    assert "GAST__Ankuenfte__Anzahl" in text
    # tablefile + 2× results + resultfile = 4 Calls
    assert len(responses.calls) == 4


@responses.activate
def test_fehlercode_wird_geworfen():
    responses.add(responses.POST, f"{LDB_GENESIS_BASE_URL}/data/tablefile",
                  body='{"Status":{"Code":15,"Content":"nicht berechtigt"}}',
                  content_type="application/json")
    client = LandesdatenbankClient(Settings(ldb_user="u", ldb_pass="p", rate_limit_seconds=0.0))
    with pytest.raises(ValueError, match="Code 15"):
        client.fetch_tablefile("12411-01i", "05316")
