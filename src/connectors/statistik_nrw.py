"""Konnektor statistik.nrw / IT.NRW.

Drei Teilquellen, in dieser Präferenzreihenfolge:

1. **Landesdatenbank NRW (GENESIS, ffcsv)** — bevorzugt, weil maschinenlesbar
   und layoutstabil. Aktiv, sobald Zugangsdaten (ENV) und Tabellencodes
   (``ldb:`` in kpi_spec.yaml) hinterlegt sind. Siehe landesdatenbank.py.
2. **Zensus-2022-XLSX** (Grundinfo Bevölkerung) — nur für kreisfreie Städte
   verfügbar (Dateischema ``{rs}000_…`` ist ein Gemeindeschlüssel); dient als
   Basiswert/Gegenprobe für Einwohner und Anteile (Stichtag 15.05.2022).
3. **Kommunalprofil-PDF** (``l{rs}.pdf``) — Hauptquelle, für alle 7 Regionen
   verfügbar. IT.NRW generiert alle Profile aus demselben Template; der Parser
   arbeitet daher mit gezielten Block-Extraktoren auf den extrahierten
   Textzeilen (pdfplumber liefert den Text OHNE Leerzeichen innerhalb der
   Wörter, z. B. "Flächeinsgesamt 7887" — die Regexe sind darauf ausgelegt).

Im Kommunalprofil NICHT enthalten (Stand Template 2025/2026): Wohnungsbestand,
Baugenehmigungen/-fertigstellungen, Tourismus — diese Kennzahlen erfordern die
Landesdatenbank (siehe README, Offene Punkte).

Fällt eine Teilquelle aus, liefern die anderen weiter (Teilquellen-Isolierung);
erst wenn ALLE Teilquellen scheitern, gilt der Konnektor für die Region als
fehlgeschlagen.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Callable, ClassVar

import openpyxl
import pdfplumber

from src.config import (
    CLUSTER_BILDUNG,
    CLUSTER_BRANCHENMIX,
    CLUSTER_DEMOGRAFIE,
    CLUSTER_EINKOMMEN,
    CLUSTER_KAUFKRAFT,
    CLUSTER_PENDLER,
    CLUSTER_SV_WZ,
    CLUSTER_TOURISMUS,
    CLUSTER_WIRTSCHAFT,
    CLUSTER_WOHNEN,
    KOMMUNALPROFIL_PDF_URL,
    WZ_ABSCHNITT_LABELS,
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

ZENSUS_STICHTAG = "2022-05-15"  # amtlicher Zensus-Stichtag
ZENSUS_QUELLE_NAME = "Zensus 2022 (statistik.nrw Grundinfo Bevölkerung)"
KOMMUNALPROFIL_QUELLE_NAME = "statistik.nrw Kommunalprofil (IT.NRW)"

# --- SV-Beschäftigte nach Wirtschaftszweigen (Landesdatenbank 13111-50i) ---
SV_WZ_TABELLE = "13111-50i"
SV_WZ_INHALT = "ERW032"        # Wertspalte: SV-pflichtig Beschäftigte
SV_WZ_KLASSIFIKATION = "WZ08S3"  # Merkmal: WZ-2008-Abschnitte
#: reine Einzelabschnitte "WZ08-A".."WZ08-U" (Aggregate wie "WZ08-B-05" und die
#: Insgesamt-Zeile "WZ08-A-U" matchen NICHT)
_WZ08_SECTION_RE = re.compile(r"^WZ08-([A-U])$")

#: Kommunalprofil-Schultabelle: Kopf-Token-Präfix → Schulform-Label.
#: Reihenfolge/Spalten werden aus der Kopfzeile gelesen (regionsrobust).
SCHULFORM_PREFIXE: dict[str, str] = {
    "Ins": "Insgesamt",
    "Grund": "Grundschule",
    "Haupt": "Hauptschule",
    "Real": "Realschule",
    "Gesamt": "Gesamtschule",
    "Sekundar": "Sekundarschule",
    "Gemeinschaft": "Gemeinschaftsschule",
    "Gymna": "Gymnasium",
    "Förder": "Förderschule",
    "Berufs": "Berufskolleg",
    "Weiterbild": "Weiterbildungskolleg",
    "Freie": "Freie Waldorfschule",
    "Volks": "Volksschule",
}


def _schulform(token: str) -> str | None:
    """Kopf-Token (z. B. "Grund-") → Schulform-Label, oder None."""
    wort = re.sub(r"[^A-Za-zÄÖÜäöü]", "", token)
    for praefix, label in SCHULFORM_PREFIXE.items():
        if wort.startswith(praefix):
            return label
    return None

#: Platzhalter in IT.NRW-Tabellen (DIN 55301): kein verwertbarer Zahlenwert
_PLATZHALTER = {"x", "X", "–", "-", ".", "/", "…"}

_JAHR_RE = re.compile(r"^(19|20)\d{2}$")


def _zahlen_tokens(tokens: list[str]) -> list[float]:
    """Parst alle numerischen Tokens einer Zeile; Platzhalter werden übersprungen."""
    werte: list[float] = []
    for tok in tokens:
        if tok in _PLATZHALTER:
            continue
        try:
            werte.append(parse_german_number(tok))
        except ValueError:
            continue
    return werte


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
        CLUSTER_SV_WZ,
        CLUSTER_BRANCHENMIX,  # Sicht auf SV-WZ (nicht separat materialisiert)
        CLUSTER_BILDUNG,
    )

    def __init__(self, settings):
        super().__init__(settings)
        # SV-WZ-Tabelle enthält alle Regionen → pro Lauf einmal laden (Instanz-Cache)
        self._sv_wz_werte = None

    # ------------------------------------------------------------------ API

    def fetch_raw(self, region: Region) -> list[RawObservation]:
        observations: list[RawObservation] = []
        failures: list[str] = []

        teilquellen = (
            ("landesdatenbank", self._fetch_landesdatenbank),
            ("sv_wz", self._fetch_sv_wz),
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
                    "Teilquelle übersprungen",
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
            if cluster["name"] not in self.clusters:
                continue
            # Kennzahlen sind in der Spec einfache Strings (Doku) ODER Dicts —
            # nur Dicts mit `ldb:`-Block sind maschinell abrufbar konfiguriert.
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
        # ffcsv je (Tabelle, Regionalvariable) nur EINMAL laden — mehrere
        # Kennzahlen teilen sich oft eine Tabelle (z. B. Ankünfte + Übernachtungen
        # aus 45412-04i); die Extraktionen sind teuer.
        tabellen_cache: dict[tuple[str, str], str] = {}
        for cluster_name, kennzahl_spec in spec_entries:
            ldb = kennzahl_spec["ldb"]
            regionalvariable = ldb.get("regionalvariable", "")
            cache_key = (ldb["tabelle"], regionalvariable)
            if cache_key not in tabellen_cache:
                tabellen_cache[cache_key] = client.fetch_tablefile(
                    ldb["tabelle"], region.regionalschluessel, regionalvariable
                )
            for value in self._select_ldb_werte(
                parse_ffcsv(tabellen_cache[cache_key]),
                inhalt=ldb.get("inhalt", ""),
                rs=region.regionalschluessel,
            ):
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
                        quelle_url=f"https://landesdatenbank.nrw.de (Tabelle {ldb['tabelle']})",
                        stand_datum=stand,
                    )
                )
        return observations

    @staticmethod
    def _select_ldb_werte(werte, *, inhalt, rs):
        """Wählt aus einer ffcsv-Tabelle die passenden Gesamt-Werte einer Region.

        Kriterien (ffcsv-2020):
        - Messgröße == ``inhalt`` (value_variable_code, z. B. "GAST01")
        - Einheit ist NICHT ``%`` (schließt „Veränderung zum Vorjahr"-Zeilen aus)
        - Region: ein Merkmal-Attribut == ``rs`` (5-stellig, Ebene KREISE)
        - „Insgesamt": alle ÜBRIGEN Merkmal-Attribute leer (keine Aufteilung nach
          Kontinent/Räumen o. Ä.)
        """
        ausgewaehlt = []
        for v in werte:
            if inhalt and v.inhalt != inhalt:
                continue
            if v.einheit.strip() == "%":
                continue
            attribute = list(v.merkmale.values())
            if rs not in attribute:
                continue
            if any(a for a in attribute if a and a != rs):
                continue  # es gibt eine Aufteilung → nicht der Insgesamt-Wert
            ausgewaehlt.append(v)
        return ausgewaehlt

    # ------------------------------- SV-Beschäftigte nach Wirtschaftszweigen

    def _fetch_sv_wz(self, region: Region) -> list[RawObservation]:
        """SV-Beschäftigte je WZ-Abschnitt A–U + Anteile (Landesdatenbank 13111-50i).

        Anders als der generische LDB-Pfad werden hier NICHT die Insgesamt-Zeilen
        gewählt, sondern je WZ-Abschnitt der Bestand (Geschlecht = Insgesamt) plus
        der berechnete Anteil an der Abschnitts-Summe (WZ08-A-U). Die Tabelle
        enthält alle Regionen → einmal pro Lauf laden (Instanz-Cache).
        """
        client = LandesdatenbankClient(self.settings)
        if not client.is_configured():
            raise NotConfiguredError("LDB_NRW_USER/LDB_NRW_PASS nicht gesetzt")
        if self._sv_wz_werte is None:
            text = client.fetch_tablefile(SV_WZ_TABELLE, region.regionalschluessel, "GEMEIN")
            self._sv_wz_werte = parse_ffcsv(text)

        rs = region.regionalschluessel

        def ist_region_insgesamt(v) -> bool:
            # Messgröße SV-Beschäftigte, keine %-Zeile, Region == rs, und alle
            # Dimensionen außer WZ08S3 (z. B. Geschlecht) auf „Insgesamt" (leer).
            if v.inhalt != SV_WZ_INHALT or v.einheit.strip() == "%":
                return False
            if rs not in v.merkmale.values():
                return False
            for code, attr in v.merkmale.items():
                if code == SV_WZ_KLASSIFIKATION or attr == rs or not attr:
                    continue
                return False
            return True

        relevant = [v for v in self._sv_wz_werte if ist_region_insgesamt(v)]
        if not relevant:
            log.warning("SV-WZ: keine Werte für Region", extra={"rs": rs})
            return []

        stand = heute()
        observations: list[RawObservation] = []
        for jahr in {v.zeit for v in relevant}:
            # reine Einzelabschnitte A–U (Aggregat-Codes wie WZ08-B-05 fallen raus)
            sektionen = [
                (match.group(1), v.wert)
                for v in relevant
                if v.zeit == jahr
                and (match := _WZ08_SECTION_RE.match(str(v.merkmale.get(SV_WZ_KLASSIFIKATION, ""))))
            ]
            # Nenner der Anteile: Summe der Abschnitte (robust, ergibt 100 %; die
            # Insgesamt-Zeile WZ08-A-U ist je nach Abruf nicht verlässlich präsent).
            summe = sum(w for _, w in sektionen) or None
            for abschnitt, wert in sektionen:
                gemeinsam = dict(
                    region=region.name,
                    regionalschluessel=rs,
                    kpi_cluster=CLUSTER_SV_WZ,
                    jahr_stichtag=jahr,
                    quelle_name=LDB_QUELLE_NAME,
                    quelle_url=(
                        f"https://landesdatenbank.nrw.de (Tabelle {SV_WZ_TABELLE}, "
                        f"WZ {abschnitt}: {WZ_ABSCHNITT_LABELS.get(abschnitt, '')})"
                    ),
                    stand_datum=stand,
                )
                observations.append(
                    RawObservation(
                        kennzahl=f"SV-Beschäftigte WZ {abschnitt}",
                        wert=wert, einheit="Anzahl", **gemeinsam,
                    )
                )
                if summe:
                    observations.append(
                        RawObservation(
                            kennzahl=f"Anteil SV-Beschäftigte WZ {abschnitt}",
                            wert=round(wert / summe * 100, 1), einheit="%", **gemeinsam,
                        )
                    )
        return observations

    # ------------------------------------------------------------ Zensus 2022

    def _fetch_zensus(self, region: Region) -> list[RawObservation]:
        if "stadt" not in region.typ.lower():
            raise NotConfiguredError(
                "Zensus-Gemeindedatei existiert nur für kreisfreie Städte "
                "(Kreise: Demografie kommt aus dem Kommunalprofil)"
            )
        url = ZENSUS_BEVOELKERUNG_XLSX_URL.format(rs=region.regionalschluessel)
        resp = http_get(url, self.settings)
        return self.zensus_observations(resp.content, region, url)

    def zensus_observations(
        self, xlsx_bytes: bytes, region: Region, url: str
    ) -> list[RawObservation]:
        """Parst das Zensus-Grundinfo-XLSX (spaltenbasiertes Layout).

        Erwartete Struktur (alle Blätter werden gescannt, erster Treffer zählt):

        - Zeile "Bevölkerung insgesamt": erster numerischer Wert = Einwohner
        - Kopfzeile mit "männlich" + "weiblich" definiert die weiblich-Spalte;
          der Wert steht in der "Bevölkerung insgesamt"-Zeile desselben Blatts
        - analog Kopfzeile mit "Deutsche" + "Ausländer/-innen"

        Anteile werden hier berechnet. Altersstruktur wird bewusst NICHT aus
        dem Zensus gezogen — die kanonischen Altersgruppen kommen für alle
        Regionen einheitlich aus dem Kommunalprofil (neuerer Stichtag).
        """
        insgesamt: float | None = None
        weiblich: float | None = None
        auslaender: float | None = None

        workbook = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
        try:
            for sheet in workbook.worksheets:
                weiblich_col: int | None = None
                auslaender_col: int | None = None
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(c).strip() if c is not None else "" for c in row]
                    lower = [c.lower() for c in cells]
                    if "männlich" in lower and "weiblich" in lower:
                        weiblich_col = lower.index("weiblich")
                    if "deutsche" in lower and any(c.startswith("ausländer") for c in lower):
                        auslaender_col = next(
                            i for i, c in enumerate(lower) if c.startswith("ausländer")
                        )
                    if not cells or not re.match(r"(?i)^bevölkerung\s+insgesamt$", cells[0]):
                        continue
                    zahlen = _zahlen_tokens(cells[1:])
                    if insgesamt is None and zahlen:
                        insgesamt = zahlen[0]
                    if weiblich is None and weiblich_col is not None and len(cells) > weiblich_col:
                        try:
                            weiblich = parse_german_number(cells[weiblich_col])
                        except ValueError:
                            pass
                    if auslaender is None and auslaender_col is not None and len(cells) > auslaender_col:
                        try:
                            auslaender = parse_german_number(cells[auslaender_col])
                        except ValueError:
                            pass
        finally:
            workbook.close()

        if insgesamt is None or insgesamt <= 0:
            raise SourceLayoutError(
                "Zensus-XLSX: Zeile 'Bevölkerung insgesamt' nicht gefunden — "
                f"Layout geändert? Parser in statistik_nrw.py prüfen ({url})"
            )

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
        if weiblich is not None:
            observations.append(obs("Anteil weiblich", round(weiblich / insgesamt * 100, 1), "%"))
        if auslaender is not None:
            observations.append(
                obs("Anteil Nichtdeutsche", round(auslaender / insgesamt * 100, 1), "%")
            )
        return observations

    # -------------------------------------------------------- Kommunalprofil

    def _fetch_kommunalprofil(self, region: Region) -> list[RawObservation]:
        url = KOMMUNALPROFIL_PDF_URL.format(rs=region.regionalschluessel)
        resp = http_get(url, self.settings)
        lines = self._lines_from_pdf(resp.content)
        return self.kommunalprofil_observations(lines, region, url)

    @staticmethod
    def _lines_from_pdf(pdf_bytes: bytes) -> list[str]:
        lines: list[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                lines.extend(line.strip() for line in text.splitlines() if line.strip())
        return lines

    def kommunalprofil_observations(
        self, lines: list[str], region: Region, url: str
    ) -> list[RawObservation]:
        """Extrahiert die KPI-Blöcke aus den Textzeilen des Kommunalprofils.

        Jeder Block-Extraktor sucht seinen Abschnitts-Titel (enthält das
        Berichtsjahr) und liest die zugehörigen Datenzeilen. Liefert ein Block
        nichts, wird das geloggt (INFO) — erst wenn ALLE Blöcke leer sind, gilt
        das Layout als grundlegend geändert (SourceLayoutError).
        """
        stand = heute()

        def obs(cluster: str, kennzahl: str, jahr: str, wert: float, einheit: str) -> RawObservation:
            return RawObservation(
                region=region.name,
                regionalschluessel=region.regionalschluessel,
                kpi_cluster=cluster,
                kennzahl=kennzahl,
                jahr_stichtag=jahr,
                wert=wert,
                einheit=einheit,
                quelle_name=KOMMUNALPROFIL_QUELLE_NAME,
                quelle_url=url,
                stand_datum=stand,
            )

        extractors: list[tuple[str, Callable]] = [
            ("flaeche", self._kp_flaeche),
            ("bevoelkerung_serie", self._kp_bevoelkerung_serie),
            ("bevoelkerungsstruktur", self._kp_bevoelkerungsstruktur),
            ("pendler", self._kp_pendler),
            ("gewerbe", self._kp_gewerbe),
            ("umsatzsteuer", self._kp_umsatzsteuer),
            ("einkommen", self._kp_einkommen),
            ("schulen", self._kp_schulen),
        ]

        observations: list[RawObservation] = []
        leere_bloecke: list[str] = []
        for block_name, extractor in extractors:
            neue = extractor(lines, obs)
            if neue:
                observations.extend(neue)
            else:
                leere_bloecke.append(block_name)

        if not observations:
            raise SourceLayoutError(
                "Kommunalprofil-PDF: kein einziger Datenblock gefunden — Template "
                f"grundlegend geändert? Extraktoren in statistik_nrw.py prüfen ({url})"
            )
        if leere_bloecke:
            log.info(
                "Kommunalprofil: Blöcke ohne Treffer",
                extra={"region": region.name, "bloecke": leere_bloecke},
            )
        return observations

    # --- Block-Extraktoren (Template-Stand: 2025/2026, Fußzeile "IT.NRW") ---

    @staticmethod
    def _kp_flaeche(lines: list[str], obs) -> list[RawObservation]:
        """'Flächeam31.12.2024nachNutzungsarten' → 'Flächeinsgesamt 7887 …' (ha)."""
        for i, line in enumerate(lines):
            m = re.match(r"Flächeam31\.12\.(\d{4})nachNutzungsarten", line)
            if not m:
                continue
            jahr = m.group(1)
            for folgezeile in lines[i + 1 : i + 15]:
                toks = folgezeile.split()
                if toks and toks[0] == "Flächeinsgesamt":
                    zahlen = _zahlen_tokens(toks[1:])
                    if zahlen:
                        # Quelle liefert Hektar → km²
                        return [obs(CLUSTER_DEMOGRAFIE, "Fläche", f"{jahr}-12-31",
                                    round(zahlen[0] / 100, 2), "km²")]
            break
        return []

    @staticmethod
    def _kp_bevoelkerung_serie(lines: list[str], obs) -> list[RawObservation]:
        """Jahresreihe 'Bevölkerungam31.12. a v1 … v7' mit Jahres-Kopfzeile davor."""
        for i, line in enumerate(lines):
            toks = line.split()
            if not (toks and toks[0] == "Bevölkerungam31.12." and len(toks) > 2 and toks[1] == "a"):
                continue
            werte = _zahlen_tokens(toks[2:])
            jahre: list[int] | None = None
            for kandidat in reversed(lines[max(0, i - 8) : i]):
                ktoks = kandidat.split()
                if len(ktoks) >= 3 and all(_JAHR_RE.fullmatch(t) for t in ktoks):
                    jahre = [int(t) for t in ktoks]
                    break
            if jahre and len(jahre) == len(werte):
                return [
                    obs(CLUSTER_DEMOGRAFIE, "Einwohner", f"{jahr}-12-31", wert, "Anzahl")
                    for jahr, wert in zip(jahre, werte)
                ]
            log.debug("Bevölkerungsserie: Jahre/Werte passen nicht", extra={"zeile": line})
        return []

    @staticmethod
    def _kp_bevoelkerungsstruktur(lines: list[str], obs) -> list[RawObservation]:
        """Abschnitt 'Bevölkerungsstruktur…am31.12.JJJJnachAltersgruppen':
        Altersgruppen-Anteile + Anteil weiblich + Anteil Nichtdeutsche."""
        alters_re = re.compile(r"^(unter(\d+)|(\d+)bisunter(\d+)|(\d+)undmehr)$")
        for i, line in enumerate(lines):
            m = re.match(r"Bevölkerungsstruktur.*?am31\.12\.(\d{4})nachAltersgruppen", line)
            if not m:
                continue
            stichtag = f"{m.group(1)}-12-31"
            ergebnisse: list[RawObservation] = []
            for folgezeile in lines[i + 1 : i + 30]:
                if folgezeile.startswith("IT.NRW"):
                    break
                toks = folgezeile.split()
                if not toks:
                    continue
                zahlen = _zahlen_tokens(toks[1:])
                am = alters_re.match(toks[0])
                if am and len(zahlen) >= 2:
                    if am.group(1) == "18bisunter65":
                        continue  # Sammelgruppe, redundant zu den Einzelgruppen
                    if am.group(2):
                        label = f"unter {am.group(2)} Jahre"
                    elif am.group(3):
                        label = f"{am.group(3)} bis unter {am.group(4)} Jahre"
                    else:
                        label = f"{am.group(5)} Jahre und mehr"
                    # zahlen = [Anzahl, Anteil%, Vergleichswerte …]
                    ergebnisse.append(
                        obs(CLUSTER_DEMOGRAFIE, f"Anteil {label}", stichtag, zahlen[1], "%")
                    )
                elif toks[0] == "Weiblich" and len(zahlen) >= 2:
                    ergebnisse.append(
                        obs(CLUSTER_DEMOGRAFIE, "Anteil weiblich", stichtag, zahlen[1], "%")
                    )
                elif toks[0].startswith("Nichtdeutsche") and len(zahlen) >= 2:
                    ergebnisse.append(
                        obs(CLUSTER_DEMOGRAFIE, "Anteil Nichtdeutsche", stichtag, zahlen[1], "%")
                    )
            return ergebnisse
        return []

    @staticmethod
    def _kp_pendler(lines: list[str], obs) -> list[RawObservation]:
        """'…Beschäftigteam30.6.JJJJnachGeschlecht' → Insgesamt-Zeile:
        [Arbeitsort, Einpendler, Wohnort, Auspendler, Saldo]."""
        for i, line in enumerate(lines):
            m = re.match(
                r"SozialversicherungspflichtigBeschäftigteam(\d{1,2})\.(\d{1,2})\.(\d{4})nachGeschlecht",
                line,
            )
            if not m:
                continue
            stichtag = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            for folgezeile in lines[i + 1 : i + 8]:
                toks = folgezeile.split()
                if toks and toks[0] == "Insgesamt":
                    zahlen = _zahlen_tokens(toks[1:])
                    if len(zahlen) >= 5:
                        return [
                            obs(CLUSTER_PENDLER, "Einpendler", stichtag, zahlen[1], "Anzahl"),
                            obs(CLUSTER_PENDLER, "Auspendler", stichtag, zahlen[3], "Anzahl"),
                            obs(CLUSTER_PENDLER, "Pendlersaldo", stichtag, zahlen[4], "Anzahl"),
                        ]
            break
        return []

    @staticmethod
    def _kp_gewerbe(lines: list[str], obs) -> list[RawObservation]:
        """'Gewerbean-und-abmeldungen…JJJJnach…' → An-/Abmeldungen insgesamt.

        Achtung: pdfplumber verliert die stilisierte Initiale, die Zeilen heißen
        'nmeldungeninsgesamt' / 'bmeldungeninsgesamt' — daher '[Aa]?'.
        """
        for i, line in enumerate(lines):
            m = re.match(r"Gewerbean-und-abmeldungen.*?(\d{4})nach", line)
            if not m:
                continue
            jahr = m.group(1)
            ergebnisse: list[RawObservation] = []
            for folgezeile in lines[i + 1 : i + 45]:
                toks = folgezeile.split()
                if not toks:
                    continue
                zahlen = _zahlen_tokens(toks[1:])
                if re.match(r"^[Aa]?nmeldungeninsgesamt$", toks[0]) and zahlen:
                    ergebnisse.append(
                        obs(CLUSTER_WIRTSCHAFT, "Gewerbeanmeldungen", jahr, zahlen[0], "Anzahl")
                    )
                elif re.match(r"^[Aa]?bmeldungeninsgesamt$", toks[0]) and zahlen:
                    ergebnisse.append(
                        obs(CLUSTER_WIRTSCHAFT, "Gewerbeabmeldungen", jahr, zahlen[0], "Anzahl")
                    )
                if len(ergebnisse) == 2:
                    break
            return ergebnisse
        return []

    @staticmethod
    def _kp_umsatzsteuer(lines: list[str], obs) -> list[RawObservation]:
        """'Steuerpflichtige,steuerbarerUmsatz…' → Reihen über die Jahres-Kopfzeile
        ('Merkmal 2014 2017 2020 2023')."""
        for i, line in enumerate(lines):
            if not re.match(r"Steuerpflichtige,steuerbarerUmsatz", line):
                continue
            jahre: list[int] | None = None
            ergebnisse: list[RawObservation] = []
            for folgezeile in lines[i + 1 : i + 15]:
                toks = folgezeile.split()
                if not toks:
                    continue
                if toks[0] == "Merkmal" and len(toks) > 2 and all(
                    _JAHR_RE.fullmatch(t) for t in toks[1:]
                ):
                    jahre = [int(t) for t in toks[1:]]
                    continue
                if jahre is None:
                    continue
                zahlen = _zahlen_tokens(toks[1:])
                if toks[0] == "Steuerpflichtige" and len(zahlen) == len(jahre):
                    ergebnisse.extend(
                        obs(CLUSTER_WIRTSCHAFT, "Umsatzsteuerpflichtige", str(jahr), wert, "Anzahl")
                        for jahr, wert in zip(jahre, zahlen)
                    )
                elif toks[0] == "SteuerbarerUmsatz(1000EUR)" and len(zahlen) == len(jahre):
                    ergebnisse.extend(
                        obs(CLUSTER_WIRTSCHAFT, "Steuerbarer Umsatz", str(jahr), wert, "Tsd. €")
                        for jahr, wert in zip(jahre, zahlen)
                    )
            return ergebnisse
        return []

    @staticmethod
    def _kp_einkommen(lines: list[str], obs) -> list[RawObservation]:
        """'PrimäreinkommenundverfügbaresEinkommenderprivatenHaushalteJJJJ':
        je Block die Zeile 'EURjeEinwohner <wert> …'.

        Auch hier fehlt teils die Initiale ('erfügbaresEinkommen…').
        """
        for i, line in enumerate(lines):
            m = re.match(
                r"Primäreinkommenundverf(ü|u)gbaresEinkommenderprivatenHaushalte(\d{4})", line
            )
            if not m:
                continue
            jahr = m.group(2)
            block: str | None = None
            ergebnisse: list[RawObservation] = []
            for folgezeile in lines[i + 1 : i + 15]:
                toks = folgezeile.split()
                if not toks:
                    continue
                if re.match(r"^Primäreinkommen$", toks[0]):
                    block = "primaer"
                elif re.match(r"^[Vv]?erfügbaresEinkommen", toks[0]):
                    block = "verfuegbar"
                elif toks[0] == "EURjeEinwohner":
                    zahlen = _zahlen_tokens(toks[1:])
                    if not zahlen:
                        continue
                    if block == "primaer":
                        ergebnisse.append(
                            obs(CLUSTER_EINKOMMEN, "Einkommen je Einwohner", jahr, zahlen[0], "€")
                        )
                    elif block == "verfuegbar":
                        ergebnisse.append(
                            obs(CLUSTER_KAUFKRAFT, "Verfügbares Einkommen je Einwohner",
                                jahr, zahlen[0], "€")
                        )
            return ergebnisse
        return []

    @staticmethod
    def _kp_schulen(lines: list[str], obs) -> list[RawObservation]:
        """'AllgemeinbildendeSchulen…am15.10.JJJJ' → Anzahl Schulen je Schulform.

        Layout (Spaltenreihenfolge variiert je Region → aus Kopfzeile gelesen):
            [A]llgemeinbildendeSchulen*)am15.10.2024
            Ins- Grund- Haupt- Real- Gesamt- Gymna-      ← Kopf-Präfixe
            gesamt1) schule schule schule schule sium
            Schulen 40 24 2 3 2 5                          ← Werte je Schulform

        pdfplumber verliert die stilisierte Initiale → '[Aa]?llgemeinbildende'.
        """
        for i, line in enumerate(lines):
            m = re.match(r"[Aa]?llgemeinbildendeSchulen.*?am\s?\d{1,2}\.(\d{1,2})\.(\d{4})", line)
            if not m:
                continue
            stichtag = f"{m.group(2)}-{int(m.group(1)):02d}-15"

            # Kopfzeile: die Folgezeile mit den meisten Schulform-Präfixen
            schulformen: list[str] = []
            for folgezeile in lines[i + 1 : i + 5]:
                kandidaten = [_schulform(t) for t in folgezeile.split()]
                treffer = [s for s in kandidaten if s]
                if len(treffer) >= 3:
                    schulformen = [s for s in kandidaten if s]  # inkl. Position
                    break
            if not schulformen:
                break

            # Datenzeile 'Schulen <werte…>'
            for folgezeile in lines[i + 1 : i + 10]:
                toks = folgezeile.split()
                if not (toks and toks[0] == "Schulen"):
                    continue
                zahlen = _zahlen_tokens(toks[1:])
                if len(zahlen) != len(schulformen):
                    log.debug("Schulen: Spaltenzahl passt nicht zum Kopf",
                              extra={"formen": schulformen, "werte": zahlen})
                    break
                return [
                    obs(CLUSTER_BILDUNG, f"Anzahl Schulen {form}", stichtag, wert, "Anzahl")
                    for form, wert in zip(schulformen, zahlen)
                ]
            break
        return []

    # ------------------------------------------------------------ Ableitungen

    @staticmethod
    def _derive_pendlersaldo(
        observations: list[RawObservation], region: Region
    ) -> list[RawObservation]:
        """Pendlersaldo = Einpendler − Auspendler — nur für Jahre, für die die
        Quelle den Saldo nicht bereits selbst ausweist."""
        vorhanden = {o.jahr_stichtag for o in observations if o.kennzahl == "Pendlersaldo"}
        einpendler = {o.jahr_stichtag: o for o in observations if o.kennzahl == "Einpendler"}
        auspendler = {o.jahr_stichtag: o for o in observations if o.kennzahl == "Auspendler"}
        saldi: list[RawObservation] = []
        for jahr, ein in einpendler.items():
            aus = auspendler.get(jahr)
            if aus is None or jahr in vorhanden:
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
