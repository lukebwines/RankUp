"""Campi aggiuntivi degli incarichi PICA e normalizzazione non distruttiva.

I valori originali restano distinti dalle date ISO derivate dai seriali Excel.
Peso Copertura e Ore NON sono equivalenti.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re

# Chiavi lette dalle intestazioni ufficiali PICA, non da lettere fisse.
EXTRA_PICA_FIELDS = {
    "anno_corso": ["anno del corso"],
    "tipo_corso": ["tipo corso"],
    "tipo_cds": ["tipo cds"],
    "ciclo": ["ciclo"],
    "inizio": ["inizio"],
    "fine": ["fine"],
    "codice_partizione": ["codice partizione studenti"],
    "partizione_studenti": ["partizione studenti"],
    "peso_copertura": ["peso copertura"],
    "ore": ["ore"],
    "stato_coperture": ["stato coperture"],
}

EXTRA_SOURCE_KEYS = tuple(EXTRA_PICA_FIELDS)
EXTRA_DB_FIELDS = (
    "anno_corso", "tipo_corso", "tipo_cds", "ciclo",
    "inizio_pica", "fine_pica", "inizio_iso", "fine_iso",
    "codice_partizione", "partizione_studenti",
    "peso_copertura", "ore", "stato_coperture",
)


def normalize_excel_date(raw: str) -> str:
    """Data ISO YYYY-MM-DD, solo se il seriale Excel 1900 è valido.

    L'export PICA esaminato usa il sistema Excel 1900. Una conversione non
    riconoscibile restituisce stringa vuota, lasciando intatto il raw.
    """
    source = str(raw or "").strip()
    if not source:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(source, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        serial = Decimal(source)
        if serial != serial.to_integral_value():
            return ""
        day = int(serial)
        if day < 61 or day > 2958465:
            return ""
        return (date(1899, 12, 30) + timedelta(days=day)).isoformat()
    except (InvalidOperation, OverflowError, ValueError):
        return ""


def incarico_extra_db(item: dict) -> dict[str, str]:
    """Mappa i campi PICA supplementari in colonne persistenti, senza inferenze."""
    original_start = str(item.get("inizio") or "").strip()
    original_end = str(item.get("fine") or "").strip()
    result = {
        key: str(item.get(key) or "").strip()
        for key in EXTRA_SOURCE_KEYS
        if key not in ("inizio", "fine")
    }
    result["inizio_pica"] = original_start
    result["fine_pica"] = original_end
    result["inizio_iso"] = normalize_excel_date(original_start)
    result["fine_iso"] = normalize_excel_date(original_end)
    return {field: result.get(field, "") for field in EXTRA_DB_FIELDS}
