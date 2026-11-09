"""Chiusura dei campi AcroForm nelle copie definitive; originali sempre intatti.

pypdf incorpora le apparenze /V nei flussi di pagina e rimuove i widget.
La funzione opera esclusivamente in memoria: non riscrive i documenti originali.
"""
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject


def appiattisci_scheda(originale: bytes) -> bytes:
    lettore = PdfReader(BytesIO(originale), strict=False)
    if lettore.is_encrypted:
        raise ValueError("Scheda PDF protetta: impossibile appiattire con certezza")
    scrittore = PdfWriter()
    scrittore.append(BytesIO(originale))
    campi = scrittore.get_fields() or {}
    if campi:
        valori = {}
        for nome, campo in campi.items():
            if str(campo.get("/FT", "")) == "/Sig":
                raise ValueError("Scheda con firma digitale: non alterare i campi firmati")
            valore = campo.get("/V")
            if isinstance(valore, list):
                valori[nome] = [str(x) for x in valore]
            else:
                valori[nome] = str(valore) if valore is not None else ""
        # La visualizzazione deriva dal valore effettivo del campo, non da
        # un'eventuale anteprima /AP non aggiornata dal compilatore del PDF.
        scrittore.update_page_form_field_values(
            None, valori, auto_regenerate=False, flatten=True
        )
    # Non basta il flag ReadOnly: rimuoviamo i widget modificabili.
    scrittore.remove_annotations(subtypes="/Widget")
    scrittore._root_object.pop(NameObject("/AcroForm"), None)
    destinazione = BytesIO()
    scrittore.write(destinazione)
    result = destinazione.getvalue()
    verifica = PdfReader(BytesIO(result), strict=False)
    if verifica.get_fields():
        raise ValueError("Sono rimasti campi compilabili nella scheda definitiva")
    if any(any(str(a.get_object().get("/Subtype", "")) == "/Widget"
                   for a in (p.get("/Annots") or [])) for p in verifica.pages):
        raise ValueError("Sono rimasti widget modificabili nella scheda definitiva")
    if len(verifica.pages) != len(lettore.pages):
        raise ValueError("Il numero di pagine è cambiato durante l'appiattimento")
    return result


def verbale_definitivo(frontespizio: bytes, allegati: list[bytes]) -> bytes:
    """Un PDF statico: frontespizio generato e schede appiattite separatamente."""
    if not allegati:
        raise ValueError("Nessuna scheda da allegare al verbale")
    result = PdfWriter()
    result.append(BytesIO(frontespizio))
    for originale in allegati:
        result.append(BytesIO(appiattisci_scheda(originale)))
    result._root_object.pop(NameObject("/AcroForm"), None)
    buffer = BytesIO()
    result.write(buffer)
    payload = buffer.getvalue()
    controllo = PdfReader(BytesIO(payload), strict=False)
    if controllo.get_fields():
        raise ValueError("Il verbale contiene ancora campi compilabili")
    if any(any(str(a.get_object().get("/Subtype", "")) == "/Widget"
                   for a in (p.get("/Annots") or [])) for p in controllo.pages):
        raise ValueError("Il verbale contiene ancora annotazioni compilabili")
    return payload
