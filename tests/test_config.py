"""Konsistenz zwischen Code-Konstanten, kpi_spec.yaml, regionen.csv und Registry."""

from src import config
from src.config import Settings, load_kpi_spec, load_regions
from src.connectors import ALL_CONNECTORS, build_connectors

ERWARTETE_RS = {
    "Leverkusen": "05316",
    "Bonn": "05314",
    "Rhein-Sieg-Kreis": "05382",
    "Rhein-Erft-Kreis": "05362",
    "Rheinisch-Bergischer Kreis": "05378",
    "Oberbergischer Kreis": "05374",
    "Kreis Euskirchen": "05366",
}


def test_regionen_csv_vollstaendig():
    regions = load_regions()
    assert {r.name: r.regionalschluessel for r in regions} == ERWARTETE_RS
    assert all(r.regierungsbezirk == "Köln" for r in regions)


def test_cluster_konstanten_matchen_kpi_spec():
    spec_namen = {c["name"] for c in load_kpi_spec()["clusters"]}
    konstanten = {
        v for k, v in vars(config).items() if k.startswith("CLUSTER_")
    }
    assert konstanten == spec_namen


def test_jeder_spec_cluster_hat_einen_konnektor():
    registry_namen = {cls.name for cls in ALL_CONNECTORS}
    spec_konnektoren = {c["connector"] for c in load_kpi_spec()["clusters"]}
    assert spec_konnektoren <= registry_namen


def test_build_connectors_phasen_filter():
    alle = build_connectors(Settings())
    nur_phase1 = build_connectors(Settings(), include_phase2=False)
    assert len(alle) == len(ALL_CONNECTORS)
    assert all(c.phase == 1 for c in nur_phase1)
    assert len(nur_phase1) < len(alle)


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "test-projekt")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("BQ_DATASET", "anderes_dataset")
    settings = Settings.from_env()
    assert settings.gcp_project == "test-projekt"
    assert settings.dry_run is True
    assert settings.bq_dataset == "anderes_dataset"
    assert settings.bq_location == "EU"
