"""Lettura dell'esportazione candidature PICA (.xls/.xlsx).

I dati vengono restituiti in memoria, non inviati a servizi esterni.
"""
from __future__ import annotations

from io import BytesIO
import re
import unicodedata
import hashlib
from collections import Counter
from typing import Any


MAX_ROWS = 20000
MAX_COLUMNS = 300
MAX_FILE_BYTES = 12 * 1024 * 1024


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).casefold()


def _read_rows(filename: str, content: bytes):
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext == "xls":
        import xlrd
        book = xlrd.open_workbook(file_contents=content, on_demand=True)
        try:
            sheet = book.sheet_by_index(0)
            if sheet.nrows > MAX_ROWS or sheet.ncols > MAX_COLUMNS:
                raise ValueError("File PICA oltre i limiti di righe o colonne")
            for i in range(sheet.nrows):
                yield [sheet.cell_value(i, j) for j in range(sheet.ncols)]
        finally:
            book.release_resources()
    elif ext == "xlsx":
        from openpyxl import load_workbook
        book = load_workbook(BytesIO(content), read_only=True, data_only=True)
        try:
            sheet = book.worksheets[0]
            if (sheet.max_row or 0) > MAX_ROWS or (sheet.max_column or 0) > MAX_COLUMNS:
                raise ValueError("File PICA oltre i limiti di righe o colonne")
            for row in sheet.iter_rows(values_only=True):
                yield list(row)
        finally:
            book.close()
    else:
        raise ValueError("Sono ammessi soltanto file Excel .xls o .xlsx")


def extract_code(title: str) -> str:
    # Esempio effettivamente riscontrato: "univr 26-02 - Esportazione dati".
    match = re.search(r"\bunivr\s+([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)\s*[-–]\s*esportazione", title, flags=re.I)
    if not match:
        raise ValueError("Codice del bando non riconosciuto nella prima riga PICA")
    return match.group(1).upper()


def parse_pica(filename: str, content: bytes) -> dict:
    if not content or len(content) > MAX_FILE_BYTES:
        raise ValueError("File vuoto o superiore a 12 MB")

    lines = iter(_read_rows(filename, content))
    try:
        title_row = next(lines)
        header_row = next(lines)
    except StopIteration as exc:
        raise ValueError("Il file non contiene intestazioni e candidature") from exc

    code = extract_code(" ".join(clean(v) for v in title_row if clean(v)))
    indexes: dict[str, int] = {}
    for i, value in enumerate(header_row):
        key = normalize(value)
        if key and key not in indexes:
            indexes[key] = i

    fields = {
        "id_pica": ["id domanda"],
        "cds": ["corso di studio"],
        "sede": ["sede"],
        "insegnamento": ["nome insegnamento padre"],
        "modulo": ["descrizione copertura"],
        "id_copertura": ["id copertura"],
        "codice_copertura": ["codice copertura"],
        "ul": ["partizione studenti", "codice partizione studenti"],
        "cognome": ["cognome"],
        "nome": ["nome"],
        "codice_fiscale": ["codice fiscale"],
        "stato_pica": ["stato"],
    }
    matched: dict[str, int] = {}
    for canonical, aliases in fields.items():
        for alias in aliases:
            if normalize(alias) in indexes:
                matched[canonical] = indexes[normalize(alias)]
                break

    necessary = ("id_pica", "cds", "sede", "insegnamento", "modulo", "cognome", "nome")
    missing = [f for f in necessary if f not in matched]
    if missing:
        raise ValueError("Colonne obbligatorie PICA assenti: " + ", ".join(missing))

    records = []
    keys_seen = set()
    duplicate_rows = 0
    warnings = []
    for row_number, row in enumerate(lines, start=3):
        if not any(clean(v) for v in row):
            continue
        get = lambda key: clean(row[matched[key]]) if key in matched and matched[key] < len(row) else ""
        candidate_id = get("id_pica")
        if not candidate_id:
            warnings.append("Riga " + str(row_number) + ": ID domanda mancante, ignorata")
            continue
        if not get("cds") or not get("modulo"):
            warnings.append("Riga " + str(row_number) + ": CdS o modulo mancante, ignorata")
            continue

        incarico = {k: get(k) for k in
                    ("cds", "sede", "insegnamento", "modulo", "id_copertura", "codice_copertura", "ul")}
        # I codici di copertura sono preferiti ai nomi. La sede e il CdS
        # restano inclusi per impedire unioni indesiderate.
        stable = incarico["id_copertura"] or incarico["codice_copertura"]
        basis = "|".join([incarico["cds"], incarico["sede"], stable,
                          incarico["insegnamento"], incarico["modulo"], incarico["ul"]])
        incarico["key"] = hashlib.sha256(normalize(basis).encode()).hexdigest()[:24]
        row_key = (candidate_id, incarico["key"])
        if row_key in keys_seen:
            duplicate_rows += 1
            continue
        keys_seen.add(row_key)
        records.append({
            "id_pica": candidate_id,
            "cognome": get("cognome"),
            "nome": get("nome"),
            "codice_fiscale": get("codice_fiscale"),
            "stato_pica": get("stato_pica"),
            "incarico": incarico,
            "source_row": row_number,
        })
    if not records:
        raise ValueError("Nessuna candidatura utilizzabile trovata nel file")
    assignments = {record["incarico"]["key"] for record in records}
    courses = {record["incarico"]["cds"] for record in records}
    return {
        "code": code,
        "source_title": " ".join(clean(v) for v in title_row if clean(v)),
        "rows": records,
        "summary": {
            "candidature": len(records),
            "incarichi": len(assignments),
            "cds": len(courses),
            "righe_duplicate_ignorate": duplicate_rows,
            "avvisi": warnings[:30],
        },
    }
