"""Konnektor Wahlprofile (statistik.nrw, ``wp{RS}.pdf``).

Reales Layout (IT.NRW-Template "Wahlergebnisse seit 1994"): je Wahlart ein
Abschnitt mit einer Tabelle über ALLE Wahlen seit 1994::

    Kommunalwahlen(WahlenzudenRätenderkreisfreienStädte)1994bis2025
    StimmenanteileinProzent
    Stichtag Wahlbeteiligungin CDU SPD GRÜNE FDP AfD DieLinke¹ Sonstige
    Prozent
    16.10.1994 81,0 37,1 37,4 10,0 3,9 x – 11,5
    ...
    14.09.2025 54,0 31,0 21,5 10,9 3,3 15,4 5,1 12,8

Die Parteispalten werden aus der Kopfzeile gelesen (robust gegen Umsortierung),
Platzhalter (x, –) überspringen die jeweilige Partei. Emittiert wird je Wahlart
nur die JÜNGSTE Wahl ("letzte Wahl: Stichtag" laut kpi_spec.yaml):

- ``Wahlbeteiligung <Wahlart>``          (%)
- ``Stimmenanteil <Partei> <Wahlart>``   (%)

``jahr_stichtag`` ist das Wahldatum (ISO). Bewusst KEINE historischen Wahlen →
kein avg_3j über verschiedene Wahlen hinweg (wäre fachlich irreführend).
"""

from __future__ import annotations

import io
import logging
import re
from typing import ClassVar

import pdfplumber

from src.config import CLUSTER_WAHLEN, WAHLPROFIL_PDF_URL, Region, get_cluster_spec
from src.connectors.base import Connector, SourceLayoutError
from src.models import RawObservation
from src.net import http_get
from src.transform import heute, parse_german_number

log = logging.getLogger(__name__)

QUELLE_NAME = "statistik.nrw Wahlprofil (IT.NRW)"

#: Defaults; kpi_spec.yaml kann sie je Cluster mit `wahlarten:`/`parteien:` überschreiben
DEFAULT_WAHLARTEN = ["Bundestagswahl", "Landtagswahl", "Europawahl", "Kommunalwahl"]
DEFAULT_PARTEIEN = ["CDU", "SPD", "GRÜNE", "FDP", "AfD", "DIE LINKE", "Sonstige"]

#: Kopfzeilen-Schreibweise (normalisiert: nur Buchstaben, Großschreibung)
#: → kanonischer Parteiname laut kpi_spec.yaml
PARTEI_ALIASE: dict[str, str] = {
    "CDU": "CDU",
    "SPD": "SPD",
    "GRÜNE": "GRÜNE",
    "GRUENE": "GRÜNE",
    "FDP": "FDP",
    "AFD": "AfD",
    "DIELINKE": "DIE LINKE",
    "LINKE": "DIE LINKE",
    "SONSTIGE": "Sonstige",
}

_PLATZHALTER = {"x", "X", "–", "-", ".", "/"}
_DATUM_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.((?:19|20)\d{2})$")


def _normalisiere_partei(token: str) -> str | None:
    """Kopfzeilen-Token ("DieLinke¹") → kanonischer Name ("DIE LINKE")."""
    nur_buchstaben = re.sub(r"[^A-Za-zÄÖÜäöü]", "", token).upper()
    return PARTEI_ALIASE.get(nur_buchstaben)


class WahlprofileConnector(Connector):
    name: ClassVar[str] = "wahlprofile"
    phase: ClassVar[int] = 1
    clusters: ClassVar[tuple[str, ...]] = (CLUSTER_WAHLEN,)

    def fetch_raw(self, region: Region) -> list[RawObservation]:
        url = WAHLPROFIL_PDF_URL.format(rs=region.regionalschluessel)
        resp = http_get(url, self.settings)
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        return self.observations_from_text(text, region, url)

    def observations_from_text(
        self, text: str, region: Region, url: str
    ) -> list[RawObservation]:
        spec = get_cluster_spec(CLUSTER_WAHLEN)
        wahlarten: list[str] = spec.get("wahlarten") or DEFAULT_WAHLARTEN
        parteien: list[str] = spec.get("parteien") or DEFAULT_PARTEIEN

        # Abschnitts-Erkennung: Zeile beginnt mit der Wahlart (Text ist
        # zusammengezogen: "Kommunalwahlen(Wahlenzu…)1994bis2025")
        alt = "|".join(re.escape(w) for w in wahlarten)
        header_re = re.compile(rf"^({alt})")

        wahlart: str | None = None
        spalten: list[str | None] = []  # Parteispalten der aktuellen Tabelle
        #: Wahlart → (Stichtag ISO, Wahlbeteiligung, {Partei: Anteil})
        letzte_wahl: dict[str, tuple[str, float | None, dict[str, float]]] = {}

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            toks = line.split()

            m = header_re.match(line)
            if m:
                wahlart = m.group(1)
                continue

            # Spalten-Kopfzeile: "Stichtag Wahlbeteiligungin CDU SPD …"
            if toks[0] == "Stichtag" and len(toks) > 2:
                spalten = [_normalisiere_partei(t) for t in toks[2:]]
                continue

            # Datenzeile: "14.09.2025 54,0 31,0 …"
            dm = _DATUM_RE.match(toks[0])
            if not (dm and wahlart and len(toks) >= 3):
                continue
            stichtag = f"{dm.group(3)}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}"

            def _wert(token: str) -> float | None:
                if token in _PLATZHALTER:
                    return None
                try:
                    return parse_german_number(token)
                except ValueError:
                    return None

            beteiligung = _wert(toks[1])
            partei_werte: dict[str, float] = {}
            for spalte, token in zip(spalten, toks[2:]):
                if spalte is None or spalte not in parteien:
                    continue
                wert = _wert(token)
                if wert is not None:
                    partei_werte[spalte] = wert

            bisher = letzte_wahl.get(wahlart)
            if bisher is None or stichtag > bisher[0]:
                letzte_wahl[wahlart] = (stichtag, beteiligung, partei_werte)

        if not letzte_wahl:
            raise SourceLayoutError(
                "Wahlprofil-PDF: keine Wahlart-Tabelle gefunden — Template "
                f"geändert? Parser in wahlprofile.py prüfen ({url})"
            )

        stand = heute()
        observations: list[RawObservation] = []
        for wahlart, (stichtag, beteiligung, partei_werte) in letzte_wahl.items():
            def obs(kennzahl: str, wert: float) -> RawObservation:
                return RawObservation(
                    region=region.name,
                    regionalschluessel=region.regionalschluessel,
                    kpi_cluster=CLUSTER_WAHLEN,
                    kennzahl=kennzahl,
                    jahr_stichtag=stichtag,
                    wert=wert,
                    einheit="%",
                    quelle_name=QUELLE_NAME,
                    quelle_url=url,
                    stand_datum=stand,
                )

            if beteiligung is not None:
                observations.append(obs(f"Wahlbeteiligung {wahlart}", beteiligung))
            for partei, wert in partei_werte.items():
                observations.append(obs(f"Stimmenanteil {partei} {wahlart}", wert))

        fehlend = set(wahlarten) - set(letzte_wahl)
        if fehlend:
            log.info(
                "Wahlprofil: Wahlarten ohne Tabelle",
                extra={"region": region.name, "fehlend": sorted(fehlend)},
            )
        return observations
