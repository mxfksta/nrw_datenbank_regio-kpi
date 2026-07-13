"""End-to-End im DRY_RUN: gemockter Konnektor → main.run() → CSV-Ausgabe."""

import csv
from datetime import date
from typing import ClassVar

import src.main as main_module
from src.config import Region
from src.connectors.base import Connector
from src.models import RawObservation


class FakeConnector(Connector):
    name: ClassVar[str] = "fake"
    clusters: ClassVar[tuple[str, ...]] = ("Demografie & Fläche",)

    def fetch_raw(self, region: Region) -> list[RawObservation]:
        def obs(jahr: str, wert: float) -> RawObservation:
            return RawObservation(
                region=region.name,
                regionalschluessel=region.regionalschluessel,
                kpi_cluster="Demografie & Fläche",
                kennzahl="Einwohner",
                jahr_stichtag=jahr,
                wert=wert,
                einheit="Anzahl",
                quelle_name="Fake-Quelle",
                quelle_url="https://example.org",
                stand_datum=date(2026, 7, 13),
            )

        return [obs("2021", 100.0), obs("2022", 200.0), obs("2023", 300.0)]


class FailingConnector(Connector):
    name: ClassVar[str] = "kaputt"

    def fetch_raw(self, region: Region) -> list[RawObservation]:
        raise RuntimeError("Quelle nicht erreichbar")


def _read_csv(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_dry_run_schreibt_csvs(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("OUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        main_module, "build_connectors", lambda settings: [FakeConnector(settings)]
    )

    exit_code = main_module.run()
    assert exit_code == 0

    fact = _read_csv(tmp_path / "fact_kpi.csv")
    # 7 Regionen × (aktuell + avg_3j)
    assert len(fact) == 7 * 2
    lev_aktuell = next(
        r for r in fact if r["region"] == "Leverkusen" and r["aggregation"] == "aktuell"
    )
    assert lev_aktuell["wert"] == "300.0"
    assert lev_aktuell["jahr_stichtag"] == "2023"
    lev_avg = next(
        r for r in fact if r["region"] == "Leverkusen" and r["aggregation"] == "avg_3j"
    )
    assert lev_avg["wert"] == "200.0"
    assert lev_avg["jahr_stichtag"] == "2021-2023"

    dim = _read_csv(tmp_path / "dim_region.csv")
    assert len(dim) == 7
    assert {d["typ"] for d in dim} == {"Kreisfreie Stadt", "Kreis"}

    runs = _read_csv(tmp_path / "pipeline_run.csv")
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["n_rows"] == "14"
    assert runs[0]["n_errors"] == "0"


def test_dry_run_fehlerisolierung(tmp_path, monkeypatch):
    """Ein kaputter Konnektor bricht den Lauf nicht ab → Status partial."""
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("OUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        main_module,
        "build_connectors",
        lambda settings: [FailingConnector(settings), FakeConnector(settings)],
    )

    exit_code = main_module.run()
    assert exit_code == 0  # Daten wurden geladen, trotz Fehlern

    runs = _read_csv(tmp_path / "pipeline_run.csv")
    assert runs[0]["status"] == "partial"
    assert int(runs[0]["n_errors"]) == 7  # kaputt × 7 Regionen
    assert "kaputt/" in runs[0]["log_summary"]


def test_dry_run_ohne_daten_exit_2(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("OUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        main_module, "build_connectors", lambda settings: [FailingConnector(settings)]
    )

    exit_code = main_module.run()
    assert exit_code == 2
    runs = _read_csv(tmp_path / "pipeline_run.csv")
    assert runs[0]["status"] == "failed"
