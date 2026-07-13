"""Phase-2-Konnektoren: Gerüste, Beschaffung vorerst MANUELL.

Diese Quellen sind Portale ohne stabile API bzw. liefern überwiegend
qualitative Angaben. Die Klassen existieren, damit

- die Registry vollständig ist (alle Cluster aus kpi_spec.yaml haben einen
  Konnektor),
- main.py sie sauber als SKIPPED protokolliert (kein Fehler),
- eine spätere Automatisierung nur ``fetch_raw`` implementieren muss.

Jede Klasse dokumentiert die recherchierten Quellen und die offenen Punkte
(TODO) für eine Automatisierung.
"""

from __future__ import annotations

from typing import ClassVar

from src.config import (
    CLUSTER_BILDUNG,
    CLUSTER_EINZELHANDEL,
    CLUSTER_MOBILITAET,
    CLUSTER_RISIKEN,
    CLUSTER_VERANSTALTUNGEN,
    CLUSTER_VEREINE,
    Region,
)
from src.connectors.base import Connector, ManualSourceError
from src.models import RawObservation


class _ManualConnector(Connector):
    """Basis für Phase-2-Stubs: wirft immer ManualSourceError."""

    phase: ClassVar[int] = 2
    #: kurze Begründung, warum (noch) manuell — erscheint im Log
    grund: ClassVar[str] = "keine stabile API / überwiegend qualitative Angaben"

    def fetch_raw(self, region: Region) -> list[RawObservation]:
        raise ManualSourceError(
            f"Cluster {', '.join(self.clusters)}: Beschaffung vorerst manuell — {self.grund}. "
            f"Siehe Docstring von {type(self).__module__}.{type(self).__name__} für Quellen/TODOs."
        )


class VeranstaltungenConnector(_ManualConnector):
    """Veranstaltungen: #Events (rollierend 90 Tage) + Eventliste.

    Quellen: kommunale Veranstaltungskalender der 7 Regionen, meinestadt.de.
    TODO für Automatisierung:
    - je Region den Kalender identifizieren (uneinheitliche CMS, teils iCal-Feeds)
    - Kategorisierung vereinheitlichen; Dublettenerkennung Kommune vs. meinestadt.de
    - Eventliste ist qualitativ → separate Tabelle statt fact_kpi nötig
    """

    name: ClassVar[str] = "veranstaltungen"
    clusters: ClassVar[tuple[str, ...]] = (CLUSTER_VERANSTALTUNGEN,)


class VereineConnector(_ManualConnector):
    """Vereine: Top-25 je Region + Anzahl je Kategorie (Kategorien: kpi_spec.yaml).

    Quellen: Vereinsverzeichnisse der Kommunen, Gemeinsames Registerportal
    (Vereinsregister), Kreis-/Stadtsportbünde.
    TODO für Automatisierung:
    - Registerportal erlaubt keine Massenabfrage (Session-/Captcha-geschützt)
    - Kategorisierung (Sport, Kultur/Musik, Brauchtum/Karneval, ...) erfordert
      Klassifikation der Vereinsnamen/-zwecke
    """

    name: ClassVar[str] = "vereine"
    clusters: ClassVar[tuple[str, ...]] = (CLUSTER_VEREINE,)


class MobilitaetConnector(_ManualConnector):
    """Mobilität & Erreichbarkeit: Bahnhöfe/Knoten, ÖPNV-Verbund, Autobahnanschlüsse.

    Quellen: Stadt-/Kreis-Webseiten, VRS/VRR, Deutsche Bahn (Stationsdaten).
    TODO für Automatisierung:
    - DB-Stationsdaten-API wäre je Region filterbar (Kandidat für Phase 1.5)
    - Autobahnanschlüsse: keine amtliche, regionsscharfe Liste mit stabiler API
    """

    name: ClassVar[str] = "mobilitaet"
    clusters: ClassVar[tuple[str, ...]] = (CLUSTER_MOBILITAET,)


class EinzelhandelConnector(_ManualConnector):
    """Einzelhandel/Innenstadt: Konzept vorhanden (+Jahr), Citymanagement, Frequenz/Leerstand.

    Quellen: kommunale Dokumente (Zentren-/Einzelhandelskonzepte, Ratsinformationssysteme).
    TODO für Automatisierung:
    - unstrukturierte PDF-/HTML-Dokumente je Kommune; keine einheitliche Ablage
    - Frequenz-/Leerstandsdaten nur vereinzelt veröffentlicht
    """

    name: ClassVar[str] = "einzelhandel"
    clusters: ClassVar[tuple[str, ...]] = (CLUSTER_EINZELHANDEL,)


class BildungConnector(_ManualConnector):
    """Bildung & Betreuung: Kita-Plätze/Betreuungsquote, Schulen nach Schulform, Bildungsbericht.

    Quellen: IT.NRW (Schulstatistik), Destatis, MSB NRW, kinderbetreuung.nrw.
    TODO für Automatisierung:
    - IT.NRW-Schulstatistik ist strukturiert verfügbar → Kandidat, um diesen
      Cluster als ERSTEN nach Phase 1 zu heben (via Landesdatenbank-Client)
    """

    name: ClassVar[str] = "bildung"
    clusters: ClassVar[tuple[str, ...]] = (CLUSTER_BILDUNG,)
    grund: ClassVar[str] = (
        "Portale uneinheitlich; IT.NRW-Schulstatistik ist strukturiert und "
        "kann als erstes gehoben werden (Landesdatenbank-Client nutzen)"
    )


class RisikenConnector(_ManualConnector):
    """Risiken Starkregen/Hochwasser: Karten vorhanden (ja/nein), zuständige Stelle + Link.

    Quellen: LANUV NRW (Gefahren-/Hinweiskarten), Bezirksregierung, Kommunen.
    WICHTIG: Keine Score-Erfindung — nur dokumentierte Fakten übernehmen.
    TODO für Automatisierung:
    - LANUV-Kartendienste (WMS) sind maschinenlesbar, aber die Aussage
      "Karte vorhanden ja/nein je Kommune" erfordert fachliche Prüfung
    """

    name: ClassVar[str] = "risiken"
    clusters: ClassVar[tuple[str, ...]] = (CLUSTER_RISIKEN,)
