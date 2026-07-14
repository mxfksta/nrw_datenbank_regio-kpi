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

ZENSUS_STICHTAG = "2022-05-15"  # amtlicher Zensus-Stichtag
ZENSUS_QUELLE_NAME = "Zensus 2022 (statistik.nrw Grundinfo Bevölkerung)"
KOMMUNALPROFIL_QUELLE_NAME = "statistik.nrw Kommunalprofil (IT.NRW)"

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
