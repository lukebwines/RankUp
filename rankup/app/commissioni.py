"""Gestione delle commissioni per singolo bando.

Importazione Excel coerente con il format scaricabile, senza applicare
modifiche in cascata ai bandi. Non introduce dati di persone o requisiti.
"""
from __future__ import annotations

from io import BytesIO
import re
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.worksheet.datavalidation import DataValidation
from pica import clean, normalize
from commission_match import course_key, seat_key

MAX_COMMISSION_FILE = 4 * 1024 * 1024
HEADERS = (
    "CORSO DI LAUREA", "SEDE", "PRESIDENTE",
    "COMPONENTI COMMISSIONE", "BANDO", "REQUISITI", "ANNO",
)
FIELD_NAMES = ("cds", "sede", "presidente", "componenti", "bando", "requisiti", "anno")


def parse_commissioni(filename: str, content: bytes, expected_code: str) -> dict:
    if not filename.lower().endswith(".xlsx"):
        raise ValueError("Il file delle commissioni deve essere .xlsx")
    if not content or len(content) > MAX_COMMISSION_FILE:
        raise ValueError("File commissioni vuoto o superiore a 4 MB")
    book = load_workbook(BytesIO(content), read_only=True, data_only=True)
    try:
        sheet = book["Commissioni"] if "Commissioni" in book.sheetnames else book.worksheets[0]
        if (sheet.max_row or 0) > 10000 or (sheet.max_column or 0) > 100:
            raise ValueError("File commissioni oltre i limiti previsti")
        it = sheet.iter_rows(values_only=True)
        try:
            head = next(it)
        except StopIteration:
            raise ValueError("File commissioni vuoto")
        indices = {normalize(v): i for i, v in enumerate(head) if clean(v)}
        missing = [label for label in HEADERS if normalize(label) not in indices]
        if missing:
            raise ValueError("Intestazioni commissioni mancanti: " + ", ".join(missing))

        rows = []
        keys_seen = set()
        for row_no, cells in enumerate(it, start=2):
            def get(label):
                idx = indices[normalize(label)]
                return clean(cells[idx]) if idx < len(cells) else ""

            values = dict(zip(FIELD_NAMES, (get(name) for name in HEADERS)))
            if not values["cds"] and not values["sede"]:
                if any(values[key] for key in
                       ("presidente", "componenti", "requisiti", "anno")):
                    raise ValueError("Riga incompleta nel file commissioni: " + str(row_no))
                continue   # righe vuote del modello, con BANDO gia' precompilato
            if not values["cds"] or not values["sede"] or not values["presidente"]:
                raise ValueError("CdS, sede e presidente obbligatori alla riga " + str(row_no))
            bando_value = values["bando"]
            code = re.search(r"(?<![A-Za-z0-9])([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)", bando_value)
            if not code or code.group(1).upper() != expected_code.upper():
                raise ValueError("Codice bando diverso o mancante alla riga " + str(row_no))
            key = (course_key(values["cds"]), seat_key(values["sede"]))
            if key in keys_seen:
                raise ValueError(
                    "CdS/sede duplicati o equivalenti alla riga " + str(row_no)
                )
            keys_seen.add(key)
            rows.append({
                "cds": values["cds"], "sede": values["sede"],
                "presidente": values["presidente"],
                "componenti": values["componenti"],
                "requisiti": values["requisiti"], "anno": values["anno"],
            })
        if not rows:
            raise ValueError("Nessuna commissione individuata")
        return {
            "rows": rows,
            "numero_cds": len({course_key(x["cds"]) for x in rows}),
            "numero_sedi": len(rows),
        }
    finally:
        book.close()


def make_commission_template(code: str, current: list[dict] | None = None) -> bytes:
    """Modello editabile: stesso schema dell'importazione, senza dati inventati.

    Se current e' valorizzato, restituisce soltanto le commissioni gia'
    registrate nel bando, senza trarre dati da altri bandi.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Commissioni"
    ws.append(HEADERS)
    for row in (current or []):
        ws.append((
            row["cds"], row["sede"], row["presidente"],
            row.get("componenti", ""), "Bando " + code,
            row.get("requisiti", ""), row.get("anno", ""),
        ))
    start = len(current or []) + 2
    for number in range(start, max(start + 30, 52)):
        ws.cell(number, 5, "Bando " + code)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = "A1:G" + str(max(51, ws.max_row))
    widths = [48, 23, 38, 75, 20, 73, 19]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[chr(64+i)].width = width
    header_fill = PatternFill("solid", fgColor="193653")
    for c in ws[1]:
        c.fill = header_fill
        c.font = Font(name="Aptos", bold=True, color="FFFFFF")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 30
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=7):
        for cell in row:
            cell.font = Font(name="Aptos", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        for col in (1, 2, 3):
            row[col-1].fill = PatternFill("solid", fgColor="EDF4FA")
    sheet = wb.create_sheet("Istruzioni")
    notes = [
        "MODELLO COMMISSIONI - RANKUP",
        "Compilare una riga per ogni CdS e sede. Non inserire dati ipotetici.",
        "Le intestazioni nella prima riga del foglio 'Commissioni' non vanno modificate.",
        "Il campo BANDO è precompilato con il codice del bando attivo.",
        "Campi minimi: CORSO DI LAUREA, SEDE, PRESIDENTE. Inserire i componenti quando disponibili.",
        "REQUISITI e ANNO si riportano solo se risultano dalla fonte.",
        "Un CdS con più sedi richiede righe distinte; non utilizzare automaticamente la commissione di un'altra sede.",
        "Importazione standard: aggiunge soltanto nuove commissioni; non sovrascrive quelle presenti.",
        "Per cambiare le commissioni esistenti via Excel selezionare esplicitamente 'Aggiorna le commissioni esistenti'.",
        "In alternativa, modificare la singola commissione direttamente nell'interfaccia.",
        "Il file è riferito al solo bando indicato. Le modifiche non si propagano agli altri bandi.",
    ]
    for row, note in enumerate(notes, start=1):
        cell = sheet.cell(row, 1, note)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        if row == 1:
            cell.font = Font(name="Aptos", bold=True, color="FFFFFF", size=14)
            cell.fill = header_fill
        else:
            cell.font = Font(name="Aptos", size=11)
        sheet.row_dimensions[row].height = 29 if row != 1 else 38
    sheet.column_dimensions["A"].width = 105
    wb.active = 0
    output = BytesIO()
    wb.save(output)
    wb.close()
    return output.getvalue()
