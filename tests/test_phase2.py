"""Phase-2-Stubs: werfen ManualSourceError und werden im Lauf als SKIPPED gezählt."""

import csv

import pytest

import src.main as main_module
from src.config import Settings
from src.connectors.base import ManualSourceError
from src.connectors.phase2 import (
    BildungConnector,
    EinzelhandelConnector,
    MobilitaetConnector,
    RisikenConnector,
    VeranstaltungenConnector,
    VereineConnector,
)

ALLE_STUBS = [
    VeranstaltungenConnector,
    VereineConnector,
    MobilitaetConnector,
    EinzelhandelConnector,
    BildungConnector,
    RisikenConnector,
]


@pytest.mark.parametrize("cls", ALLE_STUBS)
def test_stub_wirft_manual_source_error(cls, region_lev):
    connector = cls(Settings())
    with pytest.raises(ManualSourceError, match="manuell"):
        connector.fetch_raw(region_lev)


def test_main_zaehlt_stub_als_skipped_nicht_als_fehler(tmp_path, monkeypatch):
    from tests.test_main_dry_run import FakeConnector

    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("OUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        main_module,
        "build_connectors",
        lambda settings: [FakeConnector(settings), VereineConnector(settings)],
    )

    exit_code = main_module.run()
    assert exit_code == 0

    with (tmp_path / "pipeline_run.csv").open(newline="", encoding="utf-8") as f:
        run = list(csv.DictReader(f))[0]
    assert run["status"] == "success"  # SKIPPED ist kein Fehler
    assert run["n_errors"] == "0"
    assert "SKIPPED: vereine" in run["log_summary"]
