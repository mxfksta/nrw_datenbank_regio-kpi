"""Konnektor Bundesagentur für Arbeit (statistik.arbeitsagentur.de).

Zwei Teilquellen:

- **Einzelheft "Arbeitslose und Arbeitslosenquoten (Gemeindeebene)"** (XLSX):
  Arbeitslose (Bestand) + Arbeitslosenquote → Cluster "Arbeitslosigkeit (BA)"
- **CSV-Export "Beschäftigte nach Wirtschaftszweigen (WZ 2008)"**:
  SV-Beschäftigte je WZ-Abschnitt A–U + Anteil (%) → Cluster
  "SV-Beschäftigte nach Wirtschaftszweigen" (der Cluster "Branchenmix" ist
  laut kpi_spec.yaml eine Sicht darauf und wird nicht doppelt materialisiert)

WICHTIG — Konfiguration: Die BA verlinkt ihre Downloads über Suchformulare
(Einzelheftsuche) bzw. interaktive Portale; es gibt keine dokumentiert
stabilen Download-URLs. Die konkreten URLs müssen daher einmalig ermittelt
und als ENV-Templates gesetzt werden (``{rs}`` wird ersetzt):

    BA_EINZELHEFT_URL_TEMPLATE   → XLSX Einzelheft je Region
    BA_WZ_CSV_URL_TEMPLATE       → CSV Beschäftigte nach WZ je Region

Einstieg zur Ermittlung:
https://statistik.arbeitsagentur.de/SiteGlobals/Forms/Suche/Einzelheftsuche_Formular.html?nn=27098&topic_f=gemeinde-arbeitslose-quoten

Ohne Konfiguration wird die jeweilige Teilquelle als SKIPPED übersprungen
(NotConfiguredError) — kein Fehler, aber deutlich geloggt. Die Parser sind
vollständig implementiert und gegen Fixtures getestet.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from typing import ClassVar

import openpyxl

from src.config import (
    CLUSTER_ARBEITSLOSIGKEIT,
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

#: Zeilen-Labels im Einzelheft-XLSX (bei Layout-Änderung hier anpassen)
EINZELHEFT_LABELS: dict[str, str] = {
    "Arbeitslose": r"(?i)^arbeitslose(\s+insgesamt)?$",
    "Arbeitslosenquote": r"(?i)^arbeitslosenquote",
}
EINZELHEFT_EINHEITEN: dict[str, str] = {
    "Arbeitslose": "Anzahl",
    "Arbeitslosenquote": "%",
}

_MONAT_RE = re.compile(
    r"(?i)(januar|februar|märz|april|mai|juni|juli|august|september|oktober|november|dezember)"
    r"\s+((?:19|20)\d{2})"
)
_MONAT_NR = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
}


class ArbeitsagenturConnector(Connector):
    name: ClassVar[str] = "arbeitsagentur"
    phase: ClassVar[int] = 1
    clusters: ClassVar[tuple[str, ...]] = (CLUSTER_ARBEITSLOSIGKEIT, CLUSTER_SV_WZ)

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
            raise NotConfiguredError(
                "BA_EINZELHEFT_URL_TEMPLATE / BA_WZ_CSV_URL_TEMPLATE nicht gesetzt (siehe README)"
            )
        if failures and not observations:
            raise ConnectorError(
                f"Alle BA-Teilquellen für {region.name} fehlgeschlagen: " + " | ".join(failures)
            )
        return observations

    # ------------------------------------------------------------- Einzelheft

    def _fetch_einzelheft(self, region: Region) -> list[RawObservation]:
        template = self.settings.ba_einzelheft_url_template
        if not template:
            raise NotConfiguredError("BA_EINZELHEFT_URL_TEMPLATE nicht gesetzt")
        url = template.format(rs=region.regionalschluessel)
        resp = http_get(url, self.settings)
        return self.einzelheft_observations(resp.content, region, url)

    def einzelheft_observations(
        self, xlsx_bytes: bytes, region: Region, url: str
    ) -> list[RawObservation]:
        """Scannt das Einzelheft-XLSX nach Arbeitslosen-Bestand und -Quote."""
        compiled = {k: re.compile(p) for k, p in EINZELHEFT_LABELS.items()}
        werte: dict[str, float] = {}
        stichtag: str | None = None

        workbook = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
        try:
            for sheet in workbook.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    cells = list(row)
                    for i, cell in enumerate(cells):
                        if not isinstance(cell, str):
                            continue
                        text = cell.strip()
                        if stichtag is None:
                            monat_match = _MONAT_RE.search(text)
                            if monat_match:
                                monat = _MONAT_NR[monat_match.group(1).lower()]
                                stichtag = f"{monat_match.group(2)}-{monat:02d}"
                        for kennzahl, pattern in compiled.items():
                            if kennzahl in werte or not pattern.search(text):
                                continue
                            for candidate in cells[i + 1:]:
                                if candidate is None:
                                    continue
                                try:
                                    werte[kennzahl] = parse_german_number(candidate)
                                    break
                                except ValueError:
                                    continue
        finally:
            workbook.close()

        if not werte:
            raise SourceLayoutError(
                "BA-Einzelheft: weder 'Arbeitslose' noch 'Arbeitslosenquote' gefunden — "
                f"Layout geändert? EINZELHEFT_LABELS prüfen ({url})"
            )
        if stichtag is None:
            raise SourceLayoutError(
                f"BA-Einzelheft: kein Berichtsmonat (z. B. 'Juni 2025') erkennbar ({url})"
            )

        stand = heute()
        return [
            RawObservation(
                region=region.name,
                regionalschluessel=region.regionalschluessel,
                kpi_cluster=CLUSTER_ARBEITSLOSIGKEIT,
                kennzahl=kennzahl,
                jahr_stichtag=stichtag,
                wert=wert,
                einheit=EINZELHEFT_EINHEITEN[kennzahl],
                quelle_name=QUELLE_NAME,
                quelle_url=url,
                stand_datum=stand,
            )
            for kennzahl, wert in werte.items()
        ]

    # ------------------------------------------------- Beschäftigte nach WZ

    def _fetch_wz_csv(self, region: Region) -> list[RawObservation]:
        template = self.settings.ba_wz_csv_url_template
        if not template:
            raise NotConfiguredError("BA_WZ_CSV_URL_TEMPLATE nicht gesetzt")
        url = template.format(rs=region.regionalschluessel)
        resp = http_get(url, self.settings)
        resp.encoding = resp.encoding or "utf-8"
        return self.wz_observations(resp.text, region, url)

    def wz_observations(
        self, csv_text: str, region: Region, url: str
    ) -> list[RawObservation]:
        """Parst den WZ-CSV-Export: Bestand je Abschnitt A–U + berechnete Anteile."""
        wz_abschnitte: dict[str, str] = get_cluster_spec(CLUSTER_SV_WZ)["wz_abschnitte"]

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
            # WZ-Abschnitt erkennen: eigene Spalte ("A") oder Prefix ("A Land- und ...")
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
