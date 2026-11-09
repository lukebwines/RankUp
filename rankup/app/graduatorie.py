"""Graduatorie RankUP: solo esiti acquisiti, parita non risolte, istantanee immutabili.

Nessun criterio di spareggio presunto. Le graduatorie definitive per CdS e
gli atti approvati sono registrati con snapshot e non ricalcolati retroattivamente.
"""
from __future__ import annotations
from io import BytesIO
from decimal import Decimal
from xml.sax.saxutils import escape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, KeepTogether
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet


def _score(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def build_graduatoria(db, bando_id: int, cds: str, sede: str) -> dict:
    jobs = [dict(x) for x in db.execute("""
        SELECT id,key,cds,sede,insegnamento,modulo,ssd,id_copertura
        FROM incarichi WHERE bando_id=? AND cds=? AND sede=?
        ORDER BY insegnamento COLLATE NOCASE,modulo COLLATE NOCASE,key
    """,(bando_id,cds,sede))]
    result = {"cds":cds,"sede":sede,"incarichi":[],"pronta":False,
              "criticita":[],"totale_candidature":0}
    if not jobs:
        result["criticita"].append("Nessun incarico per CdS/sede")
        return result
    commission = db.execute("""
        SELECT presidente,componenti FROM commissioni
        WHERE bando_id=? AND cds=? AND sede=?
    """,(bando_id,cds,sede)).fetchone()
    # La risoluzione dei nomi dei CdS nelle commissioni viene fatta dal server.
    result["commissione"] = dict(commission) if commission else None
    for job in jobs:
        candidates=[dict(x) for x in db.execute("""
            SELECT c.id AS candidatura_id,c.id_pica,c.cognome,c.nome,c.stato,
                   v.id AS valutazione_id,v.decisione,v.esito,
                   v.totale_calcolato,v.note
            FROM candidature c
            LEFT JOIN valutazioni_pdf v ON v.candidatura_id=c.id
                AND v.stato='Acquisita' AND v.attiva=1
            WHERE c.incarico_id=? AND c.stato='Attiva'
            ORDER BY c.id_pica,c.id
        """,(job["id"],))]
        result["totale_candidature"]+=len(candidates)
        warnings=[]
        if not candidates:
            warnings.append("Nessuna candidatura attiva: verificare il trattamento dell'incarico")
        for c in candidates:
            if c["valutazione_id"] is None:
                warnings.append("Valutazione mancante: ID PICA "+str(c["id_pica"]))
            elif c["decisione"]=="idoneo" and c["totale_calcolato"] is None:
                warnings.append("Punteggio mancante: ID PICA "+str(c["id_pica"]))
            elif c["decisione"] not in ("idoneo","non_idoneo","escluso"):
                warnings.append("Esito non riconosciuto: ID PICA "+str(c["id_pica"]))
            elif c["decisione"] in ("non_idoneo","escluso") and not str(c["note"] or "").strip():
                warnings.append("Motivazione mancante: ID PICA "+str(c["id_pica"]))
        eligible=[c for c in candidates if c["decisione"]=="idoneo" and c["totale_calcolato"] is not None]
        eligible.sort(key=lambda c:(-_score(c["totale_calcolato"]),str(c["id_pica"])))
        previous=None;position=0
        for i,c in enumerate(eligible):
            score=_score(c["totale_calcolato"])
            if score!=previous:
                position=i+1
            c["posizione"]=position
            c["punteggio"]=str(score)
            previous=score
        ties={}
        for c in eligible:
            ties.setdefault(c["posizione"],[]).append(c)
        selection=db.execute("""SELECT candidatura_id,motivazione,riferimento
            FROM graduatorie_vincitori_proposti
            WHERE bando_id=? AND incarico_id=?
        """,(bando_id,job["id"])).fetchone()
        selected=next((c for c in eligible if selection and c["posizione"]==1
                       and c["candidatura_id"]==selection["candidatura_id"]),None)
        for position, group in ties.items():
            if len(group)>1 and not (position==1 and selected):
                warnings.append("Parita di punteggio non risolta alla posizione "+str(position))
        winner=selected or (eligible[0] if eligible and len(ties.get(1,[]))==1 else None)
        for c in candidates:
            if c["decisione"]=="idoneo":
                matching=next((e for e in eligible if e["candidatura_id"]==c["candidatura_id"]),None)
                c["posizione"]=matching["posizione"] if matching else None
                c["punteggio"]=matching["punteggio"] if matching else None
            else:
                c["posizione"]=None
                c["punteggio"]=None
        jobdata={k:job[k] for k in ("id","key","cds","sede","insegnamento","modulo","ssd","id_copertura")}
        jobdata["vincitore_motivazione"]=(selection["motivazione"] if selection and selected else "")
        jobdata["vincitore_riferimento"]=(selection["riferimento"] if selection and selected else "")
        jobdata.update(candidature=candidates, vincitore_id=winner["candidatura_id"] if winner else None,
                       vincitore_pica=winner["id_pica"] if winner else None,
                       criticita=warnings,pronta=not warnings)
        result["incarichi"].append(jobdata)
        result["criticita"].extend(job["modulo"]+": "+message for message in warnings)
    result["pronta"]=not result["criticita"] and result["totale_candidature"]>0
    return result


def render_graduatoria_pdf(code: str, data: dict, general: bool=False,
                           reference: str="", approved: bool=False) -> bytes:
    out=BytesIO()
    title=("Graduatoria generale definitiva parziale" if general else
           "Graduatoria della Commissione per Corso di Studio")
    doc=SimpleDocTemplate(out,pagesize=A4,leftMargin=16*mm,rightMargin=16*mm,
                          topMargin=16*mm,bottomMargin=16*mm,title=title)
    style=getSampleStyleSheet()
    heading=ParagraphStyle("RUtitle",parent=style["Heading1"],fontSize=13,leading=16)
    sub=ParagraphStyle("RUsub",parent=style["Normal"],fontSize=8,leading=11,spaceAfter=5)
    small=ParagraphStyle("RUcell",parent=style["Normal"],fontSize=7.5,leading=10,wordWrap="CJK")
    def p(t):return Paragraph(escape(str(t if t not in (None,"") else "[DA VERIFICARE]")),small)
    story=[Paragraph(title,heading),Paragraph("Bando: "+escape(code),sub)]
    if general:
        story.append(Paragraph("Stato: "+("ATTI APPROVATI" if approved else "BOZZA DA SOTTOPORRE AD APPROVAZIONE"),sub))
        story.append(Paragraph("Estremi del provvedimento: "+escape(reference or "[DA VERIFICARE]"),sub))
        scopes=data.get("selezioni",[])
    else:
        story.append(Paragraph("CdS: "+escape(data.get("cds") or "[DA VERIFICARE]")+
                               " | Sede: "+escape(data.get("sede") or "[DA VERIFICARE]"),sub))
        story.append(Paragraph("Stato: "+escape(data.get("stato") or "BOZZA"),sub))
        scopes=[data]
    story.append(Paragraph("Documento redatto esclusivamente dalle schede della Commissione acquisite. "
                "Parita e anomalie devono essere risolte prima della definitività. "
                "I dati sono soggetti a verifica amministrativa.",sub))
    for scope in scopes:
        if general:
            story.append(Paragraph("CdS: "+escape(scope["cds"])+" | Sede: "+escape(scope["sede"]),style["Heading2"]))
        for job in scope["incarichi"]:
            story.append(Paragraph("Insegnamento: "+escape(job["insegnamento"])+
                            " | Modulo: "+escape(job["modulo"])+
                            " | SSD: "+escape(job.get("ssd") or "[DA VERIFICARE]"),style["Heading3"]))
            if not general and job.get("vincitore_id"):
                winner=next((x for x in job["candidature"]
                             if x["candidatura_id"]==job["vincitore_id"]),None)
                if winner:
                    story.append(Paragraph("Vincitore proposto: "+
                        escape(str(winner["cognome"])+" "+str(winner["nome"]))+
                        " (ID PICA "+escape(str(winner["id_pica"]))+")",sub))
                    if job.get("vincitore_motivazione"):
                        story.append(Paragraph("Motivazione scelta amministrativa: "+
                            escape(job["vincitore_motivazione"])+
                            " | Riferimento: "+escape(job["vincitore_riferimento"]),sub))
            head=["Pos.","Candidato / ID PICA","Punti","Esito",("Note Commissione" if not general else "Note")]
            rows=[[p(x) for x in head]]
            cand=sorted(job["candidature"],key=lambda x:(
                0 if x["decisione"]=="idoneo" else 1,
                x["posizione"] if x["posizione"] is not None else 99999,
                str(x["id_pica"])))
            for row in cand:
                is_winner=row["candidatura_id"]==job["vincitore_id"]
                identity=((str(row["cognome"])+" "+str(row["nome"])+" (ID PICA "+str(row["id_pica"])+")")
                          if not general or is_winner else "ID PICA "+str(row["id_pica"]))
                rows.append([p(row["posizione"] if row["posizione"] is not None else "-"),
                             p(identity),p(row["punteggio"] if row["punteggio"] is not None else "non attribuito"),
                             p(row["esito"]),p((row["note"] or "—") if not general else "-")])
            if len(rows)==1:
                rows.append([p("-"),p("Nessuna candidatura"),p("-"),p("-"),p("-")])
            table=Table(rows,colWidths=[12*mm,55*mm,21*mm,39*mm,51*mm],repeatRows=1,hAlign="LEFT")
            table.setStyle(TableStyle([
                ("VALIGN",(0,0),(-1,-1),"TOP"),
                ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#e6edf5")),
                ("GRID",(0,0),(-1,-1),0.2,colors.HexColor("#d0d8e4")),
                ("LEFTPADDING",(0,0),(-1,-1),4),("RIGHTPADDING",(0,0),(-1,-1),4),
                ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
            story.extend([table,Spacer(1,8)])
    doc.build(story)
    return out.getvalue()
