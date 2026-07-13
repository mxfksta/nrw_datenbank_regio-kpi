"""Konnektor Wahlprofile (statistik.nrw, ``wp{RS}.pdf``).

Extrahiert je Wahlart (Bundestagswahl, Landtagswahl, Europawahl, Kommunalwahl)
die jeweils LETZTE Wahl:

- ``Wahlbeteiligung <Wahlart>``          (%)
- ``Stimmenanteil <Partei> <Wahlart>``   (%)

``jahr_stichtag`` ist das Wahldatum (falls im PDF erkennbar), sonst das
Wahljahr. Wahlergebnisse sind Einzelereignisse, keine Jahresreihen — avg_3j
entsteht daher (korrekt) nicht.

Die Wahlarten- und Parteienliste kommt aus kpi_spec.yaml (Cluster "Wahlprofile").
Der Parser arbeitet auf dem extrahierten PDF-Text (zeilenbasiert, defensiv):
Abschnitte werden über "<Wahlart> <Jahr>"-Überschriften erkannt; ändert sich
das Layout grundlegend, gibt es einen SourceLayoutError mit klarer Meldung.
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

QUELLE_NAME = "statistik.nrw Wahlprofil"

#: Defaults; kpi_spec.yaml kann sie je Cluster mit `wahlarten:`/`parteien:` überschreiben
DEFAULT_WAHLARTEN = ["Bundestagswahl", "Landtagswahl", "Europawahl", "Kommunalwahl"]
DEFAULT_PARTEIEN = ["CDU", "SPD", "GRÜNE", "FDP", "AfD", "DIE LINKE", "Sonstige"]

#: Schreibweisen im PDF → kanonischer Parteiname laut kpi_spec.yaml
PARTEI_ALIASE: dict[str, str] = {
    "CDU": "CDU",
    "SPD": "SPD",
    "GRÜNE": "GRÜNE",
    "GRUENE": "GRÜNE",
    "BÜNDNIS 90/DIE GRÜNEN": "GRÜNE",
    "B90/GRÜNE": "GRÜNE",
    "FDP": "FDP",
    "AFD": "AfD",
    "DIE LINKE": "DIE LINKE",
    "LINKE": "DIE LINKE",
    "SONSTIGE": "Sonstige",
    "ANDERE": "Sonstige",
    "ÜBRIGE": "Sonstige",
}

_PROZENT_RE = r"(\d{1,3}(?:,\d+)?)\s*%?"
_DATUM_RE = re.compile(r"am\s+(\d{1,2})\.\s*(?:(\d{1,2})\.|([A-Za-zäöüÄÖÜ]+))\s*((?:19|20)\d{2})")
_MONATSNAMEN = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
}


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

        sections = self._split_sections(text, wahlarten)
        if not sections:
            raise SourceLayoutError(
                "Wahlprofil-PDF: keine Wahlart-Überschrift gefunden "
                f"(erwartet z. B. 'Bundestagswahl 2025') — Layout prüfen ({url})"
            )

        stand = heute()
        observations: list[RawObservation] = []

        def obs(kennzahl: str, jahr_stichtag: str, wert: float) -> RawObservation:
            return RawObservation(
                region=region.name,
                regionalschluessel=region.regionalschluessel,
                kpi_cluster=CLUSTER_WAHLEN,
                kennzahl=kennzahl,
                jahr_stichtag=jahr_stichtag,
                wert=wert,
                einheit="%",
                quelle_name=QUELLE_NAME,
                quelle_url=url,
                stand_datum=stand,
            )

        for wahlart, jahr, section_text in sections:
            stichtag = self._find_stichtag(section_text) or jahr

            beteiligung_match = re.search(
                rf"(?i)wahlbeteiligung\D*?{_PROZENT_RE}", section_text
            )
            if beteiligung_match:
                observations.append(
                    obs(
                        f"Wahlbeteiligung {wahlart}",
                        stichtag,
                        parse_german_number(beteiligung_match.group(1)),
                    )
                )
            else:
                log.warning(
                    "Wahlbeteiligung nicht gefunden",
                    extra={"wahlart": wahlart, "region": region.name},
                )

            for line in section_text.splitlines():
                partei = self._match_partei(line, parteien)
                if partei is None:
                    continue
                prozent_match = re.search(rf"{_PROZENT_RE}\s*%?\s*$", line.strip())
                if prozent_match is None:
                    prozent_match = re.search(_PROZENT_RE, line.split(maxsplit=1)[-1])
                if prozent_match is None:
                    continue
                kennzahl = f"Stimmenanteil {partei} {wahlart}"
                if any(o.kennzahl == kennzahl and o.jahr_stichtag == stichtag for o in observations):
                    continue  # erster Treffer je Partei/Wahl gewinnt
                observations.append(
                    obs(kennzahl, stichtag, parse_german_number(prozent_match.group(1)))
                )

        return observations

    @staticmethod
    def _split_sections(text: str, wahlarten: list[str]) -> list[tuple[str, str, str]]:
        """Zerlegt den Text an "<Wahlart> <Jahr>"-Überschriften.

        Liefert je Wahlart nur die NEUESTE Wahl (höchstes Jahr) als
        (wahlart, jahr, abschnittstext).
        """
        alt = "|".join(re.escape(w) for w in wahlarten)
        # ".{0,40}?" statt "\D..." — dazwischen darf ein Datum stehen
        # ("Bundestagswahl am 23. Februar 2025")
        header_re = re.compile(rf"(?im)^\s*({alt})(?:en)?\b.{{0,40}}?((?:19|20)\d{{2}})")
        matches = list(header_re.finditer(text))
        sections: dict[str, tuple[int, str]] = {}
        for i, match in enumerate(matches):
            wahlart, jahr = match.group(1), int(match.group(2))
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            if wahlart not in sections or jahr > sections[wahlart][0]:
                sections[wahlart] = (jahr, text[start:end])
        return [(wahlart, str(jahr), sec) for wahlart, (jahr, sec) in sections.items()]

    @staticmethod
    def _find_stichtag(section_text: str) -> str | None:
        """Sucht ein Wahldatum ("am 23. Februar 2025" / "am 14.09.2025") → ISO."""
        match = _DATUM_RE.search(section_text)
        if not match:
            return None
        tag = int(match.group(1))
        jahr = match.group(4)
        if match.group(2):
            monat = int(match.group(2))
        else:
            monat = _MONATSNAMEN.get(match.group(3).lower())
            if monat is None:
                return None
        return f"{jahr}-{monat:02d}-{tag:02d}"

    @staticmethod
    def _match_partei(line: str, parteien: list[str]) -> str | None:
        """Erkennt eine Partei am Zeilenanfang (inkl. Aliase, längste zuerst)."""
        stripped = line.strip().upper()
        for alias in sorted(PARTEI_ALIASE, key=len, reverse=True):
            if stripped.startswith(alias):
                kanonisch = PARTEI_ALIASE[alias]
                if kanonisch in parteien:
                    return kanonisch
        return None
