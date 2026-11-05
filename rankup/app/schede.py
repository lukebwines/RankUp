"""Generazione di schede PDF AcroForm: facsimili da validare sul modello ufficiale.

Le strutture dei campi sono derivate dagli esempi forniti, ma il layout
è ricostruito da zero: nessuna anagrafica o firma di un PDF compilato
viene riutilizzata. I modelli definitivi andranno verificati prima
dell'impiego amministrativo.
"""
from __future__ import annotations

from io import BytesIO
import re
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.pagesizes import A4
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, DictionaryObject, TextStringObject, ArrayObject

ORDINARI = [
 ("P01", "Altra laurea (titolo aggiuntivo)", 2),
 ("P02", "Altro diploma di specializzazione", 2),
 ("P03", "Dottorato di ricerca", 3),
 ("P04", "Master", 2),
 ("P05", "Corsi di perfezionamento pertinenti", 1),
 ("P06", "Assegni di ricerca", 1),
 ("P07", "Esperienze professionali pertinenti all'estero", 2),
 ("P08", "Attivita di aggiornamento pertinente", 2),
 ("P09", "Attivita professionale", 10),
 ("P10", "Docenza accademica pertinente", 10),
 ("P11", "Insegnamenti non universitari pertinenti", 2),
 ("P12", "Relatore a corsi, convegni, congressi", 3),
 ("P13", "Attivita scientifica pertinente", 10),
 ("P14", "Valutazione didattica - indicatore Q12", 3),
 ("P15", "Esperienze e competenze specifiche", 7),
]
PROFESSIONALIZZANTI = [
 ("P01", "Master pertinente all'insegnamento", 3),
 ("P02", "Corsi di perfezionamento / specializzazione", 1),
 ("P03", "Assegni di ricerca / dottorato", 2),
 ("P04", "Esperienze professionali pertinenti all'estero", 2),
 ("P05", "Corsi di formazione alla didattica", 3),
 ("P06", "Attivita di aggiornamento pertinente", 4),
 ("P14", "Docenza accademica pertinente", 10),
 ("P15", "Insegnamenti non universitari pertinenti", 2),
 ("P16", "Relatore a corsi, convegni, congressi", 3),
 ("P17", "Pubblicazioni pertinenti", 7),
 ("P18", "Valutazione didattica - indicatore Q12", 3),
 ("P19", "Esperienze e competenze specifiche", 10),
]
STATUSES = [
 ("-", "-- Seleziona --"),
 ("CANDIDATO AMMESSO IN GRADUATORIA", "AMMESSO IN GRADUATORIA"),
 ("CANDIDATO UNICO IDONEO", "CANDIDATO UNICO IDONEO"),
 ("CANDIDATO CON CURRICULUM NON VALUTABILE O NON IDONEO", "CURRICULUM NON VALUTABILE"),
 ("CANDIDATO CON REQUISITO ENTE DI APPARTENENZA MANCANTE", "REQUISITO ENTE MANCANTE"),
 ("CANDIDATO CON TITOLO DI STUDIO RICHIESTO PER L'ACCESSO MANCANTE", "TITOLO DI STUDIO MANCANTE"),
 ("CANDIDATO CON TITOLO DI STUDIO RICHIESTO PER L'ACCESSO NON PERTINENTE", "TITOLO NON PERTINENTE"),
 ("CANDIDATO NON IDONEO", "CANDIDATO NON IDONEO"),
]


def short(value: object, limit=80) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:limit] if len(text)>limit else text


def _add_total_calculation(data: bytes, score_names: list[str]) -> bytes:
    reader = PdfReader(BytesIO(data))
    writer = PdfWriter()
    writer.append(reader)
    acro = writer._root_object.get("/AcroForm")
    if not acro:
        raise ValueError("Modulo PDF compilabile non generato correttamente")
    form = acro.get_object()
    refs = list(form["/Fields"])
    fields = {str(x.get_object().get("/T", "")):x for x in refs}
    expr = "var s=0; "
    for key in score_names:
        expr += f'var f=this.getField("{key}");if(f){{var n=parseFloat(f.value);if(!isNaN(n))s+=n;}}'
    expr += 'event.value = Math.round(s*100)/100;'
    total_ref = fields.get("TOT")
    if total_ref:
        total_ref.get_object()[NameObject("/AA")] = DictionaryObject({
            NameObject("/C"): DictionaryObject({
                NameObject("/S"):NameObject("/JavaScript"),
                NameObject("/JS"):TextStringObject(expr)
            })
        })
        form[NameObject("/CO")] = ArrayObject([total_ref])
    # L'ordine di calcolo viene indicato anche all'apertura;
    # nei lettori che ignorano JavaScript il totale va verificato in RankUp.
    writer.add_metadata({"/Producer": "RankUp - scheda compilabile (facsimile)"})
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def make_scheda(record: dict, tipo: str) -> bytes:
    if tipo not in ("professionalizzante", "ordinario"):
        raise ValueError("Tipologia SSD non classificata: scheda non generabile")
    professional = tipo == "professionalizzante"
    rows = PROFESSIONALIZZANTI if professional else ORDINARI
    primary = HexColor("#d0009a" if professional else "#00bc1f")
    bg = HexColor("#edf3fa")
    dark = HexColor("#24364b")
    page_w, page_h = A4
    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=A4, pageCompression=1)
    pdf.setTitle("Scheda di valutazione - " + short(record.get("id_pica")))
    form = pdf.acroForm

    def rect(x, y, w, h, color):
        pdf.setFillColor(color)
        pdf.rect(x, y, w, h, stroke=0, fill=1)

    def txt(x, y, label, size=9, bold=False, color=dark):
        pdf.setFillColor(color)
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        pdf.drawString(x, y, short(label, 120))

    def field(label, value, x, y, w, name):
        txt(x, y+20, label, 7, True)
        rect(x,y,w,17,bg)
        form.textfield(name=name, value=short(value,190), tooltip=label,
            x=x+3,y=y+2,width=w-6,height=12,fontSize=8,
            fillColor=bg, textColor=dark,borderWidth=0,
            fieldFlags="readOnly")

    rect(36,page_h-92,page_w-72,46,primary)
    txt(48,page_h-64,"SCHEDA DI VALUTAZIONE TITOLI CANDIDATI",12,True,white)
    txt(48,page_h-82,
        ("SSD PROFESSIONALIZZANTI" if professional else "SSD NON PROFESSIONALIZZANTI")
        + "  -  BANDO " + short(record.get("bando"),30),10,True,white)
    txt(39,page_h-106,"FACSIMILE DI PROVA - CRITERI E MODALITA DA VALIDARE",8,True,HexColor("#aa2547"))
    yy=page_h-143
    fields=[
       ("ID domanda",record.get("id_pica"),"ID-DOMANDA"),
       ("Cognome",record.get("cognome"),"COGNOME"),
       ("Nome",record.get("nome"),"NOME"),
       ("Codice fiscale",record.get("codice_fiscale"),"CF")
    ]
    for idx,(label,value,name) in enumerate(fields):
        x=36+idx*131
        field(label,value,x,yy,126,name)
    yy-=43
    field("Corso di Studio",record.get("cds"),36,yy,253,"CDS")
    field("Sede",record.get("sede"),296,yy,263,"SEDE")
    yy-=43
    field("Insegnamento",record.get("insegnamento"),36,yy,253,"INSEGNAMENTO")
    field("Modulo",record.get("modulo"),296,yy,263,"MODULO")
    yy-=43
    field("SSD",record.get("ssd"),36,yy,120,"SSD")
    field("Partizione / UL",record.get("ul"),163,yy,120,"UL")
    field("Codice univoco scheda",record.get("scheda_id"),290,yy,269,"RANKUP-ID")
    yy-=35
    field("Presidente della commissione",record.get("presidente"),36,yy,253,"PRESIDENTE")
    field("Componenti della commissione",record.get("componenti"),296,yy,263,"COMPONENTI")
    yy-=24
    txt(38,yy,"Punteggi della commissione (selezionare esplicitamente il valore):",9,True)
    yy-=18

    score_names=[]
    def score_row(name,label,allowed):
        nonlocal yy
        if yy < 65:
            pdf.showPage()
            yy = page_h - 75
            txt(36, yy+22, "VALUTAZIONE - CONTINUAZIONE", 12, True)
        rect(36, yy-5, 523, 25, bg)
        txt(43, yy+4, label,8,False)
        vals=[(str(x),str(x)) for x in allowed]
        form.choice(name=name, tooltip=label, value="0",
            x=482,y=yy-1,width=70,height=18,options=vals,fontSize=8,
            borderWidth=.5,fillColor=white,textColor=black,
            borderColor=HexColor("#b8c9dc"),fieldFlags="combo")
        score_names.append(name)
        yy-=27

    for key,label,maxval in rows:
        score_row(key,label,range(maxval+1))
        if professional and key=="P06":
            for index in range(5):
                # Il riferimento cronologico dei cinque anni dovra essere configurato.
                score_row("attivita"+str(index),
                          "Attivita professionale - periodo "+str(index+1),("0","0.5","1","1.5"))
                score_row("plus"+str(index),
                          "Tutoraggio o supervisione - periodo "+str(index+1),("0","0.5"))

    # Se la tabella e' lunga, crea una seconda pagina per note ed esito.
    def notes():
        nonlocal yy
        if yy < 145:
            pdf.showPage()
            yy = page_h-80
            txt(36, yy+25, "ESITO DELLA COMMISSIONE",12,True)
        txt(36,yy+10,"Esito (quando richiesto):",8,True)
        form.choice(name="STATUS", tooltip="Esito della commissione",
            x=145,y=yy,width=414,height=20,value="-",
            options=STATUSES,fieldFlags="combo",
            fontSize=8,fillColor=white,textColor=black,borderWidth=.5)
        yy-=31
        txt(36,yy+9,"Totale calcolato (lettori PDF compatibili):",8,True)
        form.textfield(name="TOT",value="0",tooltip="Totale automatico",
            x=325,y=yy,width=70,height=19,fontSize=9,fillColor=bg,
            textColor=dark,borderWidth=.5,fieldFlags="readOnly")
        yy-=42
        txt(36,yy+16,"Note della commissione:",8,True)
        form.textfield(name="NOTE-COMM", value="",tooltip="Note commissione",
            x=36,y=yy-45,width=523,height=56,fontSize=9,
            fillColor=bg,textColor=black,borderWidth=.5,
            fieldFlags="multiline")
        yy-=70
        txt(38,yy,"Documento generato da RankUp - firma digitale esterna al gestionale",8)
    notes()
    pdf.save()
    return _add_total_calculation(stream.getvalue(),score_names)
