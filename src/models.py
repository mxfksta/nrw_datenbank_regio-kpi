"""Pydantic-Datenmodelle.

Zwei Ebenen:

- ``RawObservation``: ein einzelner Roh-Messwert einer Quelle für genau ein
  Jahr / einen Stichtag. Konnektoren liefern ausschließlich RawObservations —
  sie kümmern sich NICHT um Aggregation.
- ``KpiRecord``: eine Zeile im finalen Faktenschema ``fact_kpi`` (langes,
  normalisiertes Schema; eine Zeile = ein Messwert inkl. Aggregationsart).
  Entsteht aus RawObservations über :func:`src.transform.aggregate`.

Die Trennung hält Konnektoren dumm (nur beschaffen + parsen) und macht die
Aggregationslogik (aktuell / avg_3j) zentral testbar.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Amtlicher Regionalschlüssel der Kreisebene: exakt 5 Ziffern (z. B. "05316").
RS_PATTERN = r"^\d{5}$"

#: jahr_stichtag muss mindestens ein plausibles Jahr enthalten
#: (erlaubt: "2024", "2024-06-30", "2021-2023" für 3-Jahres-Schnitte).
_JAHR_RE = re.compile(r"(19|20)\d{2}")


def _pruefe_wert_endlich(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("wert muss endlich sein (kein NaN/Inf)")
    return v


def _pruefe_jahr_enthalten(v: str) -> str:
    if not _JAHR_RE.search(v):
        raise ValueError(f"jahr_stichtag ohne erkennbares Jahr: {v!r}")
    return v


class Aggregation(str, Enum):
    """Erlaubte Aggregationsarten in fact_kpi."""

    AKTUELL = "aktuell"
    AVG_3J = "avg_3j"


class RawObservation(BaseModel):
    """Ein Roh-Messwert (Quelle × Region × Kennzahl × Jahr)."""

    model_config = ConfigDict(frozen=True)

    region: str = Field(min_length=1)
    regionalschluessel: str = Field(pattern=RS_PATTERN)
    kpi_cluster: str = Field(min_length=1)
    kennzahl: str = Field(min_length=1)
    jahr_stichtag: str = Field(min_length=4)
    wert: float
    einheit: str = Field(min_length=1)
    quelle_name: str = Field(min_length=1)
    quelle_url: str = Field(min_length=1)
    stand_datum: date

    @field_validator("wert")
    @classmethod
    def _wert_endlich(cls, v: float) -> float:
        return _pruefe_wert_endlich(v)

    @field_validator("jahr_stichtag")
    @classmethod
    def _jahr_enthalten(cls, v: str) -> str:
        return _pruefe_jahr_enthalten(v)


class KpiRecord(BaseModel):
    """Eine Zeile in BigQuery ``fact_kpi`` (siehe src/bq_loader.py für DDL)."""

    model_config = ConfigDict(frozen=True)

    region: str = Field(min_length=1)
    regionalschluessel: str = Field(pattern=RS_PATTERN)
    kpi_cluster: str = Field(min_length=1)
    kennzahl: str = Field(min_length=1)
    jahr_stichtag: str = Field(min_length=4)
    wert: float
    einheit: str = Field(min_length=1)
    aggregation: Aggregation
    quelle_name: str = Field(min_length=1)
    quelle_url: str = Field(min_length=1)
    stand_datum: date
    run_id: str = Field(min_length=1)
    ingested_at: datetime

    @field_validator("wert")
    @classmethod
    def _wert_endlich(cls, v: float) -> float:
        return _pruefe_wert_endlich(v)

    @field_validator("jahr_stichtag")
    @classmethod
    def _jahr_enthalten(cls, v: str) -> str:
        return _pruefe_jahr_enthalten(v)

    def to_bq_row(self) -> dict:
        """JSON-serialisierbare Zeile für BigQuery-Load-Jobs bzw. CSV-Export."""
        return {
            "region": self.region,
            "regionalschluessel": self.regionalschluessel,
            "kpi_cluster": self.kpi_cluster,
            "kennzahl": self.kennzahl,
            "jahr_stichtag": self.jahr_stichtag,
            "wert": self.wert,
            "einheit": self.einheit,
            "aggregation": self.aggregation.value,
            "quelle_name": self.quelle_name,
            "quelle_url": self.quelle_url,
            "stand_datum": self.stand_datum.isoformat(),
            "run_id": self.run_id,
            "ingested_at": self.ingested_at.isoformat(),
        }
