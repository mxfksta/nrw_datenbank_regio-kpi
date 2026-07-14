"""Erzeugt die SYNTHETISCHEN Fixtures (nur BA — dort liegt noch keine echte
Beispieldatei vor, weil die Download-URLs erst konfiguriert werden müssen).

    python tests/fixtures/generate_fixtures.py

Die übrigen Fixtures sind ECHTE Quelldaten (Leverkusen, 05316, Abruf 2026-07-14):

- zensus_05316000_GRUNDINFO_BEVOELKERUNG.xlsx  — Original von statistik.nrw
- kommunalprofil_text_05316.txt                — pdfplumber-Text aus l05316.pdf
- wahlprofil_text_05316.txt                    — pdfplumber-Text aus wp05316.pdf

Zum Aktualisieren der echten Fixtures: Dateien neu herunterladen und den Text
mit pdfplumber extrahieren (zeilenweise, leere Zeilen entfernen) — siehe
StatistikNrwConnector._lines_from_pdf.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

FIXTURES = Path(__file__).parent


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
    ba_einzelheft()
    print("Fixtures geschrieben nach", FIXTURES)
