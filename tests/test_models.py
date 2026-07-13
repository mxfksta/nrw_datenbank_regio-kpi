from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from src.models import Aggregation, KpiRecord, RawObservation


def _obs(**overrides) -> RawObservation:
    base = dict(
        region="Leverkusen",
        regionalschluessel="05316",
        kpi_cluster="Demografie & Fläche",
        kennzahl="Einwohner",
        jahr_stichtag="2023",
        wert=164202.0,
        einheit="Anzahl",
        quelle_name="Test",
        quelle_url="https://example.org",
        stand_datum=date(2026, 7, 13),
    )
    base.update(overrides)
    return RawObservation(**base)


def test_raw_observation_valide():
    obs = _obs()
    assert obs.wert == 164202.0


def test_regionalschluessel_muss_5_ziffern_haben():
    with pytest.raises(ValidationError):
        _obs(regionalschluessel="5316")
    with pytest.raises(ValidationError):
        _obs(regionalschluessel="05316000")


def test_wert_darf_nicht_nan_sein():
    with pytest.raises(ValidationError):
        _obs(wert=float("nan"))
    with pytest.raises(ValidationError):
        _obs(wert=float("inf"))


def test_jahr_stichtag_braucht_jahr():
    with pytest.raises(ValidationError):
        _obs(jahr_stichtag="unbekannt")
    _obs(jahr_stichtag="2024-06-30")
    _obs(jahr_stichtag="2021-2023")


def test_kpi_record_bq_row_serialisierbar():
    record = KpiRecord(
        region="Bonn",
        regionalschluessel="05314",
        kpi_cluster="Pendler",
        kennzahl="Einpendler",
        jahr_stichtag="2023",
        wert=150000.0,
        einheit="Anzahl",
        aggregation=Aggregation.AKTUELL,
        quelle_name="Test",
        quelle_url="https://example.org",
        stand_datum=date(2026, 7, 13),
        run_id="run-1",
        ingested_at=datetime(2026, 7, 13, 6, 0, tzinfo=timezone.utc),
    )
    row = record.to_bq_row()
    assert row["aggregation"] == "aktuell"
    assert row["stand_datum"] == "2026-07-13"
    assert row["ingested_at"].startswith("2026-07-13T06:00:00")
