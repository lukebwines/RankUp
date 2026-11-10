"""Scheda ordinaria A4 ricostruita dal modello editabile fornito.

Non incorpora dati personali, firme digitali o protocolli del PDF di esempio.
Le sei aree mantengono massimali 15/10/15/10/3/7 e 15 campi di punteggio.
"""
from __future__ import annotations

from io import BytesIO
from reportlab.pdfgen import canvas
from occupazione_pdf import draw_occupazione_warning
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white, black
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, DictionaryObject, TextStringObject, ArrayObject
from professional import STATUSES
from score_rules import indicator_limit, area_limit, total_limit, fixed_value

W, H = A4
GREEN = HexColor("#00cf0c")
BLUE = HexColor("#c4d5e9")
PALE = HexColor("#dce7f2")
GRAY = HexColor("#dddddd")
INK = HexColor("#151c26")

TITLES = [
    ("P01", "Altra Laurea (titolo aggiuntivo)", indicator_limit("ordinario", "P01")),
    ("P02", "Altro Diploma di Specializzazione (oltre al requisito)", indicator_limit("ordinario", "P02")),
    ("P03", "Dottorato di Ricerca", indicator_limit("ordinario", "P03")),
    ("P04", "Master", indicator_limit("ordinario", "P04")),
    ("P05", "Corsi di perfezionamento pertinenti", indicator_limit("ordinario", "P05")),
    ("P06", "Assegni di ricerca", indicator_limit("ordinario", "P06")),
    ("P07", "Esperienze professionali pertinenti effettuate all'estero", indicator_limit("ordinario", "P07")),
    ("P08", "Attività di aggiornamento pertinente all'insegnamento", indicator_limit("ordinario", "P08")),
]
DIDATTICA = [
    ("P10", "Docenza in ambito accademico pertinente all'insegnamento", indicator_limit("ordinario", "P10")),
    ("P11", "Insegnamenti non universitari con incarico formale e affini alla disciplina", indicator_limit("ordinario", "P11")),
    ("P12", "Relatore a corsi, convegni, congressi", indicator_limit("ordinario", "P12")),
]
SCIENTIFIC = [
    "Pubblicazione di libro/monografia come unico autore",
    "Pubblicazione di libro/monografia come coautore",
    "Pubblicazione di uno o più capitoli di libro",
    "Pubblicazione di articolo su rivista censita I.F.",
    "Pubblicazione di articolo su rivista non censita I.F.",
]


def _add_total_calculation(content: bytes) -> bytes:
    reader = PdfReader(BytesIO(content))
    writer = PdfWriter()
    writer.append(reader)
    acroform = writer._root_object.get("/AcroForm")
    if not acroform:
        raise ValueError("Campi PDF AcroForm non presenti")
    form = acroform.get_object()
    refs = {str(ref.get_object().get("/T", "")): ref for ref in form["/Fields"]}
    names = [f"P{i:02d}" for i in range(1, 16)]
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
        "/Producer": "RankUp - scheda ordinaria compilabile",
        "/Title": "Scheda di valutazione - SSD non professionalizzanti",
    })
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def make_ordinary(record: dict) -> bytes:
    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=A4, pageCompression=1)
    pdf.setTitle("Scheda ordinaria valutazione - "+str(record.get("id_pica") or ""))
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
        vals = (
            [("0", "0"), (str(maximum), str(maximum))]
            if fixed_value("ordinario", name) else
            [("0", "0"), (str(maximum), str(maximum))] if name in
                {"P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08"} else
            [(str(n), str(n)) for n in range(maximum+1)]
        )
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
             "PER SSD A BANDO (ESCLUSI SSD PROFESSIONALIZZANTI) - Bando "+
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

    headings = [
        (50, 83, ("Titoli e Attività", "da esaminare")),
        (134, 265, ("Indicatori relativi al curriculum",)),
        (401, 44, ("Punti per", "titolo", "attività")),
        (447, 52, ("Punti", "assegnati")),
        (501, 49, ("Punteggio", "per area")),
    ]
    for x,width,values in headings:
        rect(x, 231, width, 34, GREEN)
        for j,value in enumerate(values):
            centered(x, 235+j*9, width, value,
                     6.1 if x==401 else 7.3, True, white)

    # 1. Titoli e aggiornamento: 15 punti, 8 menu come nel modello.
    rect(50, 267, 83, 120, BLUE)
    lines(53, 292, [
        "Ulteriori Titoli di", "studio/Attività di",
        "aggiornamento", "pertinenti", "all'insegnamento",
        "(ultimi 5 anni)",
    ], 7.1, 9)
    for j,(key,label,maximum) in enumerate(TITLES):
        top=267+j*15
        row_bgs(top,14.5)
        text(137,top+3,label,6.95)
        centered(401,top+3,44,str(maximum),7.3)
        choice(key,450,top+1,45,12.5,maximum)
    area(267,120,str(area_limit("ordinario", 1)))

    # 2. Attività professionale: un solo indicatore, 10 punti.
    rect(50,389,83,32,BLUE)
    lines(53,394,["Attività professionale","(ultimi 5 anni)"],7.15,9)
    row_bgs(389,32)
    text(137,399,"Attività professionale",7.2)
    centered(401,399,44,"max "+str(indicator_limit("ordinario", "P09")),7)
    choice("P09",450,397,45,17,indicator_limit("ordinario", "P09"))
    area(389,32,str(area_limit("ordinario", 2)))

    # 3. Didattica: 15 punti complessivi.
    rect(50,423,83,64,BLUE)
    lines(53,442,["Attività didattica","(ultimi 5 anni)"],7.1,9)
    top=423
    heights=(27,18,19)
    for (key,label,maximum),height in zip(DIDATTICA,heights):
        row_bgs(top,height-1)
        if key=="P10":
            lines(137,top+3,[
                "Docenza in ambito accademico pertinente all'insegnamento",
                "(la stessa docenza va conteggiata una sola volta",
                "anche se svolta in più corsi di studio)",
            ],6.65,7.6)
            lines(402,top+3,["max 2 per","docenza/anno"],6.0,8.5)
        else:
            text(137,top+4,label,6.7)
            centered(401,top+4,44,"max "+str(maximum),6.8)
        choice(key,450,top+2,45,height-4,maximum)
        top+=height
    area(423,64,str(area_limit("ordinario", 3)))

    # 4. Attività scientifica: cinque tipologie, un UNICO punteggio P13.
    rect(50,489,83,80,BLUE)
    lines(53,502,[
        "Attività scientifica", "pertinente", "all'insegnamento",
        "(ultimi 5 anni)",
    ],6.95,8.9)
    for j,label in enumerate(SCIENTIFIC):
        top=489+j*16
        rect(134,top,265,15.2,BLUE)
        text(137,top+3,label,6.85)
    rect(401,489,44,80,BLUE)
    lines(403,504,[
        "Assegnare il", "punteggio in", "base alla",
        "rilevanza", "scientifica",
    ],6.2,8)
    rect(447,489,52,80,BLUE)
    choice("P13",450,518,45,20,indicator_limit("ordinario", "P13"))
    area(489,80,str(area_limit("ordinario", 4)))

    # 5. Questionario studenti.
    rect(50,571,83,28,BLUE)
    lines(53,574,["Valutazione didattica","Questionario studenti"],6.9,8.5)
    row_bgs(571,28)
    text(137,579,"Indicatore Q12 relativo alla soddisfazione complessiva >3",6.75)
    centered(401,578,44,str(indicator_limit("ordinario", "P14")),7.2)
    choice("P14",450,576,45,18,indicator_limit("ordinario", "P14"))
    area(571,28,str(area_limit("ordinario", 5)))

    # 6. Esperienze e competenze specifiche.
    rect(50,601,83,32,BLUE)
    lines(53,606,["Esperienze e","competenze specifiche"],7.0,9)
    row_bgs(601,32)
    lines(137,606,[
        "Valorizzazione dell'esperienza e della competenza nel settore",
        "specifico richiesto dall'insegnamento del corso di laurea",
    ],6.8,8.8)
    centered(401,609,44,"max "+str(indicator_limit("ordinario", "P15")),7)
    choice("P15",450,607,45,19,indicator_limit("ordinario", "P15"))
    area(601,32,str(area_limit("ordinario", 6)))

    lines(53,637,[
        "Tutte le variabili devono essere pesate in rapporto alla pertinenza",
        "del curriculum all'insegnamento/modulo indicato. Se i requisiti di",
        "accesso alla docenza non sono presenti non si procede alla",
        'valutazione e si assegna punteggio pari a "zero".',
    ],5.6,5.6)
    for x,width in ((401,44),(447,52),(501,49)):
        rect(x,636,width,19,GREEN)
    centered(401,639,44,"TOTALE",8.0,True,white)
    field("TOT",449,638,48,15,"0",readonly=True,fontsize=9,background=white)
    centered(501,638,49,str(total_limit("ordinario")),10,True,white)

    text(53,659,"Note commissione / Esito:",7.4,True)
    form.choice(name="STATUS",tooltip="Esito della commissione",
                x=210,y=H-675,width=340,height=17,
                value=STATUSES[0][0],options=STATUSES,
                fieldFlags="combo",fontSize=7.1,
                fillColor=white,textColor=black,
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
    field("RANKUP-ID",53,818,497,11,
          record.get("scheda_id") or "[DA VERIFICARE]",fontsize=6,background=white)
    pdf.save()
    return _add_total_calculation(out.getvalue())
