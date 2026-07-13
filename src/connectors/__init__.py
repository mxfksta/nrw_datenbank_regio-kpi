"""Registry aller Konnektoren.

main.py iteriert über ``build_connectors(settings)``; die Reihenfolge ist
zugleich die Abarbeitungsreihenfolge (amtliche Phase-1-Quellen zuerst).
"""

from __future__ import annotations

from src.config import Settings
from src.connectors.arbeitsagentur import ArbeitsagenturConnector
from src.connectors.base import Connector
from src.connectors.phase2 import (
    BildungConnector,
    EinzelhandelConnector,
    MobilitaetConnector,
    RisikenConnector,
    VeranstaltungenConnector,
    VereineConnector,
)
from src.connectors.statistik_nrw import StatistikNrwConnector
from src.connectors.wahlprofile import WahlprofileConnector

ALL_CONNECTORS: tuple[type[Connector], ...] = (
    # Phase 1 — automatisiert
    StatistikNrwConnector,
    WahlprofileConnector,
    ArbeitsagenturConnector,
    # Phase 2 — Gerüste (werden als SKIPPED protokolliert)
    VeranstaltungenConnector,
    VereineConnector,
    MobilitaetConnector,
    EinzelhandelConnector,
    BildungConnector,
    RisikenConnector,
)


def build_connectors(settings: Settings, include_phase2: bool = True) -> list[Connector]:
    return [
        cls(settings)
        for cls in ALL_CONNECTORS
        if include_phase2 or cls.phase == 1
    ]
