"""Importazione delle commissioni dal foglio Excel ufficiale.

L'anno e il codice bando vengono letti dal file; la composizione è
memorizzata come fotografia del singolo bando, non aggiornata in cascata.
"""
from __future__ import annotations
from io import BytesIO
import re
from collections import defaultdict
from openpyxl import load_workbook
from pica import clean, normalize

MAX_COMMISSION_FILE = 4 * 1024 * 1024

def parse_commissioni(filename: str, content: bytes, expected_code: str) -> dict:
    if not filename.lower().endswith(".xlsx"):
        raise ValueError("Il file delle commissioni deve essere .xlsx")
    if not content or len(content) > MAX_COMMISSION_FILE:
        raise ValueError("File commissioni vuoto o superiore a 4 MB")
    book = load_workbook(BytesIO(content), read_only=True, data_only=True)
    try:
        sheet = book.worksheets[0]
        if (sheet.max_row or 0) > 10000 or (sheet.max_column or 0) > 100:
            raise ValueError("File commissioni oltre i limiti previsti")
        it = sheet.iter_rows(values_only=True)
        try:
            head = next(it)
        except StopIteration:
            raise ValueError("File commissioni vuoto")
        indexes = {normalize(v):i for i,v in enumerate(head) if clean(v)}
        fields = {
            "cds": "corso di laurea",
            "sede": "sede",
            "presidente": "presidente",
            "componenti": "componenti commissione",
            "bando": "bando",
            "requisiti": "requisiti",
            "anno": "anno",
        }
        missing = [v for v in fields.values() if v not in indexes]
        if missing:
            raise ValueError("Intestazioni commissioni mancanti: " + ", ".join(missing))
        rows = []
        compositions = {}
        for row_no, row in enumerate(it, start=2):
            def get(key):
                idx = indexes[fields[key]]
                return clean(row[idx]) if idx < len(row) else ""
            cds,sede = get("cds"),get("sede")
            if not cds and not sede:
                continue
            if not cds or not sede or not get("presidente"):
                raise ValueError("Commissione incompleta alla riga " + str(row_no))
            text_bando=get("bando")
            found=re.search(r"([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)",text_bando)
            if not found or found.group(1).upper()!=expected_code.upper():
                raise ValueError("Codice bando diverso alla riga " + str(row_no))
            comp=(get("presidente"),get("componenti"))
            previous=compositions.get(normalize(cds))
            if previous and previous!=comp:
                raise ValueError("Composizioni discordanti per il CdS " + cds + ": verificare il file")
            compositions[normalize(cds)]=comp
            rows.append({
                "cds":cds,"sede":sede,"presidente":get("presidente"),
                "componenti":get("componenti"),"requisiti":get("requisiti"),
                "anno":get("anno"),
            })
        if not rows:
            raise ValueError("Nessuna commissione individuata")
        keys=[(normalize(x["cds"]),normalize(x["sede"])) for x in rows]
        if len(keys)!=len(set(keys)):
            raise ValueError("Commissioni CdS/sede duplicate nel file")
        return {"rows":rows,"numero_cds":len(compositions),"numero_sedi":len(rows)}
    finally:
        book.close()
