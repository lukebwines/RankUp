"""Scheda Inglese: 13 indicatori, massimali 13/20/10/7, totale 50."""
from __future__ import annotations

from io import BytesIO
from reportlab.pdfgen import canvas
from occupazione_pdf import draw_occupazione_warning
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white, black
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, DictionaryObject, TextStringObject, ArrayObject
from professional import STATUSES

W, H = A4
GREEN = HexColor("#4f86c2")
BLUE = HexColor("#c4d5e9")
PALE = HexColor("#dce7f2")
GRAY = HexColor("#dddddd")
INK = HexColor("#151c26")

def _add_total_calculation(content: bytes) -> bytes:
    reader = PdfReader(BytesIO(content))
    writer = PdfWriter()
    writer.append(reader)
    acroform = writer._root_object.get("/AcroForm")
    if not acroform:
        raise ValueError("Campi PDF AcroForm non presenti")
    form = acroform.get_object()
    refs = {str(ref.get_object().get("/T", "")): ref for ref in form["/Fields"]}
    names = [f"P{i:02d}" for i in range(1, 14)]
    if any(key not in refs for key in names) or "TOT" not in refs:
        raise ValueError("Campi di valutazione ordinaria incompleti")
    js = "var s=0;"
    for name in names:
        js += (
            f'var f=this.getField("{name}");'
            'if(f){var n=parseFloat(f.value);if(!isNaN(n))s+=n;}'
        )
    js += "event.value=Math.round(s*100)/100;"
    target = refs["TOT"]
    target.get_object()[NameObject("/AA")] = DictionaryObject({
        NameObject("/C"): DictionaryObject({
            NameObject("/S"): NameObject("/JavaScript"),
            NameObject("/JS"): TextStringObject(js),
        })
    })
    form[NameObject("/CO")] = ArrayObject([target])
    writer.add_metadata({
        "/Producer": "RankUp - scheda inglese compilabile",
        "/Title": "Scheda di valutazione - Lingua inglese",
    })
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def make_english(record: dict) -> bytes:
    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=A4, pageCompression=1)
    pdf.setTitle("Scheda Inglese - "+str(record.get("id_pica") or ""))
    form = pdf.acroForm

    def rect(x, top, width, height, color):
        pdf.setFillColor(color)
        pdf.rect(x, H-top-height, width, height, stroke=0, fill=1)

    def text(x, top, value, size=7.5, bold=False, color=INK):
        pdf.setFillColor(color)
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        pdf.drawString(x, H-top-size, str(value or ""))

    def centered(x, top, width, value, size=7.5, bold=False, color=INK):
        pdf.setFillColor(color)
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        pdf.drawCentredString(x+width/2, H-top-size, str(value or ""))

    def lines(x, top, values, size=7.1, leading=8.6, color=INK):
        for j, value in enumerate(values):
            text(x, top+j*leading, value, size, color=color)

    def field(name, x, top, width, height, value="", *,
              readonly=True, fontsize=8, background=GRAY, multiline=False):
        rect(x, top, width, height, background)
        content = str(value if value is not None else "")
        flags = "readOnly" if readonly else ("multiline" if multiline else "")
        form.textfield(name=name, tooltip=name, x=x+2, y=H-top-height+1.5,
                       width=width-4, height=height-3, value=content,
                       fontSize=fontsize, fillColor=background, textColor=INK,
                       borderWidth=0, fieldFlags=flags)

    def choice(name, x, top, width, height, maximum):
        vals = [(str(n),str(n)) for n in range(maximum+1)]
        form.choice(name=name, tooltip=name, x=x, y=H-top-height,
                    width=width, height=height, value="0",
                    options=vals, fieldFlags="combo", fontSize=7.5,
                    fillColor=white, textColor=black,
                    borderColor=HexColor("#a9bacc"), borderWidth=.3)

    def area(top, height, maximum):
        rect(501, top, 49, height, BLUE)
        centered(501, top+height/2-6, 49, maximum, 9.7)

    def row_bgs(top, height):
        for x,width in ((134,265),(401,44),(447,52)):
            rect(x, top, width, height, BLUE)

    # Header and identification fields are not copied from the filled model.
    rect(50, 36, 500, 41, GREEN)
    aa = str(record.get("anno") or "[DA VERIFICARE]")
    centered(50, 42, 500,
             "SCHEDA DI VALUTAZIONE TITOLI CANDIDATI - A.A. "+aa,
             10.3, True, white)
    centered(50, 59, 500,
             "LINGUA INGLESE - Bando "+
             str(record.get("bando") or "[DA VERIFICARE]"),
             9.7, True, white)

    identity = (
        ("ID domanda", "id_pica", "ID-DOMANDA", 53, 67),
        ("Cognome", "cognome", "COGNOME", 121, 144),
        ("Nome", "nome", "NOME", 267, 159),
        ("Codice fiscale", "codice_fiscale", "CF", 428, 122),
    )
    for label,key,name,x,width in identity:
        text(x, 82, label, 7.3, True)
        field(name, x, 90, width, 17, record.get(key), fontsize=8.2)
    text(53, 109, "Corso di studio:", 7.3, True)
    field("CDS", 53, 120, 497, 17, record.get("cds"))
    text(53, 135, "Insegnamento:", 7.3, True)
    text(455, 135, "Sede:", 7.3, True)
    field("INSEGNAMENTO", 53, 146, 398, 17, record.get("insegnamento"))
    field("SEDE", 453, 146, 97, 17, record.get("sede"))
    text(53, 161, "Modulo:", 7.3, True)
    text(456, 161, "Unità Logistica/Partizione:", 6.6, True)
    field("MODULO", 53, 172, 399, 17, record.get("modulo"))
    field("UL", 453, 172, 97, 17, record.get("ul"))
    text(53, 191, "Requisiti richiesti per l'accesso alla docenza:", 7.2, True)
    # Due righe distinte: riferimento del bando e testo originale PICA.
    # Il campo REQUISITI mantiene il nome AcroForm e il proprio contenuto.
    field("REQUISITI", 53, 203, 497, 9.5,
          record.get("requisiti") or "[DA VERIFICARE]", fontsize=6.3)
    text(56, 213.2, "Tipologia di incarico:", 6.6, True)
    field("TIPO-CDS-PICA", 157, 212.5, 393, 9.5,
          record.get("tipo_cds") or "[DA VERIFICARE]", fontsize=6.3)
    draw_occupazione_warning(pdf, record, H)


    headings=[(50,83,("Titoli e Attivita","da esaminare")),(134,265,("Indicatori relativi al curriculum",)),
              (401,44,("Punti per","titolo","attivita")),(447,52,("Punti","assegnati")),
              (501,49,("Punteggio","per area"))]
    for x,width,values in headings:
        rect(x,231,width,34,GREEN)
        for j,value in enumerate(values):
            centered(x,235+j*9,width,value,6.1 if x==401 else 7.1,True,white)

    def group(top,height,title,maximum):
        rect(50,top,83,height,BLUE)
        lines(53,top+8,title,size=6.5,leading=9)
        area(top,height,str(maximum))
    def score(key,top,height,label,maximum,hints=None):
        row_bgs(top,height-1)
        if isinstance(label,list):
            lines(137,top+3,label,size=6.1,leading=7.7)
        else:
            text(137,top+3,label,6.7)
        if hints:
            lines(402,top+3,hints,size=5.9,leading=8)
        else:
            centered(401,top+4,44,"max "+str(maximum),6.6)
        choice(key,450,top+1,45,min(height-2,20),maximum)

    group(267,119,["Ulteriori titoli","di studio / attivita","di aggiornamento","pertinenti","(ultimi 5 anni)"],13)
    titles=[
        ("Altra Laurea",2),("Dottorato di Ricerca",3),("Master",2),
        ("Corsi di perfezionamento",1),("Assegni di ricerca",1),
        ("Corsi di formazione alla didattica",2),
        ("Attivita di aggiornamento pertinente all'insegnamento",2),
    ]
    for i,(label,maximum) in enumerate(titles):
        score(f"P{i+1:02d}",267+i*17,17,label,maximum)

    group(389,112,["Attivita didattica","(ultimi 5 anni)"],20)
    score("P08",389,52,[
        "Docenza accademica pertinente all'insegnamento di INGLESE",
        "SCIENTIFICO in ambito sanitario (anche in piu CdS:",
        "la stessa docenza si conta una sola volta).",
        "Valutazione studenti positiva (Q12 >= 3).",
        "Max 2 punti per docenza/anno, fino a 5 anni.",
    ],10,["max 2 per","docenza/anno","fino a 5 anni"])
    for i,(label,mx) in enumerate([
        ("Altre docenze in ambito accademico",5),
        ("Insegnamenti non universitari formalizzati e affini alla disciplina",2),
        ("Relazioni scientifiche a seminari, master, corsi/convegni",3),
    ]):
        score(f"P{i+9:02d}",441+i*20,20,label,mx)

    group(504,84,["Attivita scientifica","(ultimi 5 anni)","- pertinenza al","modulo a bando"],10)
    scientific=[
        "Pubblicazione di libro/monografia come unico autore",
        "Pubblicazione di libro/monografia come coautore",
        "Pubblicazione di uno o piu capitoli di libro",
        "Pubblicazione di articolo su rivista censita I.F.",
        "Pubblicazione di articolo su rivista non censita I.F.",
    ]
    for i,label in enumerate(scientific):
        rect(134,504+i*16.8,265,16.1,BLUE)
        text(137,507+i*16.8,label,6.3)
    rect(401,504,44,84,BLUE)
    lines(403,520,["Punteggio","in base alla","rilevanza","scientifica"],6.1,9)
    rect(447,504,52,84,BLUE)
    choice("P12",450,533,45,22,10)

    group(591,29,["Esperienze e","competenze","specifiche"],7)
    row_bgs(591,29)
    lines(137,595,[
        "Valorizzazione dell'esperienza e della competenza nel",
        "settore specifico richiesto dall'insegnamento del CdS",
    ],6.5,9)
    centered(401,597,44,"max 7",6.7)
    choice("P13",450,596,45,17,7)

    lines(53,624,[
        "Tutte le variabili vanno pesate in rapporto alla pertinenza del curriculum.",
        "Se mancano i requisiti di accesso alla docenza non si procede alla",
        "valutazione e si assegna punteggio pari a zero.",
    ],5.5,6.7)
    for x,width in ((401,44),(447,52),(501,49)):
        rect(x,636,width,19,GREEN)
    centered(401,639,44,"TOTALE",8,True,white)
    field("TOT",449,638,48,15,"0",readonly=True,fontsize=9,background=white)
    centered(501,639,49,"50",10,True,white)

    text(53,659,"Note commissione / Esito:",7.4,True)
    form.choice(name="STATUS",tooltip="Esito della commissione",
                x=210,y=H-675,width=340,height=17,
                value=STATUSES[0][0],options=STATUSES,
                fieldFlags="combo",fontSize=7.1,fillColor=white,textColor=black,
                borderColor=HexColor("#a9bacc"),borderWidth=.3)
    field("NOTE-COMM",53,682,497,40,"",readonly=False,
          fontsize=8,background=GRAY,multiline=True)
    text(53,730,"La Commissione:",7.5,True)
    rect(53,741,497,15,PALE)
    text(56,743,"Presidente:",7.3,True)
    field("PRESIDENTE",115,742,435,14,record.get("presidente"),fontsize=7.6,background=PALE)
    rect(53,758,497,15,PALE)
    text(56,760,"Componenti:",7.3,True)
    field("COMPONENTI",115,759,435,14,record.get("componenti"),fontsize=7.4,background=PALE)
    rect(50,781,500,1,INK)
    field("RANKUP-MODEL",53,798,497,11,"inglese",fontsize=6,background=white)
    field("RANKUP-ID",53,818,497,11,
          record.get("scheda_id") or "[DA VERIFICARE]",fontsize=6,background=white)
    pdf.save()
    return _add_total_calculation(out.getvalue())
