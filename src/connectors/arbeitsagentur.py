"""Konnektor Bundesagentur für Arbeit (statistik.arbeitsagentur.de).

Zwei Teilquellen:

- **Arbeitslose + Arbeitslosenquoten (Gemeinde-/Kreisebene)** → Cluster
  "Arbeitslosigkeit (BA)". Quelle ist EINE bundesweite ZIP ("dlk" =
  Deutschland/Länder/Kreise) mit zwei XLSX (Bestand + Quoten). Im Blatt
  ``Übersicht_Kreise`` steht je Zeile ein Regionalschlüssel; die Spalten sind
  Monatswerte. Wir laden die Datei EINMAL pro Lauf (Instanz-Cache) und filtern
  daraus alle 7 Regionen — kein Download pro Region, keine Konfiguration nötig
  (stabile Default-URL, per ENV ``BA_EINZELHEFT_ZIP_URL`` überschreibbar).
  Emittiert wird der jeweils neueste Berichtsmonat mit Wert (``aktuell``);
  avg_3j entsteht hier nicht, da die Datei nur ~2 Jahre Monatswerte enthält.

- **SV-Beschäftigte nach Wirtschaftszweigen (WZ A–U)** → Cluster
  "SV-Beschäftigte nach Wirtschaftszweigen" (der Cluster "Branchenmix" ist
  laut kpi_spec.yaml eine Sicht darauf). Die BA stellt die WZ-Tiefe nur über
  den interaktiven Report "Branchen im Fokus" bereit; eine stabile Export-URL
  ist noch zu ermitteln und als ENV ``BA_WZ_CSV_URL_TEMPLATE`` ({rs}) zu
  hinterlegen. Ohne Konfiguration wird diese Teilquelle als SKIPPED
  übersprungen (Parser ist implementiert und getestet).
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import re
import zipfile
from typing import ClassVar

import openpyxl

from src.config import (
    CLUSTER_ARBEITSLOSIGKEIT,
    CLUSTER_BRANCHENMIX,
    CLUSTER_SV_WZ,
    Region,
    get_cluster_spec,
)
from src.connectors.base import Connector, ConnectorError, NotConfiguredError, SourceLayoutError
from src.models import RawObservation
from src.net import http_get
from src.transform import heute, parse_german_number

log = logging.getLogger(__name__)

QUELLE_NAME = "Statistik der Bundesagentur für Arbeit"
QUELLE_URL_EINZELHEFT = (
    "https://statistik.arbeitsagentur.de (Arbeitslose und Arbeitslosenquoten, Gemeindeebene)"
)

#: Dateinamen-Muster der beiden XLSX in der ZIP → Kennzahl + Einheit
_ARBEITSLOSE_RE = re.compile(r"(?i)arbeitslose(?!nquote)")
_QUOTEN_RE = re.compile(r"(?i)quote")

#: WZ-2008-Abschnitte A–U; kpi_spec.yaml kann sie je Cluster mit
#: `wz_abschnitte:` überschreiben
WZ_ABSCHNITTE: dict[str, str] = {
    "A": "Land- und Forstwirtschaft, Fischerei",
    "B": "Bergbau und Gewinnung von Steinen und Erden",
    "C": "Verarbeitendes Gewerbe",
    "D": "Energieversorgung",
    "E": "Wasserversorgung; Abwasser- und Abfallentsorgung",
    "F": "Baugewerbe",
    "G": "Handel; Instandhaltung und Reparatur von Kraftfahrzeugen",
    "H": "Verkehr und Lagerei",
    "I": "Gastgewerbe",
    "J": "Information und Kommunikation",
    "K": "Erbringung von Finanz- und Versicherungsdienstleistungen",
    "L": "Grundstücks- und Wohnungswesen",
    "M": "Freiberufliche, wissenschaftliche und technische Dienstleistungen",
    "N": "Sonstige wirtschaftliche Dienstleistungen",
    "O": "Öffentliche Verwaltung, Verteidigung; Sozialversicherung",
    "P": "Erziehung und Unterricht",
    "Q": "Gesundheits- und Sozialwesen",
    "R": "Kunst, Unterhaltung und Erholung",
    "S": "Erbringung von sonstigen Dienstleistungen",
    "T": "Private Haushalte",
    "U": "Exterritoriale Organisationen und Körperschaften",
}


class ArbeitsagenturConnector(Connector):
    name: ClassVar[str] = "arbeitsagentur"
    phase: ClassVar[int] = 1
    # Branchenmix ist eine Sicht auf CLUSTER_SV_WZ (siehe README).
    clusters: ClassVar[tuple[str, ...]] = (
        CLUSTER_ARBEITSLOSIGKEIT,
        CLUSTER_SV_WZ,
        CLUSTER_BRANCHENMIX,
    )

    def __init__(self, settings):
        super().__init__(settings)
        # RS → {"Arbeitslose": (stichtag, wert), "Arbeitslosenquote": (stichtag, wert)}
        # Einmal pro Lauf gefüllt (die BA-ZIP ist bundesweit, ~23 MB).
        self._einzelheft_cache: dict[str, dict[str, tuple[str, float]]] | None = None

    def fetch_raw(self, region: Region) -> list[RawObservation]:
        observations: list[RawObservation] = []
        failures: list[str] = []
        skipped = 0

        teilquellen = (
            ("einzelheft", self._fetch_einzelheft),
            ("wz_csv", self._fetch_wz_csv),
        )
        for teilquelle, fn in teilquellen:
            try:
                observations.extend(fn(region))
            except NotConfiguredError as exc:
                skipped += 1
                log.info(
                    "BA-Teilquelle übersprungen (nicht konfiguriert)",
                    extra={"connector": self.name, "teilquelle": teilquelle,
                           "region": region.name, "grund": str(exc)},
                )
            except Exception as exc:  # noqa: BLE001 — Teilquellen-Isolierung
                failures.append(f"{teilquelle}: {exc}")
                log.warning(
                    "BA-Teilquelle fehlgeschlagen",
                    extra={"connector": self.name, "teilquelle": teilquelle,
                           "region": region.name},
                    exc_info=True,
                )

        if skipped == len(teilquellen):
            raise NotConfiguredError("Keine BA-Teilquelle lieferte Daten (siehe Logs)")
        if failures and not observations:
            raise ConnectorError(
                f"Alle BA-Teilquellen für {region.name} fehlgeschlagen: " + " | ".join(failures)
            )
        return observations

    # ---------------------------------- Arbeitslose + Quoten (bundesweite ZIP)

    def _fetch_einzelheft(self, region: Region) -> list[RawObservation]:
        if self._einzelheft_cache is None:
            self._einzelheft_cache = self._load_einzelheft_zip()

        eintrag = self._einzelheft_cache.get(region.regionalschluessel)
        if not eintrag:
            log.warning(
                "BA-Einzelheft: Regionalschlüssel nicht in der bundesweiten Datei",
                extra={"region": region.name, "rs": region.regionalschluessel},
            )
            return []

        stand = heute()
        observations: list[RawObservation] = []
        einheiten = {"Arbeitslose": "Anzahl", "Arbeitslosenquote": "%"}
        for kennzahl, (stichtag, wert) in eintrag.items():
            observations.append(
                RawObservation(
                    region=region.name,
                    regionalschluessel=region.regionalschluessel,
                    kpi_cluster=CLUSTER_ARBEITSLOSIGKEIT,
                    kennzahl=kennzahl,
                    jahr_stichtag=stichtag,
                    wert=wert,
                    einheit=einheiten[kennzahl],
                    quelle_name=QUELLE_NAME,
                    quelle_url=QUELLE_URL_EINZELHEFT,
                    stand_datum=stand,
                )
            )
        return observations

    def _load_einzelheft_zip(self) -> dict[str, dict[str, tuple[str, float]]]:
        """Lädt die bundesweite ZIP und parst beide XLSX (Bestand + Quoten)."""
        url = self.settings.ba_einzelheft_zip_url
        if not url:
            raise NotConfiguredError("BA_EINZELHEFT_ZIP_URL ist leer")
        resp = http_get(url, self.settings)
        cache: dict[str, dict[str, tuple[str, float]]] = {}
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            namen = zf.namelist()
            arbeitslose = next((n for n in namen if _ARBEITSLOSE_RE.search(n) and n.endswith(".xlsx")), None)
            quoten = next((n for n in namen if _QUOTEN_RE.search(n) and n.endswith(".xlsx")), None)
            if not arbeitslose or not quoten:
                raise SourceLayoutError(
                    f"BA-ZIP: erwartete XLSX (Arbeitslose/Quoten) nicht gefunden, enthalten: {namen}"
                )
            for name, kennzahl in ((arbeitslose, "Arbeitslose"), (quoten, "Arbeitslosenquote")):
                for rs, (stichtag, wert) in self._parse_uebersicht_kreise(zf.read(name)).items():
                    cache.setdefault(rs, {})[kennzahl] = (stichtag, wert)
        return cache

    @staticmethod
    def _parse_uebersicht_kreise(xlsx_bytes: bytes) -> dict[str, tuple[str, float]]:
        """Parst Blatt 'Übersicht_Kreise': RS → (Stichtag, neuester Monatswert).

        Layout: eine Kopfzeile trägt in den Datenspalten Datums-Werte (Monate),
        darunter je Region eine Zeile "<RS> <Name>". 'aktuell' = jüngster Monat
        mit gültigem Wert (Platzhalter/0 der noch nicht berichteten Monate werden
        von rechts übersprungen).
        """
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
        try:
            ws = wb["Übersicht_Kreise"] if "Übersicht_Kreise" in wb.sheetnames else wb.worksheets[0]
            rows = list(ws.iter_rows(values_only=True))
        finally:
            wb.close()

        # Kopfzeile finden: Zeile mit den meisten Datums-Zellen ab Spalte 3
        header_idx = -1
        best = 0
        for i, row in enumerate(rows):
            n = sum(1 for c in row[3:] if isinstance(c, datetime.datetime))
            if n > best:
                best, header_idx = n, i
        if header_idx < 0:
            raise SourceLayoutError(
                "BA-Übersicht_Kreise: keine Datums-Kopfzeile gefunden — Layout geändert?"
            )
        datum_spalten = {
            idx: cell for idx, cell in enumerate(rows[header_idx])
            if isinstance(cell, datetime.datetime)
        }

        rs_re = re.compile(r"^(\d{5})\b")
        ergebnis: dict[str, tuple[str, float]] = {}
        for row in rows[header_idx + 1:]:
            if not row or not isinstance(row[0], str):
                continue
            m = rs_re.match(row[0].strip())
            if not m:
                continue
            rs = m.group(1)
            # jüngste Datumsspalte mit gültigem, positivem Wert
            for idx in sorted(datum_spalten, reverse=True):
                if idx >= len(row):
                    continue
                try:
                    wert = parse_german_number(row[idx])
                except (ValueError, TypeError):
                    continue
                if wert <= 0:
                    continue
                stichtag = datum_spalten[idx].strftime("%Y-%m")
                ergebnis[rs] = (stichtag, wert)
                break
        if not ergebnis:
            raise SourceLayoutError(
                "BA-Übersicht_Kreise: keine Regionszeile (RS + Wert) gefunden — Layout geändert?"
            )
        return ergebnis

    # ------------------------------------------------- Beschäftigte nach WZ

    def _fetch_wz_csv(self, region: Region) -> list[RawObservation]:
        template = self.settings.ba_wz_csv_url_template
        if not template:
            raise NotConfiguredError("BA_WZ_CSV_URL_TEMPLATE nicht gesetzt (Branchen im Fokus)")
        url = template.format(rs=region.regionalschluessel)
        resp = http_get(url, self.settings)
        resp.encoding = resp.encoding or "utf-8"
        return self.wz_observations(resp.text, region, url)

    def wz_observations(
        self, csv_text: str, region: Region, url: str
    ) -> list[RawObservation]:
        """Parst den WZ-CSV-Export: Bestand je Abschnitt A–U + berechnete Anteile."""
        wz_abschnitte: dict[str, str] = (
            get_cluster_spec(CLUSTER_SV_WZ).get("wz_abschnitte") or WZ_ABSCHNITTE
        )

        delimiter = ";" if csv_text.count(";") >= csv_text.count(",") else ","
        reader = csv.reader(io.StringIO(csv_text), delimiter=delimiter)

        stichtag: str | None = None
        bestand: dict[str, float] = {}
        for row in reader:
            cells = [c.strip() for c in row]
            if stichtag is None:
                for cell in cells:
                    date_match = re.search(r"((?:19|20)\d{2})-(\d{2})(?:-(\d{2}))?", cell)
                    if not date_match:
                        de_match = re.search(r"(\d{1,2})\.(\d{1,2})\.((?:19|20)\d{2})", cell)
                        if de_match:
                            stichtag = (
                                f"{de_match.group(3)}-{int(de_match.group(2)):02d}"
                                f"-{int(de_match.group(1)):02d}"
                            )
                            break
                        continue
                    stichtag = date_match.group(0)
                    break
            if len(cells) < 2:
                continue
            abschnitt: str | None = None
            wert_cells: list[str] = []
            first = cells[0]
            if first.upper() in wz_abschnitte:
                abschnitt = first.upper()
                wert_cells = cells[1:]
            else:
                prefix_match = re.match(r"^([A-U])\s+\S", first)
                if prefix_match:
                    abschnitt = prefix_match.group(1)
                    wert_cells = cells[1:]
            if abschnitt is None:
                continue
            for cell in wert_cells:
                try:
                    bestand[abschnitt] = parse_german_number(cell)
                    break
                except ValueError:
                    continue

        if not bestand:
            raise SourceLayoutError(
                "BA-WZ-CSV: kein WZ-Abschnitt (A–U) mit Wert gefunden — "
                f"Exportformat prüfen ({url})"
            )
        gesamt = sum(bestand.values())
        if gesamt <= 0:
            raise SourceLayoutError(f"BA-WZ-CSV: Summe der Bestände ist {gesamt} ({url})")
        jahr_stichtag = stichtag or str(heute().year)

        stand = heute()
        observations: list[RawObservation] = []
        for abschnitt, wert in sorted(bestand.items()):
            gemeinsam = dict(
                region=region.name,
                regionalschluessel=region.regionalschluessel,
                kpi_cluster=CLUSTER_SV_WZ,
                jahr_stichtag=jahr_stichtag,
                quelle_name=QUELLE_NAME,
                quelle_url=url,
                stand_datum=stand,
            )
            observations.append(
                RawObservation(
                    kennzahl=f"SV-Beschäftigte WZ {abschnitt}",
                    wert=wert,
                    einheit="Anzahl",
                    **gemeinsam,
                )
            )
            observations.append(
                RawObservation(
                    kennzahl=f"Anteil SV-Beschäftigte WZ {abschnitt}",
                    wert=round(wert / gesamt * 100, 1),
                    einheit="%",
                    **gemeinsam,
                )
            )
        return observations
