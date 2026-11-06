"""Report PDF riservato Admin: controllo istruttorio occupazionale per CdS.

Dati solo da candidature PICA, dizionario dei requisiti e verifiche IA
realmente registrate. Nessun esito amministrativo viene inferito.
"""
from __future__ import annotations

from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether


def report_cds_pdf(code: str, cds: str, entries: list[dict], created: str) -> bytes:
    result = BytesIO()
    doc = SimpleDocTemplate(
        result, pagesize=A4, rightMargin=17*mm, leftMargin=17*mm,
        topMargin=17*mm, bottomMargin=17*mm,
        title="RankUP - Controllo occupazionale - " + cds)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("ReportTitle", parent=styles["Heading1"], fontSize=13,
                           leading=17, textColor=colors.HexColor("#17283b"))
    subtitle = ParagraphStyle("ReportSubtitle", parent=styles["Normal"], fontSize=8.4,
                              leading=12, spaceAfter=7)
    item = ParagraphStyle("ReportBody", parent=styles["Normal"], fontSize=8.3,
                          leading=11.5, wordWrap="CJK")
    label = ParagraphStyle("ReportLabel", parent=item, fontName="Helvetica-Bold",
                           textColor=colors.HexColor("#36465c"))
    warning = ParagraphStyle("ReportWarning", parent=item, textColor=colors.HexColor("#9b1522"),
                             fontName="Helvetica-Bold")
    def p(value, style=item):
        return Paragraph(escape(str(value if value not in (None,"") else "[DA VERIFICARE]")), style)

    all_count=len(entries)
    checked=sum(bool(x.get("ai")) for x in entries)
    story=[Paragraph("RankUP | Verifica occupazionale - report per CdS",title),
           Paragraph("Bando: "+escape(code)+" | Corso di studio: "+escape(cds),subtitle),
           Paragraph("Generato il: "+escape(created)+" | Candidature: "+
                     str(all_count)+" | Controlli IA registrati: "+str(checked),subtitle),
           Paragraph("Report istruttorio riservato all'amministratore. "
                     "Le dichiarazioni provengono dal PICA; il requisito e' ricavato "
                     "dalla colonna L o da un'eventuale rettifica manuale. "
                     "L'analisi Ollama non attesta il rapporto di lavoro e non "
                     "determina ammissioni, esclusioni o punteggi.",subtitle),
           Spacer(1,8)]
    for index,entry in enumerate(entries,1):
        ai=entry.get("ai")
        req=entry.get("requisito") or "[DA VERIFICARE]"
        different=bool(ai and ai.get("requisito_analizzato")!=req)
        status=("[DA VERIFICARE] - controllo IA non eseguito" if not ai
                else ("DA RIPETERE - requisito modificato dopo il controllo" if different
                      else ai.get("stato") or "[DA VERIFICARE]"))
        data=[
          [p("Candidatura "+str(index)+" - ID PICA "+str(entry.get("id_pica") or ""),label),
           p(str(entry.get("cognome") or "")+" "+str(entry.get("nome") or ""),label)],
          [p("CdS / sede",label),p(str(entry.get("cds") or "")+" / "+str(entry.get("sede") or ""))],
          [p("Insegnamento / modulo",label),p(str(entry.get("insegnamento") or "")+" / "+str(entry.get("modulo") or ""))],
          [p("Scheda restituita",label),p(entry.get("scheda_stato") or "Non pervenuta / non associata")],
          [p("Tipo CDS - colonna L PICA",label),p(entry.get("tipo_cds"))],
          [p("Requisito previsto",label),p(req)],
          [p("Occupazione dichiarata",label),p(entry.get("occupazione"))],
          [p("Ente / azienda dichiarata",label),p(entry.get("azienda_ente"))],
          [p("Profilo professionale dichiarato",label),p(entry.get("profilo_professionale"))],
          [p("Controllo Ollama",label),p(status,warning if not ai or different or status!="COERENTE (PRELIMINARE)" else item)],
          [p("Motivazione IA",label),p(ai.get("motivo") if ai else "Controllo non eseguito")],
          [p("Evidenza citata",label),p(ai.get("evidenza") if ai else None)],
          [p("Data e autore controllo",label),
           p(str(ai.get("quando") or "")+" - "+str(ai.get("autore") or "") if ai else None)]
        ]
        table=Table(data,colWidths=[57*mm,119*mm],hAlign="LEFT")
        table.setStyle(TableStyle([
          ("VALIGN",(0,0),(-1,-1),"TOP"),
          ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#e8eff7")),
          ("ROWBACKGROUNDS",(0,1),(-1,-1),
           [colors.white,colors.HexColor("#f8fafc")]),
          ("LINEBELOW",(0,0),(-1,-1),0.25,colors.HexColor("#dae1e8")),
          ("LEFTPADDING",(0,0),(-1,-1),6),
          ("RIGHTPADDING",(0,0),(-1,-1),6),
          ("TOPPADDING",(0,0),(-1,-1),4),
          ("BOTTOMPADDING",(0,0),(-1,-1),4),
        ]))
        story.append(table)
        story.append(Spacer(1,12))
    if not entries:
        story.append(Paragraph("Nessuna candidatura attiva per questo CdS.",item))
    doc.build(story)
    return result.getvalue()
