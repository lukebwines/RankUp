"""Verbale descrittivo della Commissione: formulario e copia statica reimpaginata.

Il documento originale restituito e' conservato senza modificarlo.
Il testo normativo viene esclusivamente dai Riferimenti del bando, mai
da facsimile o da valori inventati.
"""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
from io import BytesIO
import json
import re
import unicodedata
from xml.sax.saxutils import escape

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Flowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer,
)

# Logo originale ricavato dall'intestazione del modello Word fornito
# dall'utente. Versione monocromatica compressa senza ricostruzioni.
_LOGO_B64 = (
"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"
)
LOGO = base64.b64decode(_LOGO_B64)
MAX_PDF_DESCRITTIVO = 8 * 1024 * 1024
MAX_DISCUSSIONE = 35000

_BODY = ParagraphStyle(
    "TestoVerbale", fontName="Helvetica", fontSize=10, leading=15,
    alignment=TA_JUSTIFY, spaceAfter=9, splitLongWords=1,
)
_CENTER = ParagraphStyle(
    "CentroVerbale", parent=_BODY, alignment=TA_CENTER, fontSize=11,
    leading=15, spaceAfter=7,
)
_HEAD = ParagraphStyle(
    "TitoloVerbale", parent=_CENTER, fontName="Helvetica-Bold",
    fontSize=13, leading=17, spaceAfter=13,
)
_SMALL = ParagraphStyle(
    "NotaVerbale", parent=_BODY, fontSize=8, leading=11,
    alignment=TA_LEFT,
)
_RIGHT = ParagraphStyle(
    "FirmaVerbale", parent=_BODY, alignment=TA_RIGHT,
    fontSize=10, leading=16, spaceAfter=6,
)


def _text(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def source_digest(context: dict) -> str:
    fields = ("codice", "cds", "sede", "anno_accademico", "repertorio",
              "protocollo", "data_avviso", "presidente", "componenti", "testo_bando")
    payload = {name: _text(context.get(name)) for name in fields}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def nome_verbale(code: str, cds: str, sede: str) -> str:
    """Massimo 55 caratteri, nome discriminante anche con corsi lunghi."""
    n = unicodedata.normalize("NFKD", cds).encode("ascii", "ignore").decode().upper()
    s = unicodedata.normalize("NFKD", sede).encode("ascii", "ignore").decode().upper()
    reduced = re.sub(r"[^A-Z0-9]+", "_", n).strip("_")
    seat = {"VERONA": "VR", "VICENZA": "VI", "TRENTO": "TN",
            "BOLZANO": "BZ", "LEGNAGO": "LG", "ROVERETO": "RVT"}.get(s)
    if not seat:
        seat = re.sub(r"[^A-Z0-9]+", "", s)[:6] or "SEDE"
    # Abbreviazioni approvate per i CdS dell'area Medicina.
    # Le corrispondenze più specifiche precedono quelle generiche.
    approved = (
        ("SCIENZE_INFERMIERISTICHE_SPECIALISTICHE", "LM_INF_FAM_COM"),
        ("TECNICHE_DI_FISIOPATOLOGIA_CARDIOCIRCOLATORIA", "FISIOP_CARDIO_PERF"),
        ("TECNICHE_DELLA_PREVENZIONE", "TEC_PREVENZIONE"),
        ("TECNICA_DELLA_RIABILITAZIONE_PSICHIATRICA", "RIAB_PSI"),
        ("SCIENZE_DELLE_PROFESSIONI_SANITARIE_TECNICHE_ASSISTENZIALI", "LM_PROF_TEC_ASS"),
        ("SCIENZE_DELLE_PROFESSIONI_SANITARIE_DELLA_RIABILITAZIONE", "LM_SCI_RIABIL"),
        ("SCIENZE_RIABILITATIVE_DELLE_PROFESSIONI_SANITARIE", "LM_SCI_RIABIL"),
        ("SCIENZE_NUTRACEUTICHE", "SC_NUTRACEUTICHE"),
        ("BIOLOGY_FOR_TRANSLATIONAL_RESEARCH", "LM_BIO_TRANSL"),
        ("MEDICINA_E_CHIRURGIA_A_INDIRIZZO_TECNOLOGICO", "MED_CHIR_TEC"),
        ("MEDICINA_E_CHIRURGIA_INTERATENEO", "MED_CHIR"),
        ("SCIENZE_INFERMIERISTICHE_E_OSTETRICHE", "LM_INF_OST"),
        ("TECNICHE_DI_LABORATORIO_BIOMEDICO", "LAB_BIOMEDICO"),
        ("TECNICHE_DI_RADIOLOGIA_MEDICA", "RADIOL_MEDICA"),
        ("TECNICHE_AUDIOPROTESICHE", "TEC_AUDIOPROTESICHE"),
        ("TECNICHE_ORTOPEDICHE", "TEC_ORTOPEDICHE"),
        ("ODONTOIATRIA_E_PROTESI_DENTARIA", "ODONTOIATRIA"),
        ("ASSISTENZA_SANITARIA", "ASS_SANITARIA"),
        ("IGIENE_DENTALE", "IGIENE_DENTALE"),
        ("FISIOTERAPIA", "FISIOTERAPIA"),
        ("INFERMIERISTICA", "INFERMIERISTICA"),
        ("LOGOPEDIA", "LOGOPEDIA"),
        ("OSTEOPATIA", "OSTEOPATIA"),
        ("OSTETRICIA", "OSTETRICIA"),
        ("FARMACIA", "FARMACIA"),
        ("MEDICINA_E_CHIRURGIA", "MED_CHIR"),
    )
    slug = next((abbr for phrase,abbr in approved if phrase in reduced), reduced)
    raw = "VB_" + slug + "_" + seat + "_" + re.sub(r"[^A-Z0-9-]", "", code.upper()) + ".pdf"
    if len(raw) <= 55:
        return raw
    tag = hashlib.sha256((code + "|" + cds + "|" + sede).encode("utf-8")).hexdigest()[:6].upper()
    tail = "_" + seat[:6] + "_" + code[:14].upper() + "_" + tag + ".pdf"
    return "VB_" + slug[:max(1, 55 - len("VB_") - len(tail))].rstrip("_") + tail


def _header(canvas, doc, context: dict, editable: bool) -> None:
    canvas.saveState()
    width, height = A4
    canvas.drawImage(ImageReader(BytesIO(LOGO)), 46, height - 101,
                     width=195, height=49, mask="auto")
    canvas.setStrokeColor(colors.HexColor("#D4DDE6"))
    canvas.line(47, height - 110, width - 47, height - 110)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#63707D"))
    canvas.drawRightString(width - 48, 35, "Pagina " + str(doc.page))
    if editable:
        canvas.setKeywords("RANKUP-VERBALE:" + json.dumps({
            "codice": context["codice"], "cds": context["cds"],
            "sede": context["sede"], "sha256": source_digest(context),
            "form_version": 1,
        }, ensure_ascii=False, separators=(",", ":")))
    canvas.restoreState()


class _CampiSeduta(Flowable):
    def __init__(self, editable: bool, data: str = "", ora: str = ""):
        Flowable.__init__(self)
        self.width, self.height = 480, 34
        self.editable, self.data, self.ora = editable, data, ora

    def draw(self):
        c = self.canv
        c.setFont("Helvetica", 10)
        c.drawString(0, 14, "In data")
        c.drawString(170, 14, "alle ore")
        if self.editable:
            for name, x, w, hint in (
                ("data_seduta", 44, 107, "Data GG/MM/AAAA"),
                ("ora_seduta", 218, 72, "Ora HH:MM"),
            ):
                # Solo una riga di guida: niente riquadro per data e ora.
                # Nel PDF statico non viene disegnata neppure questa riga.
                c.setStrokeColor(colors.HexColor("#A7B3BF"))
                c.line(x, 7, x+w, 7)
                c.acroForm.textfield(
                    name=name, tooltip=hint, x=x, y=8, width=w, height=22,
                    borderWidth=0, borderStyle="solid",
                    fillColor=colors.white, textColor=colors.black,
                    fontName="Helvetica", fontSize=10, maxlen=20,
                )
        else:
            c.drawString(44, 14, self.data)
            c.drawString(218, 14, self.ora)


class _CampoDiscussione(Flowable):
    def __init__(self):
        Flowable.__init__(self)
        self.width, self.height = 485, 248

    def draw(self):
        self.canv.acroForm.textfield(
            name="riassunto_discussione",
            tooltip="Riassunto integrale della discussione della Commissione",
            x=0, y=2, width=480, height=238,
            borderWidth=0.7, borderStyle="solid",
            borderColor=colors.HexColor("#C5CFDA"),
            fillColor=colors.white, textColor=colors.black,
            fontName="Helvetica", fontSize=10, maxlen=MAX_DISCUSSIONE,
            fieldFlags="multiline", forceBorder=True,
        )


def _paragraphs_from_source(source: str, style=_BODY) -> list:
    result = []
    for line in _text(source).split("\n"):
        if line.strip():
            result.append(Paragraph(escape(line), style))
        else:
            result.append(Spacer(1, 6))
    return result


def _story(context: dict, *, editable: bool, values: dict | None = None) -> list:
    vals = values or {}
    refparts = []
    for key, label in (("repertorio", "Rep."), ("protocollo", "Prot."), ("data_avviso", "del")):
        v = _text(context.get(key))
        if v:
            refparts.append(label + " " + v)
    story = [
        Paragraph("VERBALE DELLA COMMISSIONE DI VALUTAZIONE", _HEAD),
        Paragraph("per il conferimento di incarichi di insegnamento" +
                  (" per l'A.A. " + escape(_text(context.get("anno_accademico")))
                   if _text(context.get("anno_accademico")) else ""), _CENTER),
        Paragraph("<b>Corso di Studio:</b> " + escape(context["cds"]), _BODY),
        Paragraph("<b>Sede:</b> " + escape(context["sede"]), _BODY),
        Paragraph("<b>Bando:</b> " + escape(context["codice"]) +
                  ((" — " + escape(" ".join(refparts))) if refparts else ""), _BODY),
        Spacer(1, 10),
        _CampiSeduta(editable, _text(vals.get("data_seduta")), _text(vals.get("ora_seduta"))),
        Paragraph("si è riunita la Commissione di valutazione per le domande "
                  "di incarico di insegnamento riferite al bando indicato.", _BODY),
        Paragraph("La Commissione risulta così composta:", _BODY),
        Paragraph("<b>PRESIDENTE:</b> " +
                  escape(_text(context.get("presidente")) or "[DA VERIFICARE]"), _BODY),
        Paragraph("<b>COMPONENTI:</b> " +
                  escape(_text(context.get("componenti")) or "[DA VERIFICARE]"), _BODY),
        Paragraph("La Commissione, dopo le verifiche di competenza, prende "
                  "visione delle candidature e procede alla valutazione "
                  "in conformità alle disposizioni applicabili al bando.", _BODY),
        Spacer(1, 7),
        Paragraph("<b>Disposizioni del bando richiamate dalla Commissione</b>", _BODY),
    ]
    source = _text(context.get("testo_bando"))
    if not source:
        source = "[DA VERIFICARE] — Inserire in Riferimenti del bando il testo ufficiale applicabile."
    story.extend(_paragraphs_from_source(source))
    story.append(Spacer(1, 15))
    story.append(Paragraph("<b>Riassunto della discussione della Commissione</b>", _BODY))
    if editable:
        story.append(_CampoDiscussione())
        story.append(Paragraph(
            "Il campo può scorrere durante la compilazione; RankUP lo reimpagina "
            "integralmente quando acquisisce il PDF e produce la copia statica.",
            _SMALL,
        ))
    else:
        story.extend(_paragraphs_from_source(_text(vals.get("riassunto_discussione"))))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "La Commissione procede alla compilazione delle schede di valutazione "
        "allegate al verbale e alla formulazione delle conseguenti risultanze.", _BODY))
    story.append(Spacer(1, 14))
    story.append(KeepTogether([
        Paragraph("Per la Commissione di Valutazione", _RIGHT),
        Paragraph("Il Presidente", _RIGHT),
        Paragraph("______________________________", _RIGHT),
    ]))
    return story


def genera_pdf(context: dict, *, editable: bool = True, values: dict | None = None) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=48, rightMargin=48,
        topMargin=126, bottomMargin=62,
        title="Verbale descrittivo Commissione - " + context["cds"],
        author="RankUP",
    )
    cb = lambda c, d: _header(c, d, context, editable)
    doc.build(_story(context, editable=editable, values=values),
              onFirstPage=cb, onLaterPages=cb)
    data = buffer.getvalue()
    reader = PdfReader(BytesIO(data), strict=False)
    if reader.is_encrypted:
        raise ValueError("PDF protetto inatteso")
    if editable:
        fields = reader.get_fields() or {}
        for name in ("data_seduta", "ora_seduta", "riassunto_discussione"):
            if name not in fields:
                raise ValueError("Campo PDF non generato: " + name)
    elif reader.get_fields():
        raise ValueError("Il verbale statico contiene campi compilabili")
    return data


def acquisisci_pdf(payload: bytes, expected: dict) -> tuple[dict, bytes]:
    if not payload or len(payload) > MAX_PDF_DESCRITTIVO or not payload.startswith(b"%PDF-"):
        raise ValueError("Il verbale deve essere un PDF valido di massimo 8 MB")
    try:
        reader = PdfReader(BytesIO(payload), strict=False)
        if reader.is_encrypted:
            raise ValueError("PDF protetto: impossibile leggere i campi")
        fields = reader.get_fields() or {}
        if any(str(v.get("/FT", "")) == "/Sig" for v in fields.values()):
            raise ValueError("PDF già firmato: acquisire la versione editabile prima della firma")
        raw_meta = str((reader.metadata or {}).get("/Keywords") or "")
        if not raw_meta.startswith("RANKUP-VERBALE:"):
            raise ValueError("PDF non prodotto da RankUP come verbale descrittivo")
        meta = json.loads(raw_meta[len("RANKUP-VERBALE:"):])
        for key in ("codice", "cds", "sede"):
            if meta.get(key) != expected[key]:
                raise ValueError("Il verbale appartiene a un altro bando/CdS/sede")
        if meta.get("sha256") != source_digest(expected):
            raise ValueError(
                "Dati del bando o della Commissione cambiati: rigenerare il PDF")
        vals = {key: _text(fields.get(key, {}).get("/V", ""))
                for key in ("data_seduta", "ora_seduta", "riassunto_discussione")}
        if not vals["data_seduta"] or not vals["ora_seduta"] or not vals["riassunto_discussione"]:
            raise ValueError("Compilare data, ora e riassunto della discussione")
        if len(vals["riassunto_discussione"]) > MAX_DISCUSSIONE:
            raise ValueError("Riassunto troppo lungo per il verbale")
        try:
            if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", vals["data_seduta"]):
                raise ValueError()
            datetime.strptime(vals["data_seduta"], "%d/%m/%Y")
            if not re.fullmatch(r"\d{2}:\d{2}", vals["ora_seduta"]):
                raise ValueError()
            datetime.strptime(vals["ora_seduta"], "%H:%M")
        except ValueError:
            raise ValueError("Data/ora non valide: usare GG/MM/AAAA e HH:MM") from None
        # Ricomposizione statica: non incorpora appearance stream dei widget,
        # evitando la perdita di righe nascoste da campi PDF a scorrimento.
        static = genera_pdf(expected, editable=False, values=vals)
        if not all(isinstance(x, str) for x in vals.values()):
            raise ValueError("Campi della discussione non leggibili")
        return vals, static
    except (OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("Verbale non leggibile o con struttura incompatibile") from exc
