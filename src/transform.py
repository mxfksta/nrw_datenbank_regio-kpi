"""Transformationen: Roh-Beobachtungen → fact_kpi-Zeilen.

Kernanforderung je numerischer Kennzahl:

- ``aktuell``: neuester verfügbarer Wert (inkl. Jahr/Stichtag)
- ``avg_3j`` : Durchschnitt der 3 neuesten verfügbaren JAHRE —
  nur wenn mindestens 3 verschiedene Jahre vorliegen

Außerdem: Parsing deutscher Zahlenformate und Einheiten-Normalisierung,
damit alle Konnektoren identisch formatierte Werte liefern.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, datetime
from statistics import fmean

from src.models import Aggregation, KpiRecord, RawObservation

log = logging.getLogger(__name__)

_JAHR_RE = re.compile(r"(19|20)\d{2}")

# ---------------------------------------------------------------------------
# Zahlen-Parsing (deutsche Formate) & Einheiten-Normalisierung
# ---------------------------------------------------------------------------

#: Quell-Schreibweisen → normalisierte Einheit
EINHEITEN_MAP = {
    "prozent": "%",
    "in %": "%",
    "%": "%",
    "anzahl": "Anzahl",
    "personen": "Anzahl",
    "km2": "km²",
    "qkm": "km²",
    "km²": "km²",
    "eur": "€",
    "euro": "€",
    "€": "€",
    "eur je einwohner": "€",
    "1 000 eur": "Tsd. €",
    "1000 eur": "Tsd. €",
    "tsd. eur": "Tsd. €",
    "tsd. €": "Tsd. €",
    "eur/m2": "€/m²",
    "€/m²": "€/m²",
}


def normalize_einheit(einheit: str) -> str:
    """Normalisiert eine Quell-Einheit; unbekannte Einheiten bleiben unverändert."""
    key = einheit.strip().lower()
    return EINHEITEN_MAP.get(key, einheit.strip())


def parse_german_number(raw: str | int | float) -> float:
    """Parst deutsche Zahlformate: "1.234,5" → 1234.5, "12,3 %" → 12.3.

    Wirft ``ValueError`` bei nicht interpretierbaren Werten (inkl. der in
    amtlichen Tabellen üblichen Platzhalter "–", ".", "x", "…").
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    text = raw.strip()
    # Amtliche Platzhalter für "kein Wert" / "geheim"
    if text in {"", "-", "–", "—", ".", "..", "...", "…", "x", "X", "/", "•"}:
        raise ValueError(f"Kein numerischer Wert: {raw!r}")
    # IT.NRW nutzt Gedankenstrich/Minuszeichen als Vorzeichen ("–254")
    text = text.replace("–", "-").replace("−", "-")
    # Einheiten/Sonderzeichen entfernen, Vorzeichen erhalten
    text = re.sub(r"[^\d,.\-+]", "", text)
    if not re.search(r"\d", text):
        raise ValueError(f"Kein numerischer Wert: {raw!r}")
    if "," in text:
        # Deutsche Notation: Punkt = Tausender, Komma = Dezimal
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or re.fullmatch(r"[-+]?\d{1,3}(\.\d{3})+", text):
        # Nur Punkte, als Tausendertrenner gruppiert → entfernen
        text = text.replace(".", "")
    return float(text)


def extract_year(jahr_stichtag: str) -> int:
    """Extrahiert das (erste) Jahr aus jahr_stichtag ("2024", "2024-06-30", "31.12.2023")."""
    match = _JAHR_RE.search(jahr_stichtag)
    if not match:
        raise ValueError(f"Kein Jahr erkennbar in {jahr_stichtag!r}")
    return int(match.group(0))


# ---------------------------------------------------------------------------
# Aggregation: aktuell + avg_3j
# ---------------------------------------------------------------------------

#: Gruppierungsschlüssel: eine Zeitreihe = Region × Cluster × Kennzahl × Einheit
_GroupKey = tuple[str, str, str, str, str]


def aggregate(
    observations: list[RawObservation],
    *,
    run_id: str,
    ingested_at: datetime,
) -> list[KpiRecord]:
    """Verdichtet Roh-Zeitreihen zu fact_kpi-Zeilen (aktuell + avg_3j).

    - Innerhalb eines Jahres gewinnt der jüngste Stichtag (bzw. der zuletzt
      gelieferte Wert bei identischem Stichtag).
    - avg_3j wird nur erzeugt, wenn >= 3 verschiedene Jahre vorliegen;
      jahr_stichtag ist dann die Spanne "JJJJ-JJJJ" der einbezogenen Jahre.
    """
    groups: dict[_GroupKey, list[RawObservation]] = defaultdict(list)
    for obs in observations:
        key = (obs.region, obs.regionalschluessel, obs.kpi_cluster, obs.kennzahl, obs.einheit)
        groups[key].append(obs)

    records: list[KpiRecord] = []
    for key, group in groups.items():
        # Stabil sortieren: (Jahr, voller Stichtag-String) aufsteigend → letzter = neuester
        group_sorted = sorted(group, key=lambda o: (extract_year(o.jahr_stichtag), o.jahr_stichtag))

        # Pro Jahr nur den neuesten Stichtag behalten
        latest_per_year: dict[int, RawObservation] = {}
        for obs in group_sorted:
            latest_per_year[extract_year(obs.jahr_stichtag)] = obs

        years_desc = sorted(latest_per_year, reverse=True)
        latest = latest_per_year[years_desc[0]]

        records.append(
            KpiRecord(
                region=latest.region,
                regionalschluessel=latest.regionalschluessel,
                kpi_cluster=latest.kpi_cluster,
                kennzahl=latest.kennzahl,
                jahr_stichtag=latest.jahr_stichtag,
                wert=latest.wert,
                einheit=latest.einheit,
                aggregation=Aggregation.AKTUELL,
                quelle_name=latest.quelle_name,
                quelle_url=latest.quelle_url,
                stand_datum=latest.stand_datum,
                run_id=run_id,
                ingested_at=ingested_at,
            )
        )

        if len(years_desc) >= 3:
            avg_years = years_desc[:3]
            avg_wert = fmean(latest_per_year[y].wert for y in avg_years)
            records.append(
                KpiRecord(
                    region=latest.region,
                    regionalschluessel=latest.regionalschluessel,
                    kpi_cluster=latest.kpi_cluster,
                    kennzahl=latest.kennzahl,
                    jahr_stichtag=f"{min(avg_years)}-{max(avg_years)}",
                    wert=round(avg_wert, 4),
                    einheit=latest.einheit,
                    aggregation=Aggregation.AVG_3J,
                    quelle_name=latest.quelle_name,
                    quelle_url=latest.quelle_url,
                    stand_datum=latest.stand_datum,
                    run_id=run_id,
                    ingested_at=ingested_at,
                )
            )
        else:
            log.debug(
                "avg_3j übersprungen (weniger als 3 Jahre)",
                extra={"kennzahl": key[3], "region": key[0], "jahre": len(years_desc)},
            )

    return records


def heute() -> date:
    """Abrufdatum der Quelle (stand_datum) — zentral, damit in Tests mockbar."""
    return date.today()
