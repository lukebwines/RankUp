"""Report istruttorio leggibile per la verifica del requisito di ente.

Il prospetto e' costruito esclusivamente dai dati PICA gia' acquisiti
e dagli esiti istruttori disponibili: non inventa date, giudizi di idoneita'
o dati dei candidati. Non invia nulla a GroqCloud e non salva PDF sul server.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Frame, KeepTogether, PageTemplate, Paragraph,
    Spacer, Table, TableStyle
)

NON_APPLICABILE="NON APPLICABILE"
ENTE_COERENTE="ENTE COERENTE"
DA_APPROFONDIRE="DA APPROFONDIRE"
ORDER={DA_APPROFONDIRE:0,ENTE_COERENTE:1,NON_APPLICABILE:2}

NAVY=colors.HexColor("#15334d")
INK=colors.HexColor("#1d2c39")
MUTED=colors.HexColor("#4e5f6c")
LINE=colors.HexColor("#dbe3ea")
PALE=colors.HexColor("#f3f7fa")
GOLD=colors.HexColor("#8b5806")
GREEN=colors.HexColor("#16704d")
NEUTRAL=colors.HexColor("#657383")
PAGE_W,PAGE_H=landscape(A4)
MARGIN=38
CONTENT_W=PAGE_W-2*MARGIN

NORMAL=ParagraphStyle("RU-normal",fontName="Helvetica",
    fontSize=9,leading=13,spaceAfter=5,textColor=INK,
    splitLongWords=True)
SMALL=ParagraphStyle("RU-small",parent=NORMAL,fontSize=8,
    leading=11,spaceAfter=4,textColor=MUTED)
TINY=ParagraphStyle("RU-table",parent=NORMAL,fontSize=7.8,
    leading=10,spaceAfter=0)
TABLE_HEAD=ParagraphStyle("RU-table-head",parent=TINY,
    fontName="Helvetica-Bold",textColor=colors.white)
TITLE=ParagraphStyle("RU-title",parent=NORMAL,fontSize=16,leading=21,
    fontName="Helvetica-Bold",spaceAfter=9,textColor=NAVY)
SECTION=ParagraphStyle("RU-section",parent=NORMAL,fontSize=11,
    leading=15,fontName="Helvetica-Bold",spaceBefore=13,spaceAfter=7,
    textColor=NAVY,keepWithNext=True)
GROUP=ParagraphStyle("RU-group",parent=NORMAL,fontSize=10,
    leading=14,fontName="Helvetica-Bold",spaceBefore=13,spaceAfter=5,
    textColor=NAVY,keepWithNext=True)
DETAIL_HEAD=ParagraphStyle("RU-detail-head",parent=NORMAL,fontSize=9,
    leading=13,fontName="Helvetica-Bold",spaceAfter=6,textColor=NAVY)
NOTE=ParagraphStyle("RU-note",parent=NORMAL,fontSize=8.5,
    leading=12,spaceAfter=5)
FOOTER_NOTICE=("Documento di supporto istruttorio: la coerenza "
    "istituzionale non prova la posizione lavorativa del candidato "
    "e non determina ammissione, esclusione o idoneita'.")


def _safe(value,missing="[DA VERIFICARE]"):
    text=" ".join(str(value or "").split())
    if not text:
        text=missing
    # Helvetica PDF standard non supporta tutti i segni Unicode.
    return (text.replace("\u2011","-").replace("\u2013","-")
      .replace("\u2014","-").replace("\u2022","-")
      .replace("\u2019","'").replace("\u2018","'").replace("\u00a0"," "))


def _p(value,style=NORMAL,missing="[DA VERIFICARE]"):
    return Paragraph(escape(_safe(value,missing)),style)


def _candidate(item):
    return " ".join(str(item.get(k) or "").strip() for k in
                    ("cognome","nome")).strip() or "[DA VERIFICARE]"


def _group(item):
    return (str(item.get("cds") or "").strip() or "[DA VERIFICARE]",
            str(item.get("sede") or "").strip() or "[DA VERIFICARE]")


def _page_decoration(canvas,doc,code):
    canvas.saveState()
    canvas.setFont("Helvetica-Bold",8)
    canvas.setFillColor(NAVY)
    canvas.drawString(MARGIN,PAGE_H-23,"RANKUP  |  VERIFICA REQUISITO ENTE")
    canvas.setFont("Helvetica",8)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(PAGE_W-MARGIN,PAGE_H-23,
                           "Bando: "+_safe(code))
    canvas.setStrokeColor(LINE)
    canvas.line(MARGIN,PAGE_H-30,PAGE_W-MARGIN,PAGE_H-30)
    canvas.line(MARGIN,35,PAGE_W-MARGIN,35)
    canvas.setFont("Helvetica",7)
    canvas.drawString(MARGIN,23,"Documento istruttorio - uso amministrativo")
    canvas.drawRightString(PAGE_W-MARGIN,23,
                           "Pagina "+str(doc.page))
    canvas.restoreState()


def _status_cell(value):
    status=str(value or "")
    col=GREEN if status==ENTE_COERENTE else GOLD if status==DA_APPROFONDIRE else NEUTRAL
    st=ParagraphStyle("RU-status-"+status,parent=TINY,fontName="Helvetica-Bold",
                      textColor=col)
    return _p(status,st)


def _overview(totals,shown,filter_text):
    order=(DA_APPROFONDIRE,ENTE_COERENTE,NON_APPLICABILE)
    all_count=sum(int(totals.get(k,0)) for k in order)
    metrics=[
        ("Totale ambito",str(all_count)),
        ("Nel PDF",str(shown)),
        ("Da approfondire",str(totals.get(DA_APPROFONDIRE,0))),
        ("Ente coerente",str(totals.get(ENTE_COERENTE,0))),
        ("Non applicabile",str(totals.get(NON_APPLICABILE,0))),
    ]
    numstyle=ParagraphStyle("RU-metric-num",parent=TITLE,fontSize=16,leading=19,
                           spaceAfter=2)
    lblstyle=ParagraphStyle("RU-metric-label",parent=SMALL,fontSize=7.5)
    values=[[Paragraph(escape(x[1]),numstyle),_p(x[0],lblstyle)] for x in metrics]
    data=[[val[0] for val in values],[val[1] for val in values]]
    tab=Table(data,colWidths=[CONTENT_W/5]*5,hAlign="LEFT")
    tab.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),PALE),
        ("BOX",(0,0),(-1,-1),0.7,LINE),
        ("INNERGRID",(0,0),(-1,-1),0.5,LINE),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),10),
        ("TOPPADDING",(0,0),(-1,0),10),
        ("BOTTOMPADDING",(0,-1),(-1,-1),9),
    ]))
    return tab


def _table(rows):
    header=["ID PICA","Candidato","Insegnamento",
            "Requisito / ente","Rapporto dichiarato","Esito"]
    widths=[58,120,214,140,122,CONTENT_W-58-120-214-140-122]
    data=[[ _p(v,TABLE_HEAD) for v in header]]
    for item in rows:
        data.append([
          _p(item.get("id_pica"),TINY),
          _p(_candidate(item),TINY),
          _p(item.get("insegnamento"),TINY),
          _p(str(item.get("requisito") or "[DA VERIFICARE]")+
             " / "+str(item.get("categoria_ente") or "[DA VERIFICARE]"),TINY),
          _p(item.get("categoria_rapporto"),TINY),
          _status_cell(item.get("esito")),
        ])
    table=Table(data,colWidths=widths,repeatRows=1,hAlign="LEFT",
                splitByRow=1)
    table.setStyle(TableStyle([
       ("BACKGROUND",(0,0),(-1,0),NAVY),
       ("ROWBACKGROUNDS",(0,1),(-1,-1),
                            [colors.white,PALE]),
       ("VALIGN",(0,0),(-1,-1),"TOP"),
       ("GRID",(0,0),(-1,-1),0.4,LINE),
       ("LEFTPADDING",(0,0),(-1,-1),7),
       ("RIGHTPADDING",(0,0),(-1,-1),7),
       ("TOPPADDING",(0,0),(-1,-1),7),
       ("BOTTOMPADDING",(0,0),(-1,-1),7),
    ]))
    return table


def _different(a,b):
    """Una spiegazione AI non va ripetuta se coincide col testo gia' stampato."""
    x=" ".join(_safe(a,"").lower().split()).strip(" .;")
    y=" ".join(_safe(b,"").lower().split()).strip(" .;")
    if not x or not y:
        return False
    return x not in y and y not in x


def _detail(item):
    identifier=_safe(item.get("id_pica"))
    candidate=_candidate(item)
    course,seat=_group(item)
    title=identifier+"  |  "+candidate+"  |  "+course+" - "+seat
    result=[_p(title,DETAIL_HEAD),
            _p("Insegnamento: "+_safe(item.get("insegnamento")),SMALL)]
    kind=str(item.get("tipo_approfondimento") or "").strip()
    if kind:
        result.append(_p("Tipo di approfondimento: "+kind,SMALL))
    # Queste voci sono presentate una sola volta per posizione, senza
    # sovrapporre il suggerimento AI alla verifica amministrativa.
    reason=str(item.get("motivo") or "").strip()
    verification=str(item.get("verifiche") or "").strip()
    if reason:
        result.append(_p("Motivo del rilievo: "+reason,NOTE))
    if verification and _different(verification,reason):
        result.append(_p("Controllo suggerito: "+verification,NOTE))
    # Il modello fornisce indicazioni orientative, mai fatti accertati.
    ai=item.get("ai") or {}
    if ai:
        hint=str(ai.get("verifiche") or "").strip()
        if (hint and _different(hint,verification)
            and _different(hint,reason)):
            result.append(_p("Ulteriore indicazione AI: "+hint,SMALL))
    result.append(Spacer(1,9))
    return KeepTogether(result)


def render_ente_report_pdf(code:str,cds:str,sede:str,rows:list[dict],
                          totals:dict|None=None,filter_text:str="") -> bytes:
    """PDF locale riservato all'Admin. Nessuna sorgente esterna."""
    rows=list(rows)
    totals=dict(totals or Counter(str(r.get("esito") or "") for r in rows))
    rows.sort(key=lambda r:(
        _group(r)[0].casefold(),_group(r)[1].casefold(),
        ORDER.get(r.get("esito"),99),
        _candidate(r).casefold(),str(r.get("id_pica") or "")))
    bio=BytesIO()
    doc=BaseDocTemplate(bio,pagesize=(PAGE_W,PAGE_H),
        leftMargin=MARGIN,rightMargin=MARGIN,topMargin=46,bottomMargin=48,
        title="RankUP - Verifica requisito ente - Bando "+_safe(code),
        author="RankUP")
    frame=Frame(MARGIN,48,CONTENT_W,PAGE_H-46-48,
                leftPadding=0,rightPadding=0,
                topPadding=0,bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id="report",frames=[frame],
         onPage=lambda c,d:_page_decoration(c,d,code)))
    story=[
        _p("Verifica preliminare del requisito di ente",TITLE),
        _p("Bando: "+_safe(code)+"   |   CdS: "+_safe(cds)+
           "   |   Sede: "+_safe(sede),NORMAL),
        _p("Filtro applicato: "+(filter_text or "Tutti gli esiti"),SMALL),
        _p("Il prospetto confronta le denominazioni istituzionali delle "
           "dichiarazioni PICA. Gli esiti sono indicativi: il rapporto "
           "di lavoro personale deve essere accertato separatamente.",SMALL),
        Spacer(1,8),
        _overview(totals,len(rows),filter_text),
        Spacer(1,10),
        _p("1. Elenco candidature per CdS e sede",SECTION),
    ]
    grouped=defaultdict(list)
    for row in rows:
        grouped[_group(row)].append(row)
    if not rows:
        story.append(_p("Nessuna candidatura presente nel filtro selezionato."))
    for (course,seat),group_rows in sorted(grouped.items(),
                                          key=lambda x:(x[0][0].casefold(),x[0][1].casefold())):
        story.append(_p(course+"  |  "+seat+"  ("+str(len(group_rows))+" candidature)",GROUP))
        story.append(_table(group_rows))
    flagged=[r for r in rows if r.get("esito")==DA_APPROFONDIRE]
    if flagged:
        story.extend([Spacer(1,13),
          _p("2. Posizioni da approfondire - indicazioni istruttorie",SECTION),
          _p("Per ciascuna posizione sono riportati il motivo e il controllo "
             "suggerito una sola volta. Verificare sempre le dichiarazioni "
             "originali e le fonti ufficiali.",SMALL)])
        by_issue=defaultdict(list)
        for item in flagged:
            by_issue[str(item.get("tipo_approfondimento") or "NON DEFINITO")].append(item)
        priority=("ENTE NON DICHIARATO","FILTRO PRIVACY",
                  "DATI INSUFFICIENTI","POSSIBILE DISALLINEAMENTO",
                  "ERRORE TECNICO AI","IN ATTESA AI",
                  "REQUISITO NON CLASSIFICATO")
        for kind in sorted(by_issue, key=lambda v:(priority.index(v)
                           if v in priority else len(priority),v)):
            entries=by_issue[kind]
            story.append(_p(kind+"  ("+str(len(entries))+" candidature)",GROUP))
            for item in entries:
                story.append(_detail(item))
    story.extend([Spacer(1,14),
        _p("Avvertenza",SECTION),
        _p(FOOTER_NOTICE,SMALL)])
    doc.build(story)
    return bio.getvalue()
