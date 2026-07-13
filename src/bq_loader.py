"""Laden nach BigQuery (bzw. CSV im DRY_RUN).

Zielschema (Dataset per ENV ``BQ_DATASET``, Default ``kpi_regional``):

- ``fact_kpi``     : langes Faktenschema, partitioniert nach ``ingested_at``
                     (DAY), geclustert nach ``region, kpi_cluster``
- ``dim_region``   : Dimensionstabelle (Seed: regionen.csv), je Lauf komplett
                     neu geschrieben (WRITE_TRUNCATE — klein und ableitbar)
- ``pipeline_run`` : Lauf-Metadaten (append)

Idempotenz: fact_kpi wird über einen MERGE auf den fachlichen Schlüssel
``(regionalschluessel, kpi_cluster, kennzahl, aggregation, jahr_stichtag)``
aktualisiert. Ein erneuter Lauf im selben Quartal überschreibt somit die
bestehenden Werte statt Duplikate zu erzeugen; neue Jahre/Stichtage kommen als
neue Zeilen hinzu. (Partition-Overwrite wäre die Alternative, koppelt die
Idempotenz aber an den Kalendertag des Laufs — MERGE ist hier robuster.)

DRY_RUN=1 schreibt stattdessen ``fact_kpi.csv``, ``dim_region.csv`` und
``pipeline_run.csv`` nach ``OUT_DIR`` (Default ./out) — lokales Testen ohne GCP.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from src.config import Region, Settings
from src.models import KpiRecord

log = logging.getLogger(__name__)

FACT_TABLE = "fact_kpi"
DIM_TABLE = "dim_region"
RUN_TABLE = "pipeline_run"

#: Fachlicher Schlüssel für den idempotenten MERGE
MERGE_KEYS = ("regionalschluessel", "kpi_cluster", "kennzahl", "aggregation", "jahr_stichtag")

FACT_COLUMNS = [
    "region", "regionalschluessel", "kpi_cluster", "kennzahl", "jahr_stichtag",
    "wert", "einheit", "aggregation", "quelle_name", "quelle_url",
    "stand_datum", "run_id", "ingested_at",
]


@dataclass(frozen=True)
class RunMeta:
    """Eine Zeile für pipeline_run."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    status: str  # "success" | "partial" | "failed"
    n_rows: int
    n_errors: int
    log_summary: str

    def to_row(self) -> dict:
        row = asdict(self)
        row["started_at"] = self.started_at.isoformat()
        row["finished_at"] = self.finished_at.isoformat()
        return row


def region_to_row(region: Region) -> dict:
    return {
        "region": region.name,
        "regionalschluessel": region.regionalschluessel,
        "typ": region.typ,
        "regierungsbezirk": region.regierungsbezirk,
    }


def build_loader(settings: Settings) -> "CsvLoader | BigQueryLoader":
    return CsvLoader(settings) if settings.dry_run else BigQueryLoader(settings)


# ---------------------------------------------------------------------------
# DRY_RUN: CSV-Ausgabe
# ---------------------------------------------------------------------------


class CsvLoader:
    """Schreibt die drei Zieltabellen als CSV nach OUT_DIR (lokales Testen)."""

    def __init__(self, settings: Settings):
        self.out_dir = Path(settings.out_dir)

    def load(self, records: list[KpiRecord], regions: list[Region], run_meta: RunMeta) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._write(f"{FACT_TABLE}.csv", FACT_COLUMNS, [r.to_bq_row() for r in records])
        self._write(
            f"{DIM_TABLE}.csv",
            ["region", "regionalschluessel", "typ", "regierungsbezirk"],
            [region_to_row(r) for r in regions],
        )
        self._write(
            f"{RUN_TABLE}.csv",
            ["run_id", "started_at", "finished_at", "status", "n_rows", "n_errors", "log_summary"],
            [run_meta.to_row()],
        )
        log.info("DRY_RUN: CSVs geschrieben", extra={"out_dir": str(self.out_dir), "n_rows": len(records)})

    def _write(self, filename: str, columns: list[str], rows: list[dict]) -> None:
        path = self.out_dir / filename
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------


class BigQueryLoader:
    def __init__(self, settings: Settings):
        # Lazy import: DRY_RUN und Tests kommen ohne GCP-Bibliothek aus
        from google.cloud import bigquery

        self._bq = bigquery
        self.settings = settings
        self.client = bigquery.Client(project=settings.gcp_project, location=settings.bq_location)
        self.dataset_ref = f"{settings.gcp_project}.{settings.bq_dataset}"

    # ------------------------------------------------------------------ DDL

    def ensure_infrastructure(self) -> None:
        """Legt Dataset + Tabellen an, falls nicht vorhanden (idempotent)."""
        bq = self._bq
        dataset = bq.Dataset(self.dataset_ref)
        dataset.location = self.settings.bq_location
        self.client.create_dataset(dataset, exists_ok=True)

        fact = bq.Table(f"{self.dataset_ref}.{FACT_TABLE}", schema=self._fact_schema())
        fact.time_partitioning = bq.TimePartitioning(
            type_=bq.TimePartitioningType.DAY, field="ingested_at"
        )
        fact.clustering_fields = ["region", "kpi_cluster"]
        self.client.create_table(fact, exists_ok=True)

        dim = bq.Table(f"{self.dataset_ref}.{DIM_TABLE}", schema=self._dim_schema())
        self.client.create_table(dim, exists_ok=True)

        run = bq.Table(f"{self.dataset_ref}.{RUN_TABLE}", schema=self._run_schema())
        self.client.create_table(run, exists_ok=True)

    def _fact_schema(self):
        f = self._bq.SchemaField
        return [
            f("region", "STRING", mode="REQUIRED"),
            f("regionalschluessel", "STRING", mode="REQUIRED"),
            f("kpi_cluster", "STRING", mode="REQUIRED"),
            f("kennzahl", "STRING", mode="REQUIRED"),
            f("jahr_stichtag", "STRING", mode="REQUIRED"),
            f("wert", "FLOAT64", mode="REQUIRED"),
            f("einheit", "STRING", mode="REQUIRED"),
            f("aggregation", "STRING", mode="REQUIRED"),
            f("quelle_name", "STRING", mode="REQUIRED"),
            f("quelle_url", "STRING", mode="REQUIRED"),
            f("stand_datum", "DATE", mode="REQUIRED"),
            f("run_id", "STRING", mode="REQUIRED"),
            f("ingested_at", "TIMESTAMP", mode="REQUIRED"),
        ]

    def _dim_schema(self):
        f = self._bq.SchemaField
        return [
            f("region", "STRING", mode="REQUIRED"),
            f("regionalschluessel", "STRING", mode="REQUIRED"),
            f("typ", "STRING", mode="REQUIRED"),
            f("regierungsbezirk", "STRING", mode="REQUIRED"),
        ]

    def _run_schema(self):
        f = self._bq.SchemaField
        return [
            f("run_id", "STRING", mode="REQUIRED"),
            f("started_at", "TIMESTAMP", mode="REQUIRED"),
            f("finished_at", "TIMESTAMP", mode="REQUIRED"),
            f("status", "STRING", mode="REQUIRED"),
            f("n_rows", "INT64", mode="REQUIRED"),
            f("n_errors", "INT64", mode="REQUIRED"),
            f("log_summary", "STRING"),
        ]

    # ----------------------------------------------------------------- Load

    def load(self, records: list[KpiRecord], regions: list[Region], run_meta: RunMeta) -> None:
        self.ensure_infrastructure()
        if records:
            self._merge_fact(records)
        else:
            log.warning("Keine fact_kpi-Zeilen zu laden")
        self._replace_dim_region(regions)
        self._append_pipeline_run(run_meta)

    def _merge_fact(self, records: list[KpiRecord]) -> None:
        """Stage-Tabelle laden, dann idempotenter MERGE in fact_kpi."""
        bq = self._bq
        stage_name = f"{FACT_TABLE}_stage_{records[0].run_id.replace('-', '')}"
        stage_ref = f"{self.dataset_ref}.{stage_name}"

        job_config = bq.LoadJobConfig(
            schema=self._fact_schema(),
            write_disposition=bq.WriteDisposition.WRITE_TRUNCATE,
        )
        self.client.load_table_from_json(
            [r.to_bq_row() for r in records], stage_ref, job_config=job_config
        ).result()

        on_clause = " AND ".join(f"T.{k} = S.{k}" for k in MERGE_KEYS)
        update_cols = [c for c in FACT_COLUMNS if c not in MERGE_KEYS]
        update_clause = ", ".join(f"T.{c} = S.{c}" for c in update_cols)
        insert_cols = ", ".join(FACT_COLUMNS)
        insert_vals = ", ".join(f"S.{c}" for c in FACT_COLUMNS)
        merge_sql = f"""
        MERGE `{self.dataset_ref}.{FACT_TABLE}` T
        USING `{stage_ref}` S
        ON {on_clause}
        WHEN MATCHED THEN UPDATE SET {update_clause}
        WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})
        """
        try:
            result = self.client.query(merge_sql).result()
            log.info(
                "MERGE in fact_kpi abgeschlossen",
                extra={"n_records": len(records), "num_dml_affected_rows": getattr(result, "num_dml_affected_rows", None)},
            )
        finally:
            self.client.delete_table(stage_ref, not_found_ok=True)

    def _replace_dim_region(self, regions: list[Region]) -> None:
        bq = self._bq
        job_config = bq.LoadJobConfig(
            schema=self._dim_schema(),
            write_disposition=bq.WriteDisposition.WRITE_TRUNCATE,
        )
        self.client.load_table_from_json(
            [region_to_row(r) for r in regions],
            f"{self.dataset_ref}.{DIM_TABLE}",
            job_config=job_config,
        ).result()

    def _append_pipeline_run(self, run_meta: RunMeta) -> None:
        bq = self._bq
        job_config = bq.LoadJobConfig(
            schema=self._run_schema(),
            write_disposition=bq.WriteDisposition.WRITE_APPEND,
        )
        self.client.load_table_from_json(
            [run_meta.to_row()],
            f"{self.dataset_ref}.{RUN_TABLE}",
            job_config=job_config,
        ).result()
