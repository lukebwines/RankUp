"""Scheda PDF professionalizzante in una pagina: riproduzione strutturale
del modello fornito. Non riusa dati personali o il PDF compilato originale.
Facsimile tecnico: richiede validazione prima di un uso amministrativo.
"""
from __future__ import annotations
from io import BytesIO
import re
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white, black
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, DictionaryObject, TextStringObject, ArrayObject

W, H = A4
MAGENTA = HexColor("#e500a6")
BLUE = HexColor("#c4d5e9")
PALE = HexColor("#dce7f2")
GRAY = HexColor("#dddddd")
INK = HexColor("#151c26")

AREA1 = [
    ("P01", "Master pertinente all’insegnamento", 3),
    ("P02", "Corsi di perfezionamento-specializzazione pertinente", 1),
    ("P03", "Assegni di ricerca/dottorato", 2),
    ("P04", "Esperienze professionali pertinenti effettuate all’estero", 2),
    ("P05", "Corsi di formazione alla didattica", 3),
    ("P06", "Attività di aggiornamento pertinente all’insegnamento", 4),
]
AREA3 = [
    ("P14", "Docenza in ambito accademico pertinente all’insegnamento", 10, "max 2 per docenza/anno"),
    ("P15", "Insegnamenti non universitari con incarico formale e affini alla disciplina", 2, "max 2"),
    ("P16", "Relatore a corsi, convegni, congressi", 3, "max 3"),
]
AREA4 = ("P17", "Pubblicazioni pertinenti all’insegnamento", 7)
AREA5 = ("P18", "Indicatore Q12 relativo alla soddisfazione complessiva >3", 3)
AREA6 = ("P19", "Valorizzazione dell’esperienza e della competenza nel settore specifico richiesto dall’insegnamento del corso di laurea", 10)

PRO_ACTIVITY = [
    ("0", "NESSUNA (0 pt)"),
    ("1.5", "Dirigente Professioni Sanitarie (1.5 pt)"),
    ("1", "Coordinatore reparto/servizio (1 pt)"),
    ("0.5", "Collaboratore professionale (0.5 pt)"),
    ("0.25", "Libero professionista (0.25 pt)"),
    ("1.5", "Coordinatore Corso di Laurea (1.5 pt)"),
]
PRO_PLUS = [
    ("0", "NESSUNA (0 pt)"),
    ("0.5", "Tutor dedicato (0.5 pt)"),
    ("0.5", "Guida/supervis. tirocin. (0.5 pt)"),
]
STATUSES = [
    ("-", "-- Seleziona --"),
    ("CANDIDATO AMMESSO IN GRADUATORIA", "CANDIDATO AMMESSO IN GRADUATORIA"),
    ("CANDIDATO UNICO IDONEO", "CANDIDATO UNICO IDONEO"),
    ("CANDIDATO CON CURRICULUM NON VALUTABILE O NON IDONEO", "CANDIDATO CON CURRICULUM NON VALUTABILE O NON IDONEO"),
    ("CANDIDATO CON REQUISITO ENTE DI APPARTENENZA MANCANTE", "CANDIDATO CON REQUISITO ENTE DI APPARTENENZA MANCANTE"),
    ("CANDIDATO CON TITOLO DI STUDIO RICHIESTO PER L'ACCESSO MANCANTE", "CANDIDATO CON TITOLO DI STUDIO RICHIESTO PER L'ACCESSO MANCANTE"),
    ("CANDIDATO CON TITOLO DI STUDIO RICHIESTO PER L'ACCESSO NON PERTINENTE", "CANDIDATO CON TITOLO DI STUDIO RICHIESTO PER L'ACCESSO NON PERTINENTE"),
    ("CANDIDATO NON IDONEO", "CANDIDATO NON IDONEO"),
]


def _calculations(content: bytes) -> bytes:
    reader = PdfReader(BytesIO(content))
    writer = PdfWriter()
    writer.append(reader)
    acro = writer._root_object.get("/AcroForm")
    if not acro:
        raise ValueError("Campi PDF non generati")
    form = acro.get_object()
    names = {str(ref.get_object().get("/T", "")): ref for ref in form["/Fields"]}
    order = []
    for i in range(5):
        key = f"extraField{i}"
        js = (
            f'var a=parseFloat(this.getField("attivita{i}").value)||0;'
            f'var p=parseFloat(this.getField("plus{i}").value)||0;'
            'event.value=Math.round((a+p)*100)/100;'
        )
        ref = names[key]
        ref.get_object()[NameObject("/AA")] = DictionaryObject({
            NameObject("/C"): DictionaryObject({
                NameObject("/S"): NameObject("/JavaScript"),
                NameObject("/JS"): TextStringObject(js),
            })
        })
        order.append(ref)
    scores = (
        [row[0] for row in AREA1] + [f"extraField{i}" for i in range(5)] +
        [row[0] for row in AREA3] + [AREA4[0], AREA5[0], AREA6[0]]
    )
    js = "var s=0;"
    for key in scores:
        js += (
            f'var n=parseFloat(this.getField("{key}").value);'
            'if(!isNaN(n))s+=n;'
        )
    js += "event.value=Math.round(s*100)/100;"
    ref = names["TOT"]
    ref.get_object()[NameObject("/AA")] = DictionaryObject({
        NameObject("/C"): DictionaryObject({
            NameObject("/S"): NameObject("/JavaScript"),
            NameObject("/JS"): TextStringObject(js),
        })
    })
    order.append(ref)
    form[NameObject("/CO")] = ArrayObject(order)
    writer.add_metadata({
        "/Producer": "RankUp - scheda professionalizzante compilabile",
        "/Title": "Scheda di valutazione - SSD professionalizzanti",
    })
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def make_professional(record: dict) -> bytes:
    """AcroForm A4 con sezioni e limiti del modello professionalizzante fornito."""
    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=A4, pageCompression=1)
    form = pdf.acroForm

    def rect(x, top, width, height, fill):
        pdf.setFillColor(fill)
        pdf.rect(x, H - top - height, width, height, stroke=0, fill=1)

    def txt(x, top, value, size=8, bold=False, color=INK):
        pdf.setFillColor(color)
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        pdf.drawString(x, H - top - size, value)

    def centered(x, top, width, value, size=8, bold=False, color=INK):
        pdf.setFillColor(color)
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        pdf.drawCentredString(x + width / 2, H - top - size, value)

    def multiline(x, top, lines, size=7.2, color=INK, leading=8.6):
        for i, line in enumerate(lines):
            txt(x, top + i * leading, line, size, color=color)

    def field(name, x, top, width, height, value, readonly=True,
              fontsize=8, background=GRAY, multiline=False):
        content = str(value or "")
        rect(x, top, width, height, background)
        flags = "readOnly" if readonly else ("multiline" if multiline else "")
        form.textfield(
            name=name, tooltip=name, x=x+2, y=H-top-height+1.5,
            width=width-4, height=height-3, value=content,
            fontSize=fontsize, textColor=INK, fillColor=background,
            borderWidth=0, fieldFlags=flags,
        )

    def choice(name, x, top, width, height, options):
        form.choice(
            name=name, tooltip=name, x=x, y=H-top-height,
            width=width, height=height, value=options[0][0],
            options=options, fieldFlags="combo", fontSize=7.6,
            fillColor=white, textColor=black,
            borderColor=HexColor("#a9bacc"), borderWidth=.3,
        )

    def numbers(name, x, top, width, height, maximum):
        options = (
            [("0", "0"), ("3", "3")] if name == "P18" else
            [(str(i), str(i)) for i in range(maximum + 1)]
        )
        choice(name, x, top, width, height, options)

    def area(x, top, width, height, maximum):
        rect(x, top, width, height, BLUE)
        centered(x, top + height / 2 - 7, width, str(maximum), 10)

    # No private data from the filled example is embedded in this template.
    rect(50, 36, 500, 41, MAGENTA)
    aa = str(record.get("anno") or "[DA VERIFICARE]")
    centered(50, 42, 500,
             "SCHEDA DI VALUTAZIONE TITOLI CANDIDATI - A.A. " + aa,
             10.4, True, white)
    centered(50, 59, 500,
             "PER I SSD PROFESSIONALIZZANTI - Bando " +
             str(record.get("bando") or "[DA VERIFICARE]"),
             10.4, True, white)

    identity = [
        ("ID Domanda", "id_pica", "ID-DOMANDA", 53, 90, 67),
        ("Cognome", "cognome", "COGNOME", 121, 90, 144),
        ("Nome", "nome", "NOME", 267, 90, 159),
        ("Codice fiscale", "codice_fiscale", "CF", 428, 90, 122),
    ]
    for label, key, name, x, top, width in identity:
        txt(x, top-8, label, 7.3, True)
        field(name, x, top, width, 17, record.get(key), fontsize=8.2)

    txt(53, 109, "Corso di studio:", 7.2, True)
    field("CDS", 53, 120, 497, 17, record.get("cds"))
    txt(53, 135, "Insegnamento:", 7.2, True)
    txt(455, 135, "Sede:", 7.2, True)
    field("INSEGNAMENTO", 53, 146, 398, 17, record.get("insegnamento"))
    field("SEDE", 453, 146, 97, 17, record.get("sede"))
    txt(53, 161, "Modulo:", 7.2, True)
    txt(456, 161, "Unità Logistica/Partizione:", 6.8, True)
    field("MODULO", 53, 172, 399, 17, record.get("modulo"))
    field("UL", 453, 172, 97, 17, record.get("ul"))
    txt(53, 191, "Requisiti richiesti per l'accesso alla docenza:", 7.2, True)
    field("REQUISITI", 53, 203, 497, 19,
          record.get("requisiti") or "[DA VERIFICARE]", fontsize=7.5)

    headings = [
        (50, 83, ["Titoli e Attività", "da esaminare"]),
        (134, 266, ["Indicatori relativi al curriculum"]),
        (401, 44, ["Punti per", "titolo", "attività"]),
        (446, 53, ["Punti", "assegnati"]),
        (500, 50, ["Punteggio", "per area"]),
    ]
    for x, width, labels in headings:
        rect(x, 231, width, 34, MAGENTA)
        for j, label in enumerate(labels):
            centered(x, 235+j*9, width, label, 6.3 if x==401 else 7.6, True, white)

    rect(50, 267, 83, 104, BLUE)
    multiline(53, 286, [
        "Ulteriori titoli", "di studio / Attività", "di aggiornamento",
        "pertinenti", "all’insegnamento", "(ultimi 5 anni)",
    ], 7.5, leading=9)
    for i, (key, label, maximum) in enumerate(AREA1):
        top = 267 + i*17.33
        for x, width in [(134, 266), (401, 44), (446, 53)]:
            rect(x, top, width, 16.5, BLUE)
        txt(137, top+3, label, 7)
        centered(401, top+3, 44, "max " + str(maximum), 7)
        numbers(key, 449, top+1, 47, 14.3, maximum)
    area(500, 267, 50, 104, 15)

    rect(50, 373, 83, 85, BLUE)
    multiline(53, 391, [
        "Attività", "professionale svolta", "con continuità e in",
        "settori pertinenti", "all’insegnamento", "(ultimi 5 anni)",
    ], 6.6, leading=8.5)
    # L'unico quinquennio verificato nel modello fornito è il 2021-2025
    # per l'A.A. 2026-2027. Per altri bandi servono anni approvati.
    years = (
        ["2025", "2024", "2023", "2022", "2021"]
        if re.fullmatch(r"2026[-/]2027", aa) else ["[DA VER.]"] * 5
    )
    for i, year_label in enumerate(years):
        top = 373 + 17*i
        for x, width in [(134, 46), (181, 141), (324, 121), (446, 53)]:
            rect(x, top, width, 16, BLUE)
        txt(137, top+4, "Anno " + year_label, 7)
        choice(f"attivita{i}", 182, top+1, 139, 14, PRO_ACTIVITY)
        choice(f"plus{i}", 325, top+1, 119, 14, PRO_PLUS)
        field(f"extraField{i}", 449, top+1, 47, 14, "0",
              readonly=True, fontsize=7.4, background=BLUE)
    area(500, 373, 50, 85, 10)

    rect(50, 461, 83, 68, BLUE)
    multiline(53, 479, ["Attività didattica", "(ultimi 5 anni)"],
              7.4, leading=9)
    top = 461
    for (key, label, maxpt, desc), height in zip(AREA3, [27, 19, 19]):
        for x, width in [(134, 266), (401, 44), (446, 53)]:
            rect(x, top, width, height-1, BLUE)
        if key == "P14":
            multiline(137, top+3, [
                "Docenza in ambito accademico pertinente all’insegnamento",
                "(la stessa docenza va conteggiata una sola volta",
                "anche se svolta in più corsi di studio)",
            ], 6.8, leading=7.6)
        else:
            txt(137, top+5, label, 6.9)
        centered(401, top+3, 44,
                 desc if key != "P14" else "max 2/anno", 6.5)
        numbers(key, 449, top+2, 47, height-4, maxpt)
        top += height
    area(500, 461, 50, 68, 15)

    blocks = [
        (531, 28, ["Attività scientifica", "(ultimi 5 anni)"], 7,
         AREA4, ["Rilevanza", "scientifica"]),
        (561, 30, ["Valutazione didattica", "Questionario studenti"], 3,
         AREA5, ["3"]),
        (594, 30, ["Esperienze e", "competenze specifiche"], 10,
         AREA6, ["max 10"]),
    ]
    for top, height, title, maximum_area, item, hints in blocks:
        key, label, maximum = item
        rect(50, top, 83, height, BLUE)
        multiline(53, top+4, title, 7.2, leading=9)
        for x, width in [(134, 266), (401, 44), (446, 53)]:
            rect(x, top, width, height, BLUE)
        if key == "P19":
            multiline(137, top+4, [
                "Valorizzazione dell’esperienza e della competenza",
                "nel settore specifico richiesto dall’insegnamento del corso di laurea",
            ], 7, leading=9)
        else:
            txt(137, top+8, label, 7.1)
        for j, hint in enumerate(hints):
            centered(401, top+7+j*8, 44, hint, 7.2)
        numbers(key, 449, top+2, 47, height-4, maximum)
        area(500, top, 50, height, maximum_area)

    # Testo operativo fedele al documento: non introdurre una regola
    # diversa sui requisiti mancanti.
    multiline(53, 626, [
        "Tutte le variabili devono essere pesate in rapporto alla pertinenza del curriculum",
        "all'insegnamento/modulo indicato. Se i requisiti di accesso alla docenza non",
        "sono presenti non si procede alla valutazione e si assegna punteggio pari a zero.",
    ], 5.3, leading=6.2)
    for x, width in [(401, 45), (447, 51), (500, 50)]:
        rect(x, 626, width, 19, MAGENTA)
    centered(401, 629, 45, "TOTALE", 8.2, True, white)
    field("TOT", 449, 628, 47, 15, "0", readonly=True, fontsize=9,
          background=white)
    centered(500, 629, 50, "60", 10, True, white)

    txt(53, 650, "Note commissione / Esito:", 7.5, True)
    choice("STATUS", 210, 651, 340, 17, STATUSES)
    field("NOTE-COMM", 53, 673, 497, 49, "", readonly=False,
          fontsize=8, background=GRAY, multiline=True)
    txt(53, 731, "La Commissione:", 7.5, True)
    rect(53, 742, 497, 15, PALE)
    txt(56, 744, "Presidente:", 7.3, True)
    field("PRESIDENTE", 115, 743, 435, 14, record.get("presidente"),
          fontsize=7.7, background=PALE)
    rect(53, 759, 497, 15, PALE)
    txt(56, 761, "Componenti:", 7.3, True)
    field("COMPONENTI", 115, 760, 435, 14, record.get("componenti"),
          fontsize=7.5, background=PALE)
    rect(50, 781, 500, 1, INK)
    centered(50, 789, 500,
             "Documento da compilare e sottoporre successivamente a firma digitale.",
             8.2, True, INK)
    centered(50, 801, 500,
             "Firma e protocollo sono gestiti fuori da RankUp.", 7.6)
    field("RANKUP-ID", 53, 818, 497, 11,
          record.get("scheda_id") or "[DA VERIFICARE]",
          fontsize=6, background=white)
    pdf.save()
    return _calculations(out.getvalue())
