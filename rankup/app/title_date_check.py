"""Controllo istruttorio del solo intervallo temporale tra date PICA e bando.

Nessuna valutazione di titolo, deroga, ammissibilità o stato delle schede.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import re


ITALIAN_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}


def parse_pica_title_date(value: object) -> date | None:
    """Accetta date Excel, ISO, GG/MM/AAAA e date italiane in lettere."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    # Excel 1900; seriali moderni. Si conserva sempre il valore PICA originale.
    if re.fullmatch(r"[0-9]{4,6}(?:\.0+)?", raw):
        serial = int(float(raw))
        if 61 <= serial <= 2958465:
            try:
                return date(1899, 12, 30) + timedelta(days=serial)
            except (ValueError, OverflowError):
                return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    match = re.fullmatch(r"\s*([0-9]{1,2})\s+([A-Za-zÀ-ÿ]+)\s+([0-9]{4})\s*", raw)
    if match:
        month = ITALIAN_MONTHS.get(match.group(2).lower())
        if month:
            try:
                return date(int(match.group(3)), month, int(match.group(1)))
            except ValueError:
                return None
    return None


def check_three_years(source_date: object, deadline: str) -> dict[str, str]:
    """Sola segnalazione temporale, mai decisione sull'ammissione."""
    source = str(source_date or "").strip()
    info = {"stato": "DA VERIFICARE", "data_iso": "", "limite_iso": "",
            "motivo": ""}
    issued = parse_pica_title_date(source_date)
    if issued:
        info["data_iso"] = issued.isoformat()
    if not deadline:
        info["motivo"] = "Scadenza candidature non inserita nei riferimenti del bando"
        return info
    try:
        reference = datetime.strptime(deadline, "%Y-%m-%dT%H:%M").date()
    except ValueError:
        info["motivo"] = "Scadenza candidature non interpretabile"
        return info
    try:
        limit = reference.replace(year=reference.year - 3)
    except ValueError:
        # Anniversario del 29 febbraio negli anni non bisestili.
        limit = reference.replace(year=reference.year - 3, day=28)
    info["limite_iso"] = limit.isoformat()
    if not issued:
        info["motivo"] = "Data di conseguimento PICA assente o non interpretabile"
    elif issued <= limit:
        info["stato"] = "OK"
        info["motivo"] = "Almeno tre anni alla scadenza del bando"
    else:
        info["stato"] = "ATTENZIONE"
        info["motivo"] = "Meno di tre anni alla scadenza del bando"
    return info
