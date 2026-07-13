"""Erzeugt die binären XLSX-Fixtures (synthetisch, an die Quell-Layouts angelehnt).

Einmalig ausführen und die erzeugten Dateien einchecken:

    python tests/fixtures/generate_fixtures.py

Die Fixtures bilden die vom Parser ERWARTETEN Layouts ab (Label-Spalte +
Wertspalten). Weichen die echten Quelldateien ab, müssen die Label-Konstanten
in den Konnektoren UND diese Fixtures nachgezogen werden.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

FIXTURES = Path(__file__).parent


def zensus_grundinfo() -> None:
    """Synthetisches Zensus-2022-Grundinfo-XLSX (Leverkusen, absolute Zahlen).

    Altersgruppen summieren sich exakt auf 'Insgesamt' (163 905).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Bevölkerung"
    rows = [
        ["Zensus 2022 — Grundinformationen Bevölkerung", None],
        ["Leverkusen, kreisfreie Stadt (05316000)", None],
        [None, None],
        ["Merkmal", "Personen"],
        ["Insgesamt", 163905],
        ["weiblich", 83958],
        ["Ausländer/-innen", 22120],
        [None, None],
        ["Alter (Gruppen)", None],
        ["unter 18", 28684],
        ["18 bis 29", 21308],
        ["30 bis 49", 42615],
        ["50 bis 64", 36059],
        ["65 und älter", 35239],
    ]
    for row in rows:
        ws.append(row)
    wb.save(FIXTURES / "zensus_05316000_GRUNDINFO_BEVOELKERUNG.xlsx")


def ba_einzelheft() -> None:
    """Synthetisches BA-Einzelheft-XLSX (Arbeitslose + Quote, Berichtsmonat)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Eckwerte"
    rows = [
        ["Arbeitsmarkt in Zahlen — Arbeitslose und Arbeitslosenquoten", None],
        ["Leverkusen, Stadt", None],
        ["Berichtsmonat: Juni 2025", None],
        [None, None],
        ["Merkmal", "Wert"],
        ["Arbeitslose", 6512],
        ["Arbeitslosenquote bezogen auf alle zivilen Erwerbspersonen", "7,4"],
    ]
    for row in rows:
        ws.append(row)
    wb.save(FIXTURES / "ba_einzelheft_05316.xlsx")


if __name__ == "__main__":
    zensus_grundinfo()
    ba_einzelheft()
    print("Fixtures geschrieben nach", FIXTURES)
