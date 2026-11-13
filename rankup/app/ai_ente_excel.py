"""Esportazione Excel della verifica requisito ente (sola lettura).

Le righe provengono dalle dichiarazioni PICA e dalle valutazioni già acquisite.
Nessuna classificazione viene aggiunta e nessun dato viene inviato a GroqCloud.
"""
from __future__ import annotations

from collections import Counter
from io import BytesIO
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEADERS = [
    "ID PICA","Cognome","Nome","Corso di Studio","Sede","Insegnamento",
    "Requisito ente","BB - Azienda/ente","BD - Ateneo","BM - Occupazione",
    "BA - Profilo professionale","BO - Qualifica","Esito AI",
    "Motivo approfondimento","Motivazione AI","Verifiche AI","Modello AI",
    "Storico AI","Campi PICA utilizzati","Esito Commissione",
    "Stato graduatoria","Posizione","Punteggio","Graduatoria definitiva"
]
SAFE_FORMULA = re.compile(r"^\s*[=+\-@]")
BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
FIELDS = ("Azienda/ente","Ateneo","Occupazione","Profilo professionale","Qualifica")

NAVY = "16334E"
BLUE = "214D73"
PALE = "EAF2FA"
WHITE = "FFFFFF"
INK = "20344A"
MUTED = "61758A"
GREEN = "DDF3E5"
AMBER = "FFF0D0"
RED = "FCE2E1"


def _text(value: object, missing: str = "") -> str:
    """Sempre testo Excel; neutralizza formule inserite nei campi PICA."""
    value = BAD_CONTROL.sub("", str(value if value is not None else ""))
    value = value[:32760]
    if not value.strip():
        return missing
    return ("'" + value) if SAFE_FORMULA.match(value) else value


def _write_text(cell,value:object,missing:str=""):
    cell.value = _text(value,missing)
    cell.data_type = "s"
    cell.alignment = Alignment(vertical="top",wrap_text=True)


def _write_sheet(sheet, headers:list[str], values:list[list[object]],
                 widths:list[int], table_name:str):
    sheet.sheet_view.showGridLines=False
    sheet.freeze_panes="C2" if len(headers)>2 else "A2"
    for col,label in enumerate(headers,1):
        cell=sheet.cell(1,col)
        _write_text(cell,label)
        cell.fill=PatternFill("solid",fgColor=NAVY)
        cell.font=Font(name="Aptos",size=10,bold=True,color=WHITE)
        cell.alignment=Alignment(vertical="center",wrap_text=True)
        sheet.column_dimensions[get_column_letter(col)].width=widths[col-1]
    sheet.row_dimensions[1].height=38
    for index,row in enumerate(values,2):
        for col,value in enumerate(row,1):
            cell=sheet.cell(index,col)
            if isinstance(value,(int,float)) and not isinstance(value,bool):
                cell.value=value
                if headers[col-1]=="Punteggio":
                    cell.number_format="0.00"
                elif headers[col-1]=="Posizione":
                    cell.number_format="0"
                cell.alignment=Alignment(vertical="top")
            else:
                _write_text(cell,value)
            cell.font=Font(name="Aptos",size=10,color=INK)
            if index%2==0:
                cell.fill=PatternFill("solid",fgColor="F5F9FD")
        sheet.row_dimensions[index].height=38
    if len(values):
        ref=f"A1:{get_column_letter(len(headers))}{len(values)+1}"
        table=Table(displayName=table_name,ref=ref)
        table.tableStyleInfo=TableStyleInfo(name="TableStyleMedium2",
            showFirstColumn=False,showLastColumn=False,
            showRowStripes=True,showColumnStripes=False)
        sheet.add_table(table)
    else:
        sheet.auto_filter.ref=f"A1:{get_column_letter(len(headers))}1"
    sheet.sheet_properties.pageSetUpPr.fitToPage=True
    sheet.page_setup.fitToWidth=1
    sheet.print_options.horizontalCentered=True


def _status_colors(sheet, values:list[list[object]]):
    ai_col=HEADERS.index("Esito AI")+1
    commission_col=HEADERS.index("Esito Commissione")+1
    for idx,row in enumerate(values,2):
        ai=str(row[ai_col-1] or "").upper()
        status=str(row[commission_col-1] or "").upper()
        for column,name in ((ai_col,ai),(commission_col,status)):
            cell=sheet.cell(idx,column)
            if ("COERENTE" in name and "NON" not in name) or (
                "AMMESSO IN GRADUATORIA" in name and "NON" not in name):
                fill=GREEN
            elif "NON IDONEO" in name or "ESCLUSO" in name:
                fill=RED
            elif "APPROFONDIRE" in name or "PROVVISORIA" in name or "VERIFICA" in name:
                fill=AMBER
            else:
                continue
            cell.fill=PatternFill("solid",fgColor=fill)
            cell.font=Font(name="Aptos",size=10,bold=True,color=INK)


def render_ente_report_xlsx(code:str,cds:str,sede:str,
                            rows:list[dict],filters:dict[str,str]) -> bytes:
    """Un record per candidatura, senza dedurre punteggi o posizioni mancanti."""
    wb=Workbook()
    sheet=wb.active
    sheet.title="Candidature"
    data=[]
    audit=[]
    for item in rows:
        orig=item.get("campi_pica_originali") or {}
        outbound=item.get("campi_pica_ai") or {}
        ai=item.get("ai") or {}
        comm=item.get("commissione") or {}
        model=str(ai.get("modello") or item.get("modello_ai") or "")
        score=comm.get("punteggio")
        numeric_score=None
        if score is not None and str(score).strip():
            try:
                numeric_score=float(score)
            except (ValueError,TypeError,OverflowError):
                numeric_score=None
        position=comm.get("posizione")
        numeric_position=position if type(position) is int and position>0 else None
        sources=item.get("fonti_pica") or []
        has_ai=bool(ai)
        historic=bool(item.get("valutazione_storica"))
        record=[
            str(item.get("id_pica") or ""),item.get("cognome"),item.get("nome"),
            item.get("cds"),item.get("sede"),item.get("insegnamento"),
            item.get("requisito"),
            *(orig.get(field) or "Non dichiarato" for field in FIELDS),
            item.get("esito"),item.get("tipo_approfondimento"),
            ai.get("motivo") or item.get("motivo") or "",
            item.get("verifiche") or "",model,
            ("Sì" if historic else "No") if has_ai else "AI non eseguita",
            ", ".join(sources) if has_ai and not historic else "",
            comm.get("stato_commissione") or "Da valutare",
            comm.get("stato_graduatoria") or "da_valutare",
            numeric_position,numeric_score,
            "Sì" if comm.get("graduatoria_definitiva") else "No",
        ]
        data.append(record)
        audit.append([
            str(item.get("id_pica") or ""),
            model,
            "Valutazione storica" if historic else "AI eseguita" if has_ai else "AI non eseguita",
            *(outbound.get(field) or "NON DICHIARATO" for field in FIELDS),
            ", ".join(sources) if has_ai and not historic else "",
        ])
    widths=[16,23,20,34,19,39,20,39,39,42,42,32,23,28,42,42,31,17,34,
            33,23,13,13,22]
    _write_sheet(sheet,HEADERS,data,widths,"RankUPVerificaEnte")
    _status_colors(sheet,data)
    audit_sheet=wb.create_sheet("Valori AI")
    audit_headers=["ID PICA","Modello AI","Analisi","BB minimizzato",
                   "BD minimizzato","BM minimizzato","BA minimizzato",
                   "BO minimizzato","Campi utilizzati"]
    _write_sheet(audit_sheet,audit_headers,audit,
                 [16,31,22,39,39,42,42,32,40],"RankUPTracciatoAI")
    summary=wb.create_sheet("Riepilogo")
    summary.sheet_view.showGridLines=False
    summary.column_dimensions["A"].width=31
    summary.column_dimensions["B"].width=62
    summary.merge_cells("A1:B1")
    _write_text(summary["A1"],"RANKUP · VERIFICA REQUISITO ENTE")
    summary["A1"].fill=PatternFill("solid",fgColor=NAVY)
    summary["A1"].font=Font(name="Aptos Display",size=16,bold=True,color=WHITE)
    summary["A1"].alignment=Alignment(vertical="center")
    summary.row_dimensions[1].height=37
    metadata=[
        ("Bando",code),("CdS",cds or "Tutti"),("Sede",sede or "Tutte"),
        ("Esito AI",filters.get("esito") or "Tutti"),
        ("Motivo approfondimento",filters.get("tipo") or "Tutti"),
        ("Stato graduatoria",filters.get("graduatoria") or "Tutti"),
        ("Ricerca candidato / ID",filters.get("ricerca") or "Nessuna"),
        ("Candidature esportate",len(data)),
    ]
    for idx,(label,value) in enumerate(metadata,3):
        _write_text(summary.cell(idx,1),label)
        label_cell=summary.cell(idx,1)
        label_cell.font=Font(name="Aptos",size=10,bold=True,color=BLUE)
        _write_text(summary.cell(idx,2),value)
        summary.cell(idx,2).font=Font(name="Aptos",size=10,color=INK)
        if idx%2:
            for col in (1,2):
                summary.cell(idx,col).fill=PatternFill("solid",fgColor=PALE)
        summary.row_dimensions[idx].height=23
    dist=Counter(str(row[HEADERS.index("Esito AI")] or "") for row in data)
    start=13
    for i,(label,total) in enumerate(sorted(dist.items()),start):
        _write_text(summary.cell(i,1),label or "Senza esito")
        summary.cell(i,2).value=total
    summary.merge_cells("A19:B19")
    summary["A19"]="Uso istruttorio: la coerenza dell'ente non prova il rapporto lavorativo."
    summary["A19"].font=Font(name="Aptos",size=10,italic=True,color=MUTED)
    summary.merge_cells("A20:B20")
    summary["A20"]="Le posizioni definitive derivano dai dati archiviati in RankUP; nessuna idoneità è dedotta dall'AI."
    summary["A20"].font=Font(name="Aptos",size=10,italic=True,color=MUTED)
    summary.merge_cells("A21:B21")
    summary["A21"]="Nel foglio 'Valori AI' sono riportati i testi minimizzati, non necessariamente inviati al provider."
    summary["A21"].font=Font(name="Aptos",size=10,italic=True,color=MUTED)
    wb.active=0
    target=BytesIO()
    wb.save(target)
    return target.getvalue()
