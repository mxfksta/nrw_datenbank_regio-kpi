import pytest
import responses

from src.config import LDB_GENESIS_BASE_URL, Settings
from src.connectors.landesdatenbank import LandesdatenbankClient, parse_ffcsv


def test_parse_ffcsv(fixtures_dir):
    text = (fixtures_dir / "ldb_ffcsv_bevoelkerung.csv").read_text(encoding="utf-8")
    values = parse_ffcsv(text)
    # 2024 hat Platzhalter "..." → nur 3 Werte
    assert [v.zeit for v in values] == ["2021", "2022", "2023"]
    assert values[0].wert == 163851.0
    assert values[0].merkmale == {"KREISE": "05316"}
    assert values[0].inhalt == "BEVSTD__Bevoelkerungsstand__Anzahl"


def test_parse_ffcsv_ohne_zeitspalte():
    with pytest.raises(ValueError, match="Zeit"):
        parse_ffcsv("A;B\n1;2\n")


def test_parse_ffcsv_ohne_wertspalten():
    with pytest.raises(ValueError, match="Wertspalten"):
        parse_ffcsv("Zeit;Irgendwas\n2023;x\n")


def test_client_konfiguration():
    assert not LandesdatenbankClient(Settings()).is_configured()
    assert LandesdatenbankClient(
        Settings(ldb_user="ich", ldb_pass="geheim")
    ).is_configured()


@responses.activate
def test_zugangsdaten_stehen_im_body_nicht_in_der_url():
    """Sicherheit: username/password dürfen NICHT in der URL landen (Logs!)."""
    url = f"{LDB_GENESIS_BASE_URL}/data/tablefile"
    responses.add(responses.POST, url, body="Zeit;X__Y__Anzahl\n2023;1\n")
    client = LandesdatenbankClient(
        Settings(ldb_user="geheim-user", ldb_pass="geheim-pass", rate_limit_seconds=0.0)
    )
    client.fetch_tablefile("12411-01i", "05316")

    call = responses.calls[0]
    assert "geheim-pass" not in call.request.url
    assert "geheim-user" not in call.request.url
    assert "geheim-pass" in call.request.body  # im Form-Body
    assert "regionalkey=05316" in call.request.body
