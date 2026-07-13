"""Konnektor statistik.nrw / IT.NRW.

Drei Teilquellen, in dieser Präferenzreihenfolge:

1. **Landesdatenbank NRW (GENESIS, ffcsv)** — bevorzugt, weil maschinenlesbar
   und layoutstabil. Aktiv, sobald Zugangsdaten (ENV) und Tabellencodes
   (``ldb:`` in kpi_spec.yaml) hinterlegt sind. Siehe landesdatenbank.py.
2. **Zensus-2022-XLSX** (Grundinfo Bevölkerung je Gemeinde/Kreis) — strukturiert,
   liefert den Demografie-Kern (Einwohner, Anteile weiblich/nichtdeutsch,
   Altersstruktur) zum Zensus-Stichtag 15.05.2022.
3. **Kommunalprofil-PDF** — Fallback und Quelle für die übrigen Cluster
   (Fläche, Einkommen, Umsatzsteuer, Pendler, Wohnen, Kaufkraft, Tourismus).
   PDF-Layouts können sich ändern → das Mapping läuft über die Konstanten
   ``KOMMUNALPROFIL_LABELS`` und wirft bei komplett abweichendem Layout einen
   ``SourceLayoutError`` mit klarer Meldung.

Fällt eine Teilquelle aus, liefern die anderen weiter (Teilquellen-Isolierung);
erst wenn ALLE Teilquellen scheitern, gilt der Konnektor für die Region als
fehlgeschlagen.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import ClassVar

import openpyxl
import pdfplumber

from src.config import (
    CLUSTER_DEMOGRAFIE,
    CLUSTER_EINKOMMEN,
    CLUSTER_KAUFKRAFT,
    CLUSTER_PENDLER,
    CLUSTER_TOURISMUS,
    CLUSTER_WIRTSCHAFT,
    CLUSTER_WOHNEN,
    KOMMUNALPROFIL_PDF_URL,
    ZENSUS_BEVOELKERUNG_XLSX_URL,
    Region,
    load_kpi_spec,
)
from src.connectors.base import (
    Connector,
    ConnectorError,
    NotConfiguredError,
    SourceLayoutError,
)
from src.connectors.landesdatenbank import (
    QUELLE_NAME as LDB_QUELLE_NAME,
    LandesdatenbankClient,
    parse_ffcsv,
)
from src.models import RawObservation
from src.net import http_get
from src.transform import heute, parse_german_number

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Zensus 2022 (XLSX)
# ---------------------------------------------------------------------------

ZENSUS_STICHTAG = "2022-05-15"  # amtlicher Zensus-Stichtag
ZENSUS_QUELLE_NAME = "Zensus 2022 (statistik.nrw Grundinfo Bevölkerung)"

#: interne Schlüssel → Regex auf Zeilen-Label im XLSX.
#: Erwartet werden ABSOLUTE Personenzahlen; Anteile werden hier berechnet.
#: Bei Layout-Änderungen der Datei: diese Muster anpassen.
ZENSUS_LABELS: dict[str, str] = {
    "insgesamt": r"(?i)^(bev(ö|oe)lkerung\s+)?insgesamt$|^personen\s+insgesamt$|^einwohner(zahl)?$",
    "weiblich": r"(?i)^weiblich$|^frauen$",
    "nichtdeutsch": r"(?i)^ausl(ä|ae)nder(/-?innen|innen)?$|^nichtdeutsche?$|ausl(ä|ae)ndische\s+bev(ö|oe)lkerung",
    "unter18": r"(?i)^unter\s*18(\s*jahren?)?$",
    "18bis29": r"(?i)^18\s*(bis|–|-)\s*(unter\s*)?(29|30)(\s*jahren?)?$",
    "30bis49": r"(?i)^30\s*(bis|–|-)\s*(unter\s*)?(49|50)(\s*jahren?)?$",
    "50bis64": r"(?i)^50\s*(bis|–|-)\s*(unter\s*)?(64|65)(\s*jahren?)?$",
    "65plus": r"(?i)^65(\s*jahre)?\s*(und|oder)\s*((ä|ae)lter|mehr)$|^65\s*\+$",
}

#: interner Schlüssel → kennzahl-Name laut kpi_spec.yaml (Anteile)
ZENSUS_ANTEIL_KENNZAHLEN: dict[str, str] = {
    "weiblich": "Anteil weiblich",
    "nichtdeutsch": "Anteil Nichtdeutsche",
    "unter18": "Anteil unter 18-Jährige",
    "18bis29": "Anteil 18- bis unter 30-Jährige",
    "30bis49": "Anteil 30- bis unter 50-Jährige",
    "50bis64": "Anteil 50- bis unter 65-Jährige",
    "65plus": "Anteil 65-Jährige und älter",
}

# ---------------------------------------------------------------------------
# Kommunalprofil (PDF)
# ---------------------------------------------------------------------------

KOMMUNALPROFIL_QUELLE_NAME = "statistik.nrw Kommunalprofil"

_YEAR_CELL_RE = re.compile(r"^(19|20)\d{2}$")
_YEAR_INLINE_RE = re.compile(r"(19|20)\d{2}")
_DATE_INLINE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.((?:19|20)\d{2})")


@dataclass(frozen=True)
class PdfLabel:
    """Mapping-Konstante: Zeilen-Label im Kommunalprofil → Kennzahl.

    ``pattern`` matcht den ANFANG der Label-Zelle (kein ``$``-Anker, damit
    Textzeilen wie "Fläche am 31.12.2023 78,85" greifen); die Wortgrenze wird
    zentral geprüft. ``exclude`` verwirft falsch-positive Zellen (z. B.
    "Bevölkerung je km²" beim Label "Bevölkerung").
    """

    kennzahl: str
    cluster: str
    einheit: str
    pattern: str
    exclude: str | None = None


#: Bei Layout-/Bezeichnungsänderungen im Kommunalprofil NUR diese Liste anpassen.
KOMMUNALPROFIL_LABELS: tuple[PdfLabel, ...] = (
    PdfLabel("Einwohner", CLUSTER_DEMOGRAFIE, "Anzahl",
             r"(?i)^bev(ö|oe)lkerung(sstand)?(\s+insgesamt)?(\s+am\s+31\.12\.(\d{4})?)?",
             exclude=r"(?i)je\s*km|dichte|entwicklung|prognose|vorausberechnung"),
    PdfLabel("Fläche", CLUSTER_DEMOGRAFIE, "km²",
             r"(?i)^(kataster|gebiets)?fl(ä|ae)che(\s+insgesamt)?(\s+am\s+31\.12\.(\d{4})?)?(\s+in\s+km²?)?",
             exclude=r"(?i)\bje\b|anteil"),
    PdfLabel("Einkommen je Einwohner", CLUSTER_EINKOMMEN, "€",
             r"(?i)^prim(ä|ae)reinkommen(\s+der\s+privaten\s+haushalte)?(\s+je\s+einwohner)?"),
    PdfLabel("Verfügbares Einkommen je Einwohner", CLUSTER_KAUFKRAFT, "€",
             r"(?i)^verf(ü|ue)gbares\s+einkommen(\s+der\s+privaten\s+haushalte)?(\s+je\s+einwohner)?"),
    PdfLabel("Lohn- und Einkommensteuer", CLUSTER_KAUFKRAFT, "Tsd. €",
             r"(?i)^lohn-?\s*und\s*einkommensteuer"),
    PdfLabel("Umsatzsteuerpflichtige", CLUSTER_WIRTSCHAFT, "Anzahl",
             r"(?i)^umsatzsteuerpflichtige|^steuerpflichtige\s*\(umsatzsteuer\)"),
    PdfLabel("Steuerbarer Umsatz", CLUSTER_WIRTSCHAFT, "Tsd. €",
             r"(?i)^steuerbarer\s+umsatz|^lieferungen\s+und\s+(sonstige\s+)?leistungen"),
    PdfLabel("Gewerbeanmeldungen", CLUSTER_WIRTSCHAFT, "Anzahl",
             r"(?i)^gewerbeanmeldungen"),
    PdfLabel("Gewerbeabmeldungen", CLUSTER_WIRTSCHAFT, "Anzahl",
             r"(?i)^gewerbeabmeldungen"),
    PdfLabel("Einpendler", CLUSTER_PENDLER, "Anzahl",
             r"(?i)^einpendler(\s*\((ü|ue)ber\s+gemeindegrenzen\))?"),
    PdfLabel("Auspendler", CLUSTER_PENDLER, "Anzahl",
             r"(?i)^auspendler(\s*\((ü|ue)ber\s+gemeindegrenzen\))?"),
    PdfLabel("Wohnungsbestand", CLUSTER_WOHNEN, "Anzahl",
             r"(?i)^wohnungsbestand|^wohnungen\s+insgesamt"),
    PdfLabel("Baufertigstellungen", CLUSTER_WOHNEN, "Anzahl Wohnungen",
             r"(?i)^baufertigstellungen|^fertiggestellte\s+wohnungen"),
    PdfLabel("Baugenehmigungen", CLUSTER_WOHNEN, "Anzahl Wohnungen",
             r"(?i)^baugenehmigungen|^genehmigte\s+wohnungen"),
    PdfLabel("Gästeankünfte", CLUSTER_TOURISMUS, "Anzahl",
             r"(?i)^g(ä|ae)steank(ü|ue)nfte|^ank(ü|ue)nfte(\s+von\s+g(ä|ae)sten)?"),
    PdfLabel("Übernachtungen", CLUSTER_TOURISMUS, "Anzahl",
             r"(?i)^(ü|ue)bernachtungen"),
    PdfLabel("Bettenkapazität", CLUSTER_TOURISMUS, "Anzahl",
             r"(?i)^(angebotene\s+)?(g(ä|ae)ste)?betten|^schlafgelegenheiten"),
)


def _label_match(pattern: re.Pattern, text: str) -> re.Match | None:
    """Pattern-Match mit Wortgrenzen-Guard: nach dem Treffer darf kein
    Buchstabe folgen ("Bevölkerung" darf nicht "Bevölkerungsdichte" matchen)."""
    match = pattern.search(text)
    if not match:
        return None
    rest = text[match.end():]
    if rest and (rest[0].isalpha() or rest[0] == "-"):
        return None
    return match


class StatistikNrwConnector(Connector):
    name: ClassVar[str] = "statistik_nrw"
    phase: ClassVar[int] = 1
    clusters: ClassVar[tuple[str, ...]] = (
        CLUSTER_DEMOGRAFIE,
        CLUSTER_EINKOMMEN,
        CLUSTER_WIRTSCHAFT,
        CLUSTER_PENDLER,
        CLUSTER_WOHNEN,
        CLUSTER_KAUFKRAFT,
        CLUSTER_TOURISMUS,
    )

    # ------------------------------------------------------------------ API

    def fetch_raw(self, region: Region) -> list[RawObservation]:
        observations: list[RawObservation] = []
        failures: list[str] = []

        teilquellen = (
            ("landesdatenbank", self._fetch_landesdatenbank),
            ("zensus", self._fetch_zensus),
            ("kommunalprofil", self._fetch_kommunalprofil),
        )
        for teilquelle, fn in teilquellen:
            try:
                neue = fn(region)
                observations.extend(neue)
                log.info(
                    "Teilquelle geladen",
                    extra={"connector": self.name, "teilquelle": teilquelle,
                           "region": region.name, "n_observations": len(neue)},
                )
            except NotConfiguredError as exc:
                log.info(
                    "Teilquelle übersprungen (nicht konfiguriert)",
                    extra={"connector": self.name, "teilquelle": teilquelle,
                           "region": region.name, "grund": str(exc)},
                )
            except Exception as exc:  # noqa: BLE001 — Teilquellen-Isolierung
                failures.append(f"{teilquelle}: {exc}")
                log.warning(
                    "Teilquelle fehlgeschlagen",
                    extra={"connector": self.name, "teilquelle": teilquelle,
                           "region": region.name},
                    exc_info=True,
                )

        if failures and not observations:
            raise ConnectorError(
                f"Alle Teilquellen für {region.name} fehlgeschlagen: " + " | ".join(failures)
            )

        observations.extend(self._derive_pendlersaldo(observations, region))
        return observations

    # --------------------------------------------------- Landesdatenbank NRW

    def _fetch_landesdatenbank(self, region: Region) -> list[RawObservation]:
        """Bevorzugter Pfad: GENESIS-ffcsv je konfigurierter Kennzahl."""
        spec_entries: list[tuple[str, dict]] = []
        for cluster in load_kpi_spec()["clusters"]:
            if cluster.get("connector") != self.name:
                continue
            for kennzahl in cluster.get("kennzahlen") or []:
                if isinstance(kennzahl, dict) and "ldb" in kennzahl:
                    spec_entries.append((cluster["name"], kennzahl))
        if not spec_entries:
            raise NotConfiguredError(
                "Keine 'ldb:'-Tabellencodes in kpi_spec.yaml hinterlegt "
                "(bevorzugter Pfad; siehe README, Abschnitt Landesdatenbank)"
            )
        client = LandesdatenbankClient(self.settings)
        if not client.is_configured():
            raise NotConfiguredError("LDB_NRW_USER/LDB_NRW_PASS nicht gesetzt")

        stand = heute()
        observations: list[RawObservation] = []
        for cluster_name, kennzahl_spec in spec_entries:
            ldb = kennzahl_spec["ldb"]
            text = client.fetch_tablefile(ldb["tabelle"], region.regionalschluessel)
            for value in parse_ffcsv(text):
                if ldb.get("inhalt") and not value.inhalt.startswith(ldb["inhalt"]):
                    continue
                filters: dict = ldb.get("auspraegungen") or {}
                if any(value.merkmale.get(m) != a for m, a in filters.items()):
                    continue
                observations.append(
                    RawObservation(
                        region=region.name,
                        regionalschluessel=region.regionalschluessel,
                        kpi_cluster=cluster_name,
                        kennzahl=kennzahl_spec["kennzahl"],
                        jahr_stichtag=value.zeit,
                        wert=value.wert,
                        einheit=kennzahl_spec["einheit"],
                        quelle_name=LDB_QUELLE_NAME,
                        quelle_url=f"https://www.landesdatenbank.nrw.de (Tabelle {ldb['tabelle']})",
                        stand_datum=stand,
                    )
                )
        return observations

    # ------------------------------------------------------------ Zensus 2022

    def _fetch_zensus(self, region: Region) -> list[RawObservation]:
        url = ZENSUS_BEVOELKERUNG_XLSX_URL.format(rs=region.regionalschluessel)
        resp = http_get(url, self.settings)
        return self.zensus_observations(resp.content, region, url)

    def zensus_observations(
        self, xlsx_bytes: bytes, region: Region, url: str
    ) -> list[RawObservation]:
        """Parst das Zensus-Grundinfo-XLSX (absolute Zahlen → Anteile berechnet)."""
        werte = self._zensus_scan(xlsx_bytes)

        if "insgesamt" not in werte:
            raise SourceLayoutError(
                "Zensus-XLSX: Zeile 'Insgesamt' (Gesamtbevölkerung) nicht gefunden — "
                f"Layout geändert? ZENSUS_LABELS prüfen ({url})"
            )
        insgesamt = werte["insgesamt"]
        if insgesamt <= 0:
            raise SourceLayoutError(f"Zensus-XLSX: unplausible Gesamtbevölkerung {insgesamt}")

        stand = heute()

        def obs(kennzahl: str, wert: float, einheit: str) -> RawObservation:
            return RawObservation(
                region=region.name,
                regionalschluessel=region.regionalschluessel,
                kpi_cluster=CLUSTER_DEMOGRAFIE,
                kennzahl=kennzahl,
                jahr_stichtag=ZENSUS_STICHTAG,
                wert=wert,
                einheit=einheit,
                quelle_name=ZENSUS_QUELLE_NAME,
                quelle_url=url,
                stand_datum=stand,
            )

        observations = [obs("Einwohner", insgesamt, "Anzahl")]
        for key, kennzahl in ZENSUS_ANTEIL_KENNZAHLEN.items():
            if key not in werte:
                log.warning(
                    "Zensus-XLSX: Label nicht gefunden — ZENSUS_LABELS prüfen",
                    extra={"label": key, "region": region.name},
                )
                continue
            observations.append(obs(kennzahl, round(werte[key] / insgesamt * 100, 1), "%"))
        return observations

    @staticmethod
    def _zensus_scan(xlsx_bytes: bytes) -> dict[str, float]:
        """Scannt alle Sheets nach den ZENSUS_LABELS und liest den ersten
        numerischen Wert rechts vom Label (erster Treffer je Label gewinnt)."""
        compiled = {key: re.compile(pattern) for key, pattern in ZENSUS_LABELS.items()}
        werte: dict[str, float] = {}
        workbook = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
        try:
            for sheet in workbook.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    cells = list(row)
                    for i, cell in enumerate(cells):
                        if not isinstance(cell, str):
                            continue
                        label_text = cell.strip()
                        for key, pattern in compiled.items():
                            if key in werte or not pattern.search(label_text):
                                continue
                            for candidate in cells[i + 1:]:
                                if candidate is None:
                                    continue
                                try:
                                    werte[key] = parse_german_number(candidate)
                                    break
                                except ValueError:
                                    continue
        finally:
            workbook.close()
        return werte

    # -------------------------------------------------------- Kommunalprofil

    def _fetch_kommunalprofil(self, region: Region) -> list[RawObservation]:
        url = KOMMUNALPROFIL_PDF_URL.format(rs=region.regionalschluessel)
        resp = http_get(url, self.settings)
        rows = self._rows_from_pdf(resp.content)
        return self.kommunalprofil_observations(rows, region, url)

    @staticmethod
    def _rows_from_pdf(pdf_bytes: bytes) -> list[list[str]]:
        """Extrahiert Tabellenzeilen UND Textzeilen (als Ein-Zellen-Zeilen)."""
        rows: list[list[str]] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        rows.append([(c or "").strip() for c in row])
                text = page.extract_text() or ""
                rows.extend([[line.strip()] for line in text.splitlines() if line.strip()])
        return rows

    def kommunalprofil_observations(
        self, rows: list[list[str]], region: Region, url: str
    ) -> list[RawObservation]:
        """Mappt extrahierte Zeilen defensiv auf Kennzahlen.

        Unterstützte Layout-Muster:

        - Jahres-Kopfzeile ("Merkmal | 2021 | 2022 | 2023") + Label-Zeile mit
          gleich vielen Werten → mehrjährige Zeitreihe (positionsbasiert)
        - Datum/Jahr im Label ("... am 31.12.2023") + ein Wert
        - (Jahr, Wert)-Paare in den Zellen der Label-Zeile
        - Textzeile mit Label und Wert in einer Zelle ("Fläche ... 78,85")

        Nicht eindeutig zuordenbare Zeilen werden übersprungen (DEBUG-Log).
        Matcht GAR KEIN Label, ist das Layout grundlegend anders →
        ``SourceLayoutError``.
        """
        compiled = [(label, re.compile(label.pattern),
                     re.compile(label.exclude) if label.exclude else None)
                    for label in KOMMUNALPROFIL_LABELS]
        matched_labels: set[str] = set()
        stand = heute()
        observations: list[RawObservation] = []
        current_years: list[int] = []

        for row in rows:
            cells = [c.strip() for c in row]
            non_empty = [c for c in cells if c]
            if not non_empty:
                continue

            # 1) Label suchen (erste matchende Zelle; bei Textzeilen = ganze Zeile)
            hit: tuple[PdfLabel, int] | None = None
            for label, pattern, exclude in compiled:
                for i, cell in enumerate(cells):
                    if not cell or _label_match(pattern, cell) is None:
                        continue
                    if exclude and exclude.search(cell):
                        continue
                    hit = (label, i)
                    break
                if hit:
                    break

            if hit is None:
                # 2) Jahres-Kopfzeile: >= 2 reine Jahreszahlen, höchstens eine
                #    weitere Zelle (z. B. "Merkmal")
                year_cells = [int(c) for c in non_empty if _YEAR_CELL_RE.fullmatch(c)]
                if len(year_cells) >= 2 and len(non_empty) - len(year_cells) <= 1:
                    current_years = year_cells
                continue

            label, label_idx = hit
            label_cell = cells[label_idx]

            # Werte rechts vom Label einsammeln; reine Jahreszahlen separat merken
            inline_years: list[int] = []
            values: list[float] = []
            for cell in cells[label_idx + 1:]:
                if not cell:
                    continue
                if _YEAR_CELL_RE.fullmatch(cell):
                    inline_years.append(int(cell))
                    continue
                try:
                    values.append(parse_german_number(cell))
                except ValueError:
                    continue

            # Textzeilen ("Fläche am 31.12.2023 78,85") haben Label und Wert in
            # EINER Zelle → Resttext hinter dem Label auswerten
            if not values and not inline_years and label_idx == len(cells) - 1:
                match = _label_match(re.compile(label.pattern), label_cell)
                rest = label_cell[match.end():] if match else ""
                for token in rest.split():
                    if _YEAR_CELL_RE.fullmatch(token):
                        inline_years.append(int(token))
                        continue
                    try:
                        values.append(parse_german_number(token))
                    except ValueError:
                        continue

            # Label-Zeile, die nur Jahre trägt → als Jahres-Kopfzeile werten
            if not values and len(inline_years) >= 2:
                current_years = inline_years
                continue
            if not values:
                log.debug("Label ohne Wert übersprungen",
                          extra={"kennzahl": label.kennzahl, "row": cells})
                continue

            # Stichtag bestimmen: Datum im Label > Jahr im Label > Zellen-Jahre > Kopfzeile
            pairs: list[tuple[str, float]] = []
            date_match = _DATE_INLINE_RE.search(label_cell)
            label_year_match = _YEAR_INLINE_RE.search(label_cell)
            if date_match:
                tag, monat, jahr = date_match.group(1), date_match.group(2), date_match.group(3)
                pairs = [(f"{jahr}-{int(monat):02d}-{int(tag):02d}", values[0])]
            elif label_year_match:
                pairs = [(label_year_match.group(0), values[0])]
            elif inline_years and len(inline_years) == len(values):
                pairs = [(str(y), v) for y, v in zip(inline_years, values)]
            elif current_years and len(values) == len(current_years):
                pairs = [(str(y), v) for y, v in zip(current_years, values)]
            else:
                log.debug(
                    "Zeile nicht eindeutig zuordenbar (Jahre/Werte passen nicht)",
                    extra={"kennzahl": label.kennzahl, "row": cells,
                           "kopfzeilen_jahre": current_years},
                )
                continue

            matched_labels.add(label.kennzahl)
            for jahr_stichtag, wert in pairs:
                observations.append(
                    RawObservation(
                        region=region.name,
                        regionalschluessel=region.regionalschluessel,
                        kpi_cluster=label.cluster,
                        kennzahl=label.kennzahl,
                        jahr_stichtag=jahr_stichtag,
                        wert=wert,
                        einheit=label.einheit,
                        quelle_name=KOMMUNALPROFIL_QUELLE_NAME,
                        quelle_url=url,
                        stand_datum=stand,
                    )
                )

        if not matched_labels:
            raise SourceLayoutError(
                "Kommunalprofil-PDF: KEIN bekanntes Label gefunden — Layout hat sich "
                f"grundlegend geändert, KOMMUNALPROFIL_LABELS prüfen ({url})"
            )
        missing = {label.kennzahl for label in KOMMUNALPROFIL_LABELS} - matched_labels
        if missing:
            log.info(
                "Kommunalprofil: nicht alle Labels gefunden",
                extra={"region": region.name, "fehlend": sorted(missing)},
            )
        return observations

    # ------------------------------------------------------------ Ableitungen

    @staticmethod
    def _derive_pendlersaldo(
        observations: list[RawObservation], region: Region
    ) -> list[RawObservation]:
        """Pendlersaldo = Einpendler − Auspendler, je Jahr mit beiden Werten."""
        einpendler = {o.jahr_stichtag: o for o in observations if o.kennzahl == "Einpendler"}
        auspendler = {o.jahr_stichtag: o for o in observations if o.kennzahl == "Auspendler"}
        saldi: list[RawObservation] = []
        for jahr, ein in einpendler.items():
            aus = auspendler.get(jahr)
            if aus is None:
                continue
            saldi.append(
                RawObservation(
                    region=region.name,
                    regionalschluessel=region.regionalschluessel,
                    kpi_cluster=CLUSTER_PENDLER,
                    kennzahl="Pendlersaldo",
                    jahr_stichtag=jahr,
                    wert=ein.wert - aus.wert,
                    einheit="Anzahl",
                    quelle_name=ein.quelle_name,
                    quelle_url=ein.quelle_url,
                    stand_datum=ein.stand_datum,
                )
            )
        return saldi
