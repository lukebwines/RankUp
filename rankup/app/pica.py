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
from incarichi_fields import EXTRA_PICA_FIELDS, EXTRA_SOURCE_KEYS
from xlsx_guard import validate_xlsx


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
        # xlrd è il parser principale; alcune esportazioni PICA .xls
        # contengono record BIFF non gestiti da tutti i reader.
        # Il fallback è stato verificato sul formato effettivo PICA.
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=content, on_demand=True)
            try:
                sheet = book.sheet_by_index(0)
                if sheet.nrows > MAX_ROWS or sheet.ncols > MAX_COLUMNS:
                    raise ValueError("File PICA oltre i limiti di righe o colonne")
                rows = [[sheet.cell_value(i, j) for j in range(sheet.ncols)]
                        for i in range(sheet.nrows)]
            finally:
                book.release_resources()
        except Exception as primary_error:
            from legacy_biff import read_rows
            try:
                rows = list(read_rows(content))
            except Exception as fallback_error:
                raise ValueError(
                    "Impossibile decodificare il formato Excel .xls. "
                    "Lettore principale: " + type(primary_error).__name__ +
                    "; lettore alternativo: " + type(fallback_error).__name__
                ) from fallback_error
        yield from rows
    elif ext == "xlsx":
        from openpyxl import load_workbook
        validate_xlsx(
            content, max_uncompressed=96 * 1024 * 1024,
            max_xml_part=64 * 1024 * 1024,
            max_rows=MAX_ROWS, max_columns=MAX_COLUMNS,
        )
        book = load_workbook(BytesIO(content), read_only=True, data_only=True,
                             keep_links=False)
        try:
            sheet = book.worksheets[0]
            if (sheet.max_row or 0) > MAX_ROWS or (sheet.max_column or 0) > MAX_COLUMNS:
                raise ValueError("File PICA oltre i limiti di righe o colonne")
            # La dimensione dichiarata può essere assente o errata:
            # leggere le celle realmente presenti, senza fidarsi dei metadati.
            sheet.reset_dimensions()
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                if row_number > MAX_ROWS or len(row) > MAX_COLUMNS:
                    raise ValueError("File PICA oltre i limiti effettivi di righe o colonne")
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
        # "ssd" e il settore dell'INCARICO (colonna O nel PICA 26-02).
        # "Settore scientifico disciplinare" e invece un campo del CANDIDATO
        # (colonna BC): non deve essere usato come fallback.
        "ssd": ["ssd", "ssd insegnamento", "ssd copertura"],
        "ul": ["partizione studenti", "codice partizione studenti"],
        "cognome": ["cognome"],
        "nome": ["nome"],
        "codice_fiscale": ["codice fiscale"],
        "stato_pica": ["stato"],
        "occupazione": ["occupazione*", "occupazione"],
        "azienda_ente": ["azienda/ente", "azienda ente", "ente di appartenenza", "datore di lavoro"],
        "profilo_professionale": ["profilo professionale"],
        "ateneo": ["ateneo"],
        "qualifica": ["qualifica"],
        # Campi dichiarati dal candidato: AU (titolo) e AW (conseguimento).
        # Usare le intestazioni e non la posizione delle colonne.
        "titolo_studio": ["diploma di laurea/laurea specialistica/laurea magistrale in"],
        "data_conseguimento": ["in data"],

    }
    fields.update(EXTRA_PICA_FIELDS)
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
    ssd_by_key = {}
    extras_by_key = {}
    keys_seen = set()
    rows_seen: dict[tuple[str, str], dict] = {}
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
                    ("cds", "sede", "insegnamento", "modulo", "id_copertura", "codice_copertura", "ssd", "ul", *EXTRA_SOURCE_KEYS)}
        # I codici di copertura sono preferiti ai nomi. La sede e il CdS
        # restano inclusi per impedire unioni indesiderate.
        stable = incarico["id_copertura"] or incarico["codice_copertura"]
        basis = "|".join([incarico["cds"], incarico["sede"], stable,
                          incarico["insegnamento"], incarico["modulo"], incarico["ul"]])
        incarico["key"] = hashlib.sha256(normalize(basis).encode()).hexdigest()[:24]
        ssd = incarico["ssd"].strip().upper()
        prior = ssd_by_key.get(incarico["key"])
        if prior and ssd and prior != ssd:
            raise ValueError("SSD discordante per la stessa copertura PICA: " + incarico["modulo"])
        if ssd:
            ssd_by_key[incarico["key"]] = ssd
        # Un incarico PICA puo comparire in piu candidature: i metadati
        # devono essere uguali. Conserviamo i valori non vuoti senza inventare
        # quelli assenti, indipendentemente dall'ordine delle righe.
        existing = extras_by_key.setdefault(incarico["key"], {})
        for field in EXTRA_SOURCE_KEYS:
            value = incarico[field].strip()
            previous = existing.get(field, "")
            if value and previous and value != previous:
                raise ValueError(
                    "Valori discordanti del campo " + field +
                    " per lo stesso incarico PICA: " + incarico["modulo"]
                )
            if value:
                existing[field] = value
        row_key = (candidate_id, incarico["key"])
        if row_key in keys_seen:
            duplicate_rows += 1
            prior = rows_seen[row_key]
            # Le esportazioni possono ripetere un incarico; integrare soltanto
            # campi AU/AW mancanti, senza sovrascrivere dati discordanti.
            for field in ("titolo_studio", "data_conseguimento"):
                value = get(field)
                if value and not prior.get(field):
                    prior[field] = value
                elif value and prior.get(field) != value:
                    warnings.append("Riga " + str(row_number) + ": " + field +
                                    " discordante per la stessa candidatura; verificare PICA")
            continue
        keys_seen.add(row_key)
        record = {
            "id_pica": candidate_id,
            "cognome": get("cognome"),
            "nome": get("nome"),
            "codice_fiscale": get("codice_fiscale"),
            "stato_pica": get("stato_pica"),
            "occupazione": get("occupazione"),
            "azienda_ente": get("azienda_ente"),
            "profilo_professionale": get("profilo_professionale"),
            "ateneo": get("ateneo"),
            "qualifica": get("qualifica"),
            "titolo_studio": get("titolo_studio"),
            "data_conseguimento": get("data_conseguimento"),

            "incarico": incarico,
            "source_row": row_number,
        }
        records.append(record)
        rows_seen[row_key] = record
    # Le righe duplicate della stessa copertura possono avere alcuni campi
    # supplementari vuoti: integrare solo da altre righe del medesimo incarico.
    for record in records:
        record["incarico"].update(extras_by_key.get(record["incarico"]["key"], {}))
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
            "ssd_distinti": len(set(ssd_by_key.values())),
            "righe_duplicate_ignorate": duplicate_rows,
            "avvisi": warnings[:30],
        },
    }
