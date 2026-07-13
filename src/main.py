"""Orchestrierung: alle Konnektoren × alle Regionen → BigQuery (oder CSV).

Ablauf eines Laufs (Cloud Run Job, quartalsweise per Cloud Scheduler):

1. Konnektoren × Regionen abarbeiten — jeder Abruf ist fehlerisoliert:
   ein Fehler in einer Quelle bricht den Gesamtlauf NICHT ab, sondern wird
   gesammelt und in ``pipeline_run.log_summary`` protokolliert.
   Phase-2-/unkonfigurierte Quellen werden als SKIPPED übersprungen.
2. Roh-Beobachtungen zentral aggregieren (aktuell + avg_3j).
3. Laden: BigQuery-MERGE (idempotent) bzw. CSVs bei DRY_RUN=1.
4. Zusammenfassung loggen: X Werte geladen, Y Quellen fehlgeschlagen.

Exit-Codes: 0 = Erfolg/teilweiser Erfolg mit Daten · 1 = fataler Fehler
(z. B. Laden fehlgeschlagen) · 2 = kein einziger Wert beschafft.
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import datetime, timezone

from src.bq_loader import RunMeta, build_loader
from src.config import Settings, load_regions
from src.connectors import build_connectors
from src.connectors.base import ManualSourceError, NotConfiguredError, RunContext
from src.logging_setup import setup_logging
from src.models import RawObservation
from src.transform import aggregate

log = logging.getLogger("pipeline")


def run() -> int:
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    settings.validate_for_bq()

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    log.info(
        "Pipeline-Lauf gestartet",
        extra={"run_id": run_id, "dry_run": settings.dry_run, "dataset": settings.bq_dataset},
    )

    regions = load_regions()
    connectors = build_connectors(settings)

    observations: list[RawObservation] = []
    errors: list[str] = []
    skipped: list[str] = []

    for connector in connectors:
        for region in regions:
            try:
                neue = connector.fetch_raw(region)
            except (ManualSourceError, NotConfiguredError) as exc:
                # Quelle bewusst nicht automatisiert/konfiguriert → einmal je
                # Konnektor vermerken, restliche Regionen überspringen
                skipped.append(f"{connector.name}: {exc}")
                log.info(
                    "Konnektor übersprungen",
                    extra={"connector": connector.name, "phase": connector.phase, "grund": str(exc)},
                )
                break
            except Exception as exc:  # noqa: BLE001 — Fehlerisolierung je Quelle×Region
                errors.append(f"{connector.name}/{region.name}: {exc}")
                log.error(
                    "Konnektor fehlgeschlagen",
                    extra={"connector": connector.name, "region": region.name, "run_id": run_id},
                    exc_info=True,
                )
                continue
            observations.extend(neue)
            log.info(
                "Region abgeschlossen",
                extra={"connector": connector.name, "region": region.name, "n_observations": len(neue)},
            )

    ingested_at = datetime.now(timezone.utc)
    records = aggregate(observations, run_id=run_id, ingested_at=ingested_at)

    if errors and records:
        status = "partial"
    elif errors or not records:
        status = "failed"
    else:
        status = "success"

    log_summary = " | ".join(
        [f"{len(records)} Werte, {len(errors)} Fehler, {len(skipped)} übersprungen"]
        + errors[:20]
        + [f"SKIPPED: {s}" for s in skipped]
    )[:10000]

    finished_at = datetime.now(timezone.utc)
    run_meta = RunMeta(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        n_rows=len(records),
        n_errors=len(errors),
        log_summary=log_summary,
    )

    try:
        loader = build_loader(settings)
        loader.load(records, regions, run_meta)
    except Exception:
        log.critical("Laden fehlgeschlagen — Lauf abgebrochen", extra={"run_id": run_id}, exc_info=True)
        return 1

    log.info(
        "Pipeline-Lauf beendet: %s Werte geladen, %s Quellen fehlgeschlagen, %s übersprungen",
        len(records),
        len(errors),
        len(skipped),
        extra={"run_id": run_id, "status": status, "n_rows": len(records), "n_errors": len(errors)},
    )
    return 0 if records else 2


if __name__ == "__main__":
    sys.exit(run())
