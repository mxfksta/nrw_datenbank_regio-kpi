"""Zentrale Konfiguration.

- Regionen + Regionalschlüssel: aus ``regionen.csv`` (Seed für dim_region)
- KPI-Spezifikation: aus ``kpi_spec.yaml`` (Single Source of Truth)
- URL-Templates der Phase-1-Quellen
- Laufzeit-Einstellungen ausschließlich über ENV-Vars (12-Factor)

Warum Dateien + ENV statt hartkodierter Konstanten: Fachliche Definitionen
(Kennzahlen, Regionen) sollen ohne Code-Änderung anpassbar sein; alles
Deployment-Spezifische (Projekt, Dataset, Zugangsdaten) gehört in die Umgebung.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Cluster-Namen — MÜSSEN mit den `name`-Feldern in kpi_spec.yaml übereinstimmen
# (per Test tests/test_config.py abgesichert).
# ---------------------------------------------------------------------------
CLUSTER_DEMOGRAFIE = "Demografie & Fläche"
CLUSTER_EINKOMMEN = "Einwohner / Einkommen"
CLUSTER_WIRTSCHAFT = "Wirtschaft und Gründen"
CLUSTER_PENDLER = "Pendler"
CLUSTER_WAHLEN = "Wahlprofile"
CLUSTER_ARBEITSLOSIGKEIT = "Arbeitslosigkeit (BA)"
CLUSTER_SV_WZ = "SV-Beschäftigte nach Wirtschaftszweigen"
CLUSTER_BRANCHENMIX = "Branchenmix"
CLUSTER_WOHNEN = "Immobilien & Wohnen"
CLUSTER_KAUFKRAFT = "Kaufkraft & Konsum"
CLUSTER_TOURISMUS = "Tourismus"
CLUSTER_VERANSTALTUNGEN = "Veranstaltungen"
CLUSTER_VEREINE = "Vereine"
CLUSTER_MOBILITAET = "Mobilität & Erreichbarkeit"
CLUSTER_EINZELHANDEL = "Einzelhandel/Innenstadt & Frequenz"
CLUSTER_BILDUNG = "Bildung & Betreuung"
CLUSTER_RISIKEN = "Risiken: Starkregen/Hochwasser"

# ---------------------------------------------------------------------------
# URL-Templates Phase 1 ({rs} = 5-stelliger Regionalschlüssel)
# ---------------------------------------------------------------------------
KOMMUNALPROFIL_PDF_URL = "https://statistik.nrw/sites/default/files/municipalprofiles/l{rs}.pdf"
WAHLPROFIL_PDF_URL = "https://statistik.nrw/sites/default/files/electionprofiles/wp{rs}.pdf"
ZENSUS_BEVOELKERUNG_XLSX_URL = (
    "https://statistik.nrw/sites/default/files/municipalinformation/"
    "{rs}000_GRUNDINFO_BEVOELKERUNG.XLSX"
)
# GENESIS-REST-API der Landesdatenbank NRW (bevorzugter, maschinenlesbarer Weg)
LDB_GENESIS_BASE_URL = "https://www.landesdatenbank.nrw.de/ldbnrwws/rest/2020"

DEFAULT_USER_AGENT = (
    "regio-kpi-pipeline/0.1 (+https://github.com/mxfksta/nrw_datenbank_regio-kpi)"
)


@dataclass(frozen=True)
class Region:
    """Eine der 7 Zielregionen (Zeile aus regionen.csv, Seed für dim_region)."""

    name: str
    regionalschluessel: str
    typ: str  # "Kreisfreie Stadt" | "Kreis"
    regierungsbezirk: str


def load_regions(path: Path | None = None) -> list[Region]:
    """Lädt die Zielregionen aus regionen.csv."""
    csv_path = path or Path(os.environ.get("REGIONEN_CSV_PATH", REPO_ROOT / "regionen.csv"))
    regions: list[Region] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            regions.append(
                Region(
                    name=row["region"].strip(),
                    regionalschluessel=row["regionalschluessel"].strip(),
                    typ=row["typ"].strip(),
                    regierungsbezirk=row["regierungsbezirk"].strip(),
                )
            )
    if not regions:
        raise ValueError(f"Keine Regionen in {csv_path} gefunden")
    return regions


@lru_cache(maxsize=1)
def load_kpi_spec(path: str | None = None) -> dict:
    """Lädt kpi_spec.yaml (gecacht; Schlüssel siehe Kommentarkopf der Datei)."""
    yaml_path = Path(path or os.environ.get("KPI_SPEC_PATH", REPO_ROOT / "kpi_spec.yaml"))
    with yaml_path.open(encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    if not isinstance(spec, dict) or "clusters" not in spec:
        raise ValueError(f"kpi_spec.yaml unerwartet: 'clusters' fehlt ({yaml_path})")
    return spec


def get_cluster_spec(cluster_name: str) -> dict:
    """Liefert den Spezifikations-Block eines Clusters aus kpi_spec.yaml."""
    for cluster in load_kpi_spec()["clusters"]:
        if cluster["name"] == cluster_name:
            return cluster
    raise KeyError(f"Cluster {cluster_name!r} nicht in kpi_spec.yaml definiert")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Laufzeit-Einstellungen (12-Factor: ausschließlich ENV)."""

    gcp_project: str = ""
    bq_dataset: str = "kpi_regional"
    bq_location: str = "EU"
    dry_run: bool = True
    out_dir: str = "./out"
    log_level: str = "INFO"
    http_timeout_seconds: float = 60.0
    rate_limit_seconds: float = 1.0
    user_agent: str = DEFAULT_USER_AGENT
    # Landesdatenbank NRW (GENESIS) — optional, bevorzugt wenn gesetzt
    ldb_user: str = ""
    ldb_pass: str = ""
    # Bundesagentur für Arbeit — Download-URL-Templates ({rs} wird ersetzt)
    ba_einzelheft_url_template: str = ""
    ba_wz_csv_url_template: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gcp_project=os.environ.get("GCP_PROJECT", ""),
            bq_dataset=os.environ.get("BQ_DATASET", "kpi_regional"),
            bq_location=os.environ.get("BQ_LOCATION", "EU"),
            dry_run=_env_bool("DRY_RUN", default=False),
            out_dir=os.environ.get("OUT_DIR", "./out"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "60")),
            rate_limit_seconds=float(os.environ.get("HTTP_RATE_LIMIT_SECONDS", "1.0")),
            user_agent=os.environ.get("HTTP_USER_AGENT", DEFAULT_USER_AGENT),
            ldb_user=os.environ.get("LDB_NRW_USER", ""),
            ldb_pass=os.environ.get("LDB_NRW_PASS", ""),
            ba_einzelheft_url_template=os.environ.get("BA_EINZELHEFT_URL_TEMPLATE", ""),
            ba_wz_csv_url_template=os.environ.get("BA_WZ_CSV_URL_TEMPLATE", ""),
        )

    def validate_for_bq(self) -> None:
        """Prüft die für einen echten BigQuery-Lauf nötigen Variablen."""
        if not self.dry_run and not self.gcp_project:
            raise ValueError("GCP_PROJECT muss gesetzt sein, wenn DRY_RUN nicht aktiv ist")
