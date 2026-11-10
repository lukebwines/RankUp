"""Preflight XLSX ostili prima di openpyxl, con uso limitato di memoria.

Valida lo ZIP e lo stream XML senza materializzare i file decompressi.
La scansione delle coordinate dei fogli NON si affida al campo dimension
dichiarato, che può mancare o essere ingannevole. openpyxl beneficia inoltre
del parser defusedxml installato come dipendenza obbligatoria.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import PurePosixPath
import re
from xml.parsers import expat
from zipfile import ZipFile, BadZipFile

MAX_ZIP_ENTRIES = 2000
READ_CHUNK = 1024 * 1024

_CELL_REF = re.compile(r"([A-Za-z]+)([1-9][0-9]*)\Z")
_DIMENSION_REF = re.compile(r"([A-Za-z]+[1-9][0-9]*)(?::([A-Za-z]+[1-9][0-9]*))?\Z")


def _column_number(letters: str) -> int:
    number = 0
    for ch in letters.upper():
        number = number * 26 + (ord(ch) - 64)
        if number > 16384:
            return number
    return number


def _cell_position(value: str) -> tuple[int, int]:
    matched = _CELL_REF.fullmatch(value)
    if not matched:
        raise ValueError("Riferimento di cella XLSX non valido")
    return int(matched.group(2)), _column_number(matched.group(1))


def _check_position(value: str, max_rows: int, max_columns: int) -> None:
    row, col = _cell_position(value)
    if row > max_rows or col > max_columns:
        raise ValueError("Il foglio Excel supera il limite effettivo di righe o colonne")


def _xml_parser(is_worksheet: bool, max_rows: int, max_columns: int):
    parser = expat.ParserCreate(namespace_separator="}")
    seen_rows = 0

    def reject_doctype(*_args):
        raise ValueError("XML Excel con DOCTYPE o entità non consentite")

    parser.StartDoctypeDeclHandler = reject_doctype
    parser.EntityDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = lambda *_args: 0
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def start(tag: str, attrs: dict) -> None:
        nonlocal seen_rows
        if not is_worksheet:
            return
        local = tag.rsplit("}", 1)[-1]
        if local == "row":
            seen_rows += 1
            if seen_rows > max_rows:
                raise ValueError("Il foglio Excel supera il limite effettivo di righe")
            ref = attrs.get("r")
            if ref and (not ref.isdecimal() or int(ref) > max_rows):
                raise ValueError("Il foglio Excel supera il limite effettivo di righe")
        elif local == "c":
            ref = attrs.get("r")
            if ref:
                _check_position(ref, max_rows, max_columns)
        elif local == "dimension":
            ref = attrs.get("ref", "")
            if ref:
                dim = _DIMENSION_REF.fullmatch(ref)
                if dim:
                    _check_position(dim.group(1), max_rows, max_columns)
                    if dim.group(2):
                        _check_position(dim.group(2), max_rows, max_columns)

    parser.StartElementHandler = start
    return parser


def validate_xlsx(
    content: bytes, *,
    max_uncompressed: int,
    max_xml_part: int,
    max_rows: int,
    max_columns: int,
) -> None:
    """Valida ZIP, CRC, limiti decompressi e coordinate reali dei fogli.

    Non ritorna dati PICA o commissioni. Dopo il controllo il file viene
    letto normalmente da openpyxl (con defusedxml) in modalità read_only.
    """
    try:
        with ZipFile(BytesIO(content), "r") as book:
            items = book.infolist()
            if not items or len(items) > MAX_ZIP_ENTRIES:
                raise ValueError("Numero eccessivo di file interni nell'XLSX")
            names = set()
            total = 0
            worksheets = 0
            for info in items:
                name = info.filename
                normalized = PurePosixPath(name)
                if (
                    not name or "\\" in name or name.startswith("/")
                    or any(part in ("", ".", "..") for part in name.split("/"))
                    or normalized.is_absolute() or "\x00" in name
                    or ((info.external_attr >> 16) & 0o170000) == 0o120000
                    or info.flag_bits & 1
                ):
                    raise ValueError("Percorso interno XLSX non consentito")
                if name.casefold() in names:
                    raise ValueError("File interno duplicato nell'XLSX")
                names.add(name.casefold())
                if info.is_dir():
                    continue
                total += info.file_size
                if total > max_uncompressed:
                    raise ValueError("XLSX troppo grande dopo la decompressione")
                if info.file_size > max_uncompressed:
                    raise ValueError("File interno XLSX troppo grande")
                is_xml = name.lower().endswith((".xml", ".rels"))
                if is_xml and info.file_size > max_xml_part:
                    raise ValueError("Documento XML XLSX troppo grande")
                is_sheet = (
                    name.startswith("xl/worksheets/")
                    and name.lower().endswith(".xml")
                )
                if is_sheet:
                    worksheets += 1
                parser = _xml_parser(is_sheet, max_rows, max_columns) if is_xml else None
                expanded = 0
                with book.open(info, "r") as stream:
                    while True:
                        chunk = stream.read(READ_CHUNK)
                        if not chunk:
                            break
                        expanded += len(chunk)
                        if expanded > info.file_size or expanded > max_uncompressed:
                            raise ValueError("Decompressione XLSX oltre i limiti")
                        if parser:
                            parser.Parse(chunk, False)
                    if expanded != info.file_size:
                        raise ValueError("Dimensione interna XLSX non coerente")
                    if parser:
                        parser.Parse(b"", True)
            if not worksheets or not {"[content_types].xml", "xl/workbook.xml"}.issubset(names):
                raise ValueError("File XLSX privo della struttura Excel necessaria")
    except (BadZipFile, EOFError, expat.ExpatError) as exc:
        raise ValueError("Archivio XLSX danneggiato o XML non valido") from exc
