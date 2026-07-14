"""Erzeugt die SYNTHETISCHEN Fixtures nach.

    python tests/fixtures/generate_fixtures.py

- ba_einzelheft_dlk.zip : Mini-Nachbau der bundesweiten BA-ZIP (zwei XLSX mit
  Blatt "Übersicht_Kreise", Struktur wie das Original: Datums-Kopfzeile +
  Regionszeilen "<RS> <Name>"). Die echte Datei ist ~23 MB und wird NICHT
  eingecheckt; dieser Nachbau bildet exakt das vom Parser erwartete Layout ab.

Die übrigen Fixtures sind ECHTE Quelldaten (Leverkusen, 05316, Abruf 2026-07-14):

- zensus_05316000_GRUNDINFO_BEVOELKERUNG.xlsx  — Original von statistik.nrw
- kommunalprofil_text_05316.txt                — pdfplumber-Text aus l05316.pdf
- wahlprofil_text_05316.txt                    — pdfplumber-Text aus wp05316.pdf
- ba_wz_05316.csv                              — synthetisch (WZ-Export, URL n.v.)
"""

from __future__ import annotations

import datetime
import io
import zipfile
from pathlib import Path

from openpyxl import Workbook

FIXTURES = Path(__file__).parent

# Ein paar Regionen (inkl. der 7 Zielregionen) + Fremdregion als Rauschen.
_REGIONEN = [
    ("01001", "Flensburg, Stadt"),
    ("05314", "Bonn, Stadt"),
    ("05316", "Leverkusen, Stadt"),
    ("05362", "Rhein-Erft-Kreis"),
    ("05366", "Euskirchen"),
    ("05374", "Oberbergischer Kreis"),
    ("05378", "Rheinisch-Bergischer Kreis"),
    ("05382", "Rhein-Sieg-Kreis"),
]
# Monatswerte: 2025-05, 2025-06, 2025-07(=Platzhalter 0) → aktuell = 2025-06
_MONATE = [datetime.datetime(2025, 5, 1), datetime.datetime(2025, 6, 1), datetime.datetime(2025, 7, 1)]
_ARBEITSLOSE = {"05316": [6766, 6817, 0], "05314": [13500, 13710, 0]}
_QUOTEN = {"05316": [7.5, 7.6, "..."], "05314": [7.3, 7.4, "..."]}


def _sheet(wb, werte_je_rs, default):
    ws = wb.active
    ws.title = "Übersicht_Kreise"
    for _ in range(9):  # Vorspann wie im Original (Deckzeilen)
        ws.append([None])
    ws.append(["Gesamt", "Jahresdurch-schnitt", "Gleitender JD", *_MONATE])
    ws.append(["Region", 2025, 2026, 1, 2, 3])
    for rs, name in _REGIONEN:
        werte = werte_je_rs.get(rs, default)
        ws.append([f"{rs} {name}", 0, 0, *werte])


def ba_einzelheft_zip() -> None:
    wb_al = Workbook()
    _sheet(wb_al, _ARBEITSLOSE, [5000, 5010, 0])
    buf_al = io.BytesIO()
    wb_al.save(buf_al)

    wb_q = Workbook()
    _sheet(wb_q, _QUOTEN, [6.0, 6.0, "..."])
    buf_q = io.BytesIO()
    wb_q.save(buf_q)

    with zipfile.ZipFile(FIXTURES / "ba_einzelheft_dlk.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("202506_Heft_pol_SGBI_Arbeitslose.xlsx", buf_al.getvalue())
        zf.writestr("202506_Heft_pol_SGBI_ArbeitslosenQuoten.xlsx", buf_q.getvalue())


if __name__ == "__main__":
    ba_einzelheft_zip()
    print("Fixtures geschrieben nach", FIXTURES)
