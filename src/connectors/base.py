"""Abstrakte Konnektor-Basisklasse + Fehlertaxonomie.

Konnektoren beschaffen und parsen ausschließlich Roh-Beobachtungen
(``fetch_raw``). Die öffentliche API ``fetch`` wendet zusätzlich die zentrale
Aggregation (aktuell / avg_3j) an und liefert fertige ``KpiRecord``-Zeilen.

Fehlertaxonomie (wichtig für die Fehlerisolierung in main.py):

- ``SourceLayoutError``   : Quelle erreichbar, aber Layout weicht vom
                            erwarteten Mapping ab → Parser-Konstanten prüfen.
- ``NotConfiguredError``  : Quelle erfordert einmalige Konfiguration
                            (z. B. BA-URL-Template) → Lauf überspringt sie
                            als SKIPPED, kein Fehler.
- ``ManualSourceError``   : Phase-2-Quelle ohne stabile API → bewusst manuell,
                            Lauf überspringt sie als SKIPPED.
- alle übrigen Exceptions : echter Fehler dieser Quelle; wird gesammelt und
                            in pipeline_run protokolliert, bricht den
                            Gesamtlauf aber nicht ab.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from src.config import Region, Settings
from src.models import KpiRecord, RawObservation
from src.transform import aggregate


class ConnectorError(RuntimeError):
    """Basisklasse aller Konnektor-Fehler."""


class SourceLayoutError(ConnectorError):
    """Quell-Layout weicht vom erwarteten Mapping ab (Parser-Konstanten prüfen)."""


class NotConfiguredError(ConnectorError):
    """Quelle benötigt einmalige Konfiguration (ENV), siehe README."""


class ManualSourceError(ConnectorError):
    """Phase-2-Quelle: keine stabile API, Beschaffung vorerst manuell."""


@dataclass(frozen=True)
class RunContext:
    """Lauf-Metadaten, die in jede fact_kpi-Zeile eingehen."""

    run_id: str
    ingested_at: datetime


class Connector(ABC):
    """Basisklasse: ein Konnektor bedient eine Quelle für 1..n KPI-Cluster."""

    #: eindeutiger Name (entspricht `connector:` in kpi_spec.yaml)
    name: ClassVar[str]
    #: 1 = automatisiert, 2 = Gerüst/manuell
    phase: ClassVar[int] = 1
    #: bediente Cluster (informativ, für Logging/Doku)
    clusters: ClassVar[tuple[str, ...]] = ()

    def __init__(self, settings: Settings):
        self.settings = settings

    @abstractmethod
    def fetch_raw(self, region: Region) -> list[RawObservation]:
        """Beschafft alle Roh-Beobachtungen dieser Quelle für eine Region."""

    def fetch(self, region: Region, ctx: RunContext) -> list[KpiRecord]:
        """Öffentliche API: Roh-Beobachtungen beschaffen + aggregieren."""
        return aggregate(self.fetch_raw(region), run_id=ctx.run_id, ingested_at=ctx.ingested_at)
