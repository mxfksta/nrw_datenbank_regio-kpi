from datetime import date, datetime, timezone

import pytest

from src.models import Aggregation, RawObservation
from src.transform import (
    aggregate,
    extract_year,
    normalize_einheit,
    parse_german_number,
)

INGESTED = datetime(2026, 7, 13, 6, 0, tzinfo=timezone.utc)


def _obs(jahr: str, wert: float, kennzahl: str = "Einwohner", **overrides) -> RawObservation:
    base = dict(
        region="Leverkusen",
        regionalschluessel="05316",
        kpi_cluster="Demografie & Fläche",
        kennzahl=kennzahl,
        jahr_stichtag=jahr,
        wert=wert,
        einheit="Anzahl",
        quelle_name="Test",
        quelle_url="https://example.org",
        stand_datum=date(2026, 7, 13),
    )
    base.update(overrides)
    return RawObservation(**base)


# ---------------------------------------------------------------- Zahlen


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.234,5", 1234.5),
        ("163 851", 163851.0),
        ("12,3 %", 12.3),
        ("78,85 km²", 78.85),
        ("1.234.567", 1234567.0),
        ("-3,2", -3.2),
        (42, 42.0),
        (7.4, 7.4),
        ("2023", 2023.0),
    ],
)
def test_parse_german_number(raw, expected):
    assert parse_german_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "-", "–", ".", "x", "…", "n/v", "Abwasser- und"])
def test_parse_german_number_platzhalter(raw):
    with pytest.raises(ValueError):
        parse_german_number(raw)


def test_normalize_einheit():
    assert normalize_einheit("Prozent") == "%"
    assert normalize_einheit("qkm") == "km²"
    assert normalize_einheit("EUR") == "€"
    assert normalize_einheit("Fantasieeinheit") == "Fantasieeinheit"


def test_extract_year():
    assert extract_year("2024") == 2024
    assert extract_year("2024-06-30") == 2024
    assert extract_year("31.12.2023") == 2023
    with pytest.raises(ValueError):
        extract_year("unbekannt")


# ------------------------------------------------------------- Aggregation


def test_aktuell_nimmt_neuestes_jahr():
    records = aggregate(
        [_obs("2021", 10.0), _obs("2023", 30.0), _obs("2022", 20.0)],
        run_id="r1",
        ingested_at=INGESTED,
    )
    aktuell = [r for r in records if r.aggregation == Aggregation.AKTUELL]
    assert len(aktuell) == 1
    assert aktuell[0].wert == 30.0
    assert aktuell[0].jahr_stichtag == "2023"
    assert aktuell[0].run_id == "r1"


def test_avg_3j_bei_genau_3_jahren():
    records = aggregate(
        [_obs("2021", 10.0), _obs("2022", 20.0), _obs("2023", 30.0)],
        run_id="r1",
        ingested_at=INGESTED,
    )
    avg = [r for r in records if r.aggregation == Aggregation.AVG_3J]
    assert len(avg) == 1
    assert avg[0].wert == 20.0
    assert avg[0].jahr_stichtag == "2021-2023"


def test_avg_3j_nimmt_nur_die_3_neuesten_jahre():
    records = aggregate(
        [_obs("2020", 100.0), _obs("2021", 10.0), _obs("2022", 20.0), _obs("2023", 30.0)],
        run_id="r1",
        ingested_at=INGESTED,
    )
    avg = [r for r in records if r.aggregation == Aggregation.AVG_3J]
    assert avg[0].wert == 20.0  # 2020 fließt nicht ein
    assert avg[0].jahr_stichtag == "2021-2023"


def test_kein_avg_3j_bei_2_jahren():
    records = aggregate(
        [_obs("2022", 20.0), _obs("2023", 30.0)],
        run_id="r1",
        ingested_at=INGESTED,
    )
    assert [r.aggregation for r in records] == [Aggregation.AKTUELL]


def test_innerhalb_eines_jahres_gewinnt_der_neueste_stichtag():
    records = aggregate(
        [
            _obs("2022-05-15", 163905.0),   # Zensus
            _obs("2022-12-31", 164000.0),   # Fortschreibung 31.12.
        ],
        run_id="r1",
        ingested_at=INGESTED,
    )
    aktuell = [r for r in records if r.aggregation == Aggregation.AKTUELL]
    assert aktuell[0].wert == 164000.0
    assert aktuell[0].jahr_stichtag == "2022-12-31"


def test_getrennte_gruppen_je_kennzahl():
    records = aggregate(
        [_obs("2023", 1.0, kennzahl="Einpendler"), _obs("2023", 2.0, kennzahl="Auspendler")],
        run_id="r1",
        ingested_at=INGESTED,
    )
    assert {r.kennzahl for r in records} == {"Einpendler", "Auspendler"}
