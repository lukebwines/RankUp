"""Esportazione nominativa interna delle graduatorie approvate."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo

from incarichi_fields import normalize_excel_date

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

HEADERS = (
    "Posizione",
    "Bando",
    "CdS",
    "Sede",
    "Insegnamento",
    "Modulo",
    "SSD",
    "UL",
    "Partizione studenti",
    "Codice partizione",
    "ID candidatura RankUP",
    "ID PICA",
    "Cognome",
    "Nome",
    "Codice fiscale",
    "Data di nascita",
    "Sesso",
    "Comune di nascita",
    "Cittadinanza italiana",
    "Cittadinanza",
    "Indirizzo residenza",
    "Comune di residenza",
    "Provincia di residenza",
    "CAP residenza",
    "Nazione di residenza",
    "Titolo di studio",
    "Ateneo di conseguimento",
    "Data conseguimento",
    "Punteggio",
    "Esito",
    "Vincitore",
)

_WIDTHS = (
    11, 12, 34, 18, 42, 42, 14, 18, 25, 18, 21, 14, 24,
    22, 22, 18, 10, 24, 18, 20, 32, 24, 20, 14, 22, 48,
    34, 18, 14, 42, 12,
)

_PERSONAL_FIELDS = (
    "codice_fiscale",
    "residenza_indirizzo",
    "residenza_citta",
    "residenza_provincia",
    "residenza_cap",
    "residenza_nazione",
    "titolo_studio",
    "ateneo_conseguimento",
    "data_conseguimento",
)


def _text(value) -> str:
    return "" if value is None else str(value)


def _score(value):
    if value in (None, ""):
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date_value(value):
    raw = _text(value).strip()
    if not raw:
        return None
    normalized = normalize_excel_date(raw)
    if normalized:
        try:
            return date.fromisoformat(normalized)
        except ValueError:
            pass
    return raw


def _ordered_candidates(job: dict) -> list[dict]:
    return sorted(job.get("candidature", []), key=lambda item: (
        0 if item.get("decisione") == "idoneo" else 1,
        item.get("posizione") if item.get("posizione") is not None else 999999,
        str(item.get("id_pica") or ""),
    ))


def _write_value(cell, value, text: bool = False):
    if text:
        cell.value = _text(value)
        # Forza il tipo stringa anche per valori che iniziano con "=":
        # il contenuto PICA non deve mai diventare una formula Excel.
        cell.data_type = "s"
        cell.number_format = "@"
    else:
        cell.value = value


def render_graduatoria_excel(code: str, data: dict,
                             personal_by_id: dict[int, dict]) -> bytes:
    """Crea un report interno: graduatoria dallo snapshot, anagrafica da PICA."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Graduatoria"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A6"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.page_margins.left = 0.25
    sheet.page_margins.right = 0.25
    sheet.print_title_rows = "1:5"

    last_column = len(HEADERS)
    last_letter = sheet.cell(row=1, column=last_column).column_letter
    sheet.merge_cells(f"A1:{last_letter}1")
    title = sheet["A1"]
    title.value = "RANKUP · GRADUATORIA NOMINATIVA COMPLETA · USO INTERNO"
    title.font = Font(name="Aptos Display", size=15, bold=True, color="FFFFFF")
    title.fill = PatternFill("solid", fgColor="173B5B")
    title.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 30

    _write_value(sheet["A2"], "Bando", text=True)
    _write_value(sheet["B2"], code, text=True)
    _write_value(sheet["D2"], "Stato", text=True)
    _write_value(sheet["E2"], "ATTI APPROVATI", text=True)
    for address in ("A2", "D2"):
        sheet[address].font = Font(bold=True, color="173B5B")
    sheet.merge_cells(f"A3:{last_letter}3")
    _write_value(
        sheet["A3"],
        "Una riga per ogni candidatura e incarico. Le posizioni, i punteggi e gli esiti "
        "derivano dallo snapshot approvato; i dati personali derivano dall'importazione PICA.",
        text=True,
    )
    sheet["A3"].font = Font(italic=True, color="526477", size=9)
    sheet["A3"].alignment = Alignment(wrap_text=True, vertical="center")
    sheet.row_dimensions[3].height = 28

    header_row = 5
    for column, label in enumerate(HEADERS, 1):
        cell = sheet.cell(row=header_row, column=column)
        _write_value(cell, label, text=True)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="245477")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.number_format = "@"
    sheet.row_dimensions[header_row].height = 32

    row_number = header_row + 1
    for scope in data.get("selezioni", []):
        for job in scope.get("incarichi", []):
            winner_id = job.get("vincitore_id")
            for candidate in _ordered_candidates(job):
                candidatura_id = candidate.get("candidatura_id")
                personal = personal_by_id.get(candidatura_id, {}) if candidatura_id is not None else {}
                position = candidate.get("posizione")
                is_winner = bool(
                    winner_id is not None
                    and candidatura_id == winner_id
                    and position == 1
                )
                values = (
                    position if position is not None else None,
                    code,
                    scope.get("cds", ""),
                    scope.get("sede", ""),
                    job.get("insegnamento", ""),
                    job.get("modulo", ""),
                    job.get("ssd", ""),
                    job.get("ul", ""),
                    job.get("partizione_studenti", ""),
                    job.get("codice_partizione", ""),
                    candidatura_id,
                    candidate.get("id_pica", ""),
                    candidate.get("cognome", ""),
                    candidate.get("nome", ""),
                    personal.get("codice_fiscale", ""),
                    _date_value(personal.get("data_nascita")),
                    personal.get("sesso", ""),
                    personal.get("comune_nascita", ""),
                    personal.get("cittadinanza_italiana", ""),
                    personal.get("cittadinanza", ""),
                    personal.get("residenza_indirizzo", ""),
                    personal.get("residenza_citta", ""),
                    personal.get("residenza_provincia", ""),
                    personal.get("residenza_cap", ""),
                    personal.get("residenza_nazione", ""),
                    personal.get("titolo_studio", ""),
                    personal.get("ateneo_conseguimento", ""),
                    _date_value(personal.get("data_conseguimento")),
                    _score(candidate.get("punteggio")),
                    candidate.get("esito", ""),
                    "Sì" if is_winner else "No",
                )
                for column, value in enumerate(values, 1):
                    cell = sheet.cell(row=row_number, column=column)
                    header = HEADERS[column - 1]
                    numeric = header in ("Posizione", "ID candidatura RankUP", "Punteggio")
                    is_date = header in ("Data di nascita", "Data conseguimento") and isinstance(value, date)
                    _write_value(cell, value, text=not numeric and not is_date)
                    cell.alignment = Alignment(
                        vertical="top",
                        wrap_text=header in (
                            "CdS", "Insegnamento", "Modulo", "Partizione studenti",
                            "Comune di nascita", "Indirizzo residenza",
                            "Comune di residenza", "Nazione di residenza",
                            "Titolo di studio", "Ateneo di conseguimento", "Esito",
                        ),
                    )
                    if is_date:
                        cell.number_format = "dd/mm/yyyy"
                    elif header == "Punteggio" and value is not None:
                        cell.number_format = "0.00"
                if is_winner:
                    for cell in sheet[row_number]:
                        cell.font = Font(bold=True, color="173B5B")
                row_number += 1

    for column, width in enumerate(_WIDTHS, 1):
        sheet.column_dimensions[sheet.cell(row=1, column=column).column_letter].width = width

    last_row = max(header_row + 1, row_number - 1)
    sheet.auto_filter.ref = f"A{header_row}:{last_letter}{last_row}"
    if row_number > header_row + 1:
        table = Table(displayName="GraduatoriaNominativa",
                      ref=f"A{header_row}:{last_letter}{row_number - 1}")
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        sheet.add_table(table)

    sources = workbook.create_sheet("Fonti e criteri")
    sources.sheet_view.showGridLines = False
    sources.column_dimensions["A"].width = 30
    sources.column_dimensions["B"].width = 100
    source_rows = (
        ("Elemento", "Fonte e criterio"),
        ("Graduatoria", "Posizioni, punteggi, esiti e vincitore derivano dallo snapshot della graduatoria approvata. Non vengono ricalcolati."),
        ("Anagrafica", "Cognome, nome, data e comune di nascita, sesso, cittadinanza e codice fiscale derivano dalla candidatura PICA associata all'ID PICA e all'incarico."),
        ("Residenza", "PICA: Residenza - indirizzo, Residenza - città, Residenza - provincia, Residenza - CAP, Residenza - nazione."),
        ("Titolo di studio", "PICA AU: Diploma di Laurea/Laurea Specialistica/Laurea Magistrale in."),
        ("Ateneo di conseguimento", "PICA AV: Conseguito presso l'università di."),
        ("Data conseguimento", "PICA AW: In data."),
        ("Campi vuoti", "Un campo vuoto indica che il dato non è valorizzato nella fonte PICA disponibile. RankUP non lo ricostruisce né lo deduce."),
        ("Righe", "Una riga per ogni candidatura e incarico. Lo stesso ID PICA può comparire più volte se associato a incarichi differenti."),
        ("Uso", "Documento nominativo interno contenente dati personali. Non costituisce la graduatoria pubblica né sostituisce l'atto approvato."),
    )
    for row_index, values in enumerate(source_rows, 1):
        for column, value in enumerate(values, 1):
            cell = sources.cell(row=row_index, column=column)
            _write_value(cell, value, text=True)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if row_index == 1:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="245477")
            else:
                cell.font = Font(size=10)
        sources.row_dimensions[row_index].height = 34 if row_index > 1 else 24
    sources.freeze_panes = "A2"
    sources.auto_filter.ref = f"A1:B{len(source_rows)}"

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
