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
import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass

from src.config import LDB_GENESIS_BASE_URL, Settings
from src.net import http_post
from src.transform import parse_german_number

log = logging.getLogger(__name__)

QUELLE_NAME = "Landesdatenbank NRW (GENESIS)"

#: GENESIS-Statuscodes (Feld Status.Code in JSON-Antworten)
_CODE_OK = 0
_CODE_JOB_AUSGELOEST = 99  # Extraktion läuft als Hintergrund-Job → pollen


#: Spaltenindex-Präfixe der Merkmalsgruppen im ffcsv-2020-Format ("1_variable_*")
_VAR_IDX_RE = re.compile(r"^(\d+)_variable_code$")


@dataclass(frozen=True)
class FfcsvValue:
    """Ein Wert (eine Zeile) einer ffcsv-2020-Antwort (Long-Format).

    - ``zeit``     : Jahr/Stichtag (Spalte ``time``)
    - ``merkmale`` : {variable_code → attribute_code}, z. B. {"KREISE": "05316",
                     "KONNW1": ""} — leerer Attribut-Code = Ausprägung „Insgesamt"
    - ``inhalt``   : Messgrößen-Code (Spalte ``value_variable_code``, z. B. "GAST01")
    - ``einheit``  : Einheit der Messgröße (Spalte ``value_unit``, z. B. "Anzahl", "%")
    - ``wert``     : numerischer Wert (Spalte ``value``)
    """

    zeit: str
    merkmale: dict
    inhalt: str
    einheit: str
    wert: float


def parse_ffcsv(text: str) -> list[FfcsvValue]:
    """Parst das GENESIS-2020-ffcsv (Flat-File CSV, englische Spaltennamen).

    Kopf u. a.: ``time``, ``N_variable_code`` / ``N_variable_attribute_code``,
    ``value``, ``value_unit``, ``value_variable_code``.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    fieldnames = [f.strip() for f in (reader.fieldnames or [])]
    if "time" not in fieldnames or "value" not in fieldnames:
        raise ValueError(
            "ffcsv-2020 erwartet Spalten 'time' und 'value' — Format prüfen"
        )
    var_indices = sorted(
        int(m.group(1)) for f in fieldnames if (m := _VAR_IDX_RE.match(f))
    )

    values: list[FfcsvValue] = []
    for row in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items() if k is not None}
        zeit = row.get("time", "")
        if not zeit:
            continue
        try:
            wert = parse_german_number(row.get("value", ""))
        except ValueError:
            continue  # Platzhalter wie "...", "-", "x"
        merkmale = {
            row[f"{i}_variable_code"]: row.get(f"{i}_variable_attribute_code", "")
            for i in var_indices
            if row.get(f"{i}_variable_code")
        }
        values.append(
            FfcsvValue(
                zeit=zeit,
                merkmale=merkmale,
                inhalt=row.get("value_variable_code", ""),
                einheit=row.get("value_unit", ""),
                wert=wert,
            )
        )
    return values


def _decode_response(content: bytes) -> str:
    """Dekodiert eine GENESIS-Antwort zu Text.

    ``data/tablefile``/``resultfile`` liefern die ffcsv auch bei ``compress=false``
    teils als ZIP (Signatur ``PK``) mit einer einzelnen ``*.csv``-Datei. Diese wird
    hier ausgepackt. JSON-/CSV-Antworten kommen unverändert (nur dekodiert) zurück.
    """
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise ValueError(f"GENESIS-ZIP ohne CSV-Datei: {zf.namelist()}")
            content = zf.read(csv_names[0])
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _status_from_json(text: str) -> tuple[int, str] | None:
    """Erkennt eine GENESIS-JSON-Statusantwort. None, wenn es Nutzdaten (ffcsv) sind."""
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    status = payload.get("Status") or {}
    return int(status.get("Code", -1)), str(status.get("Content", ""))


class LandesdatenbankClient:
    """GENESIS-REST-Client.

    Authentifizierung über HTTP-HEADER ``username``/``password`` (so verlangt es
    die NRW-Instanz laut WADL/OpenAPI; Query-/Body-Auth liefert „Code 15 – nicht
    berechtigt"). Große Gemeinde-Tabellen werden server-seitig als Job aufbereitet:
    ``data/tablefile`` mit ``job=true`` liefert bei sofortiger Verfügbarkeit direkt
    das ffcsv, sonst Status-Code 99 (Auftrag ausgelöst). Dann wird ``catalogue/results``
    gepollt und das Ergebnis über ``data/resultfile`` abgeholt.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.ldb_user and self.settings.ldb_pass)

    def _auth_headers(self) -> dict:
        # Zugangsdaten als HTTP-Header → nie in URL/Logs/Exceptions
        return {"username": self.settings.ldb_user, "password": self.settings.ldb_pass}

    def _post(self, endpoint: str, data: dict) -> str:
        url = f"{LDB_GENESIS_BASE_URL}/{endpoint}"
        resp = http_post(
            url,
            self.settings,
            data={**data, "language": "de"},
            headers=self._auth_headers(),
            timeout=self.settings.ldb_timeout_seconds,
        )
        return _decode_response(resp.content)

    def fetch_tablefile(
        self, tabelle: str, regionalschluessel: str, regionalvariable: str = ""
    ) -> str:
        """Ruft eine Tabelle als ffcsv ab, gefiltert auf einen Regionalschlüssel.

        ``regionalvariable`` (z. B. "GEMEIN", "KREISE") schränkt den Abruf
        server-seitig auf die passende Regionalebene ein — ohne sie extrahiert
        GENESIS potenziell alle Regionen (sehr langsam).

        Behandelt sowohl die direkte Auslieferung als auch den Job-Fall (Code 99):
        pollt ``catalogue/results`` und lädt das fertige Ergebnis per ``resultfile``.
        """
        params = {
            "name": tabelle,
            "area": "all",
            "regionalkey": regionalschluessel,
            "format": "ffcsv",
            "compress": "false",
            "job": "true",
        }
        if regionalvariable:
            params["regionalvariable"] = regionalvariable
        text = self._post("data/tablefile", params)
        status = _status_from_json(text)
        if status is None:
            return text  # ffcsv direkt geliefert

        code, content = status
        if code == _CODE_JOB_AUSGELOEST:
            log.info(
                "GENESIS-Job ausgelöst, warte auf Ergebnis",
                extra={"tabelle": tabelle, "rs": regionalschluessel},
            )
            self._wait_for_result(tabelle)
            return self._fetch_resultfile(tabelle)
        raise ValueError(
            f"GENESIS-Fehler für Tabelle {tabelle} (Code {code}): {content[:200]}"
        )

    def _wait_for_result(self, tabelle: str) -> None:
        """Pollt catalogue/results, bis das Job-Ergebnis zum Tabellencode vorliegt."""
        for versuch in range(self.settings.ldb_job_poll_attempts):
            text = self._post("catalogue/results", {"selection": f"{tabelle}*", "area": "all"})
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {}
            treffer = [
                e for e in (payload.get("List") or [])
                if str(e.get("Code", "")).startswith(tabelle)
            ]
            if treffer:
                return
            if versuch < self.settings.ldb_job_poll_attempts - 1:
                time.sleep(self.settings.ldb_job_poll_interval_seconds)
        raise TimeoutError(
            f"GENESIS-Job für {tabelle} nach "
            f"{self.settings.ldb_job_poll_attempts} Versuchen nicht fertig"
        )

    def _fetch_resultfile(self, tabelle: str) -> str:
        text = self._post("data/resultfile", {
            "name": tabelle,
            "area": "all",
            "format": "ffcsv",
            "compress": "false",
        })
        status = _status_from_json(text)
        if status is not None and status[0] != _CODE_OK:
            raise ValueError(
                f"GENESIS-resultfile-Fehler für {tabelle} (Code {status[0]}): {status[1][:200]}"
            )
        return text
