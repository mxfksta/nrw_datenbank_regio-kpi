import pytest

from src.config import Settings
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
