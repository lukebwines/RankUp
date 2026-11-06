"""Avviso istruttorio sintetico nelle schede PDF di valutazione.

La fascia appare soltanto se il requisito è riservato o non classificato.
Non compila esiti o note della commissione, non certifica rapporti di lavoro.
"""
from reportlab.lib.colors import HexColor
from reportlab.pdfbase.pdfmetrics import stringWidth


def draw_occupazione_warning(pdf, record: dict, page_height: float):
    message = str(record.get("attenzione_occupazione") or "").strip()
    if not message:
        return
    # Fascia tra il campo REQUISITI e l'inizio della griglia punteggi.
    x, top, width, height = 53, 222.4, 497, 8.2
    pdf.setFillColor(HexColor("#ffe0e0"))
    pdf.rect(x, page_height-top-height, width, height, stroke=0, fill=1)
    font, size = "Helvetica-Bold", 6.3
    while size > 5.1 and stringWidth(message, font, size) > width-6:
        size -= 0.2
    if stringWidth(message, font, size) > width-6:
        message = message[:107].rstrip() + "..."
    pdf.setFillColor(HexColor("#a40000"))
    pdf.setFont(font, size)
    pdf.drawString(x+3, page_height-top-size-0.5, message)
