"""GENESIS-REST-Client für die Landesdatenbank NRW (bevorzugter Abrufweg).

Warum: CSV-Abrufe über die GENESIS-Schnittstelle sind deutlich robuster zu
parsen als PDF-Layouts. Die Landesdatenbank NRW stellt die Daten der
Kommunalprofile maschinenlesbar bereit (Einstieg:
https://statistik.nrw/regionale-profile/datendownloads-kommunalprofile).

Voraussetzungen:
- kostenlose Registrierung → Zugangsdaten via ENV ``LDB_NRW_USER``/``LDB_NRW_PASS``
- je Kennzahl ein verifizierter Tabellencode in ``kpi_spec.yaml`` unter ``ldb:``,
  z. B.::

      - kennzahl: "Einwohner"
        einheit: "Anzahl"
        ldb:
          tabelle: "12411-01i"     # Tabellencode in der Landesdatenbank — VERIFIZIEREN!
          inhalt: "BEVSTD"          # optional: Präfix der Wertspalte im ffcsv
          auspraegungen:            # optional: Filter Merkmal-Code → Auspraegung-Code
            GES: "GESW"

Ohne Zugangsdaten oder ohne ``ldb:``-Einträge wird dieser Pfad übersprungen
(NotConfiguredError) und der Konnektor nutzt Zensus-XLSX + Kommunalprofil-PDF.

Format-Referenz ffcsv ("Flat-File CSV", GENESIS-REST 2020):
Semikolon-separiert; Spalte ``Zeit`` trägt das Jahr, Merkmalsspalten heißen
``N_Merkmal_Code`` / ``N_Auspraegung_Code`` / ..., Wertspalten enthalten ``__``
im Namen (z. B. ``BEVSTD__Bevoelkerungsstand__Anzahl``).
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass

from src.config import LDB_GENESIS_BASE_URL, Settings
from src.net import http_post
from src.transform import parse_german_number

log = logging.getLogger(__name__)

QUELLE_NAME = "Landesdatenbank NRW (GENESIS)"


@dataclass(frozen=True)
class FfcsvValue:
    """Ein Wert aus einer ffcsv-Antwort: Zeit × Merkmalsausprägungen × Wertspalte."""

    zeit: str
    merkmale: dict  # Merkmal_Code -> Auspraegung_Code
    inhalt: str  # Name der Wertspalte (z. B. "BEVSTD__Bevoelkerungsstand__Anzahl")
    wert: float


def parse_ffcsv(text: str) -> list[FfcsvValue]:
    """Parst eine ffcsv-Antwort tolerant (unbekannte Spalten werden ignoriert)."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    if not reader.fieldnames:
        return []
    fieldnames = [f.strip() for f in reader.fieldnames]

    zeit_col = next((f for f in fieldnames if f.lower() == "zeit"), None)
    if zeit_col is None:
        raise ValueError("ffcsv ohne 'Zeit'-Spalte — Format prüfen")

    merkmal_cols: list[tuple[str, str]] = []  # (Merkmal_Code-Spalte, Auspraegung_Code-Spalte)
    for f in fieldnames:
        if f.endswith("_Merkmal_Code"):
            prefix = f.removesuffix("_Merkmal_Code")
            auspraegung = f"{prefix}_Auspraegung_Code"
            if auspraegung in fieldnames:
                merkmal_cols.append((f, auspraegung))

    wert_cols = [
        f
        for f in fieldnames
        if "__" in f and not f.endswith(("_Code", "_Label"))
    ]
    if not wert_cols:
        raise ValueError("ffcsv ohne Wertspalten (Spaltenname mit '__') — Format prüfen")

    values: list[FfcsvValue] = []
    for row in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items() if k is not None}
        zeit = row.get(zeit_col, "")
        if not zeit:
            continue
        merkmale = {
            row[mc]: row[ac]
            for mc, ac in merkmal_cols
            if row.get(mc)
        }
        for wc in wert_cols:
            try:
                wert = parse_german_number(row.get(wc, ""))
            except ValueError:
                continue  # Platzhalter wie "...", "-", "x"
            values.append(FfcsvValue(zeit=zeit, merkmale=merkmale, inhalt=wc, wert=wert))
    return values


class LandesdatenbankClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.ldb_user and self.settings.ldb_pass)

    def fetch_tablefile(self, tabelle: str, regionalschluessel: str) -> str:
        """Ruft eine Tabelle als ffcsv ab, gefiltert auf einen Regionalschlüssel.

        Nutzt POST mit den Zugangsdaten im FORM-BODY (nicht in der URL), damit
        username/password niemals in Logs oder Exceptions auftauchen.
        """
        url = f"{LDB_GENESIS_BASE_URL}/data/tablefile"
        data = {
            "username": self.settings.ldb_user,
            "password": self.settings.ldb_pass,
            "name": tabelle,
            "area": "all",
            "regionalkey": regionalschluessel,
            "format": "ffcsv",
            "language": "de",
            "compress": "false",
        }
        resp = http_post(url, self.settings, data=data)
        text = resp.text
        # GENESIS liefert Fehler teils als JSON mit HTTP 200 → defensiv erkennen
        if text.lstrip().startswith("{"):
            raise ValueError(f"GENESIS-Fehlerantwort für Tabelle {tabelle}: {text[:300]}")
        return text
