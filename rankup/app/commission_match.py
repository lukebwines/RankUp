"""Associazione commissioni a CdS e sede, con confronti deterministici.

Le denominazioni PICA possono contenere in coda la specificazione
"(ABILITANTE ALLA PROFESSIONE ...)" assente dal file commissioni.
Si elimina esclusivamente questa specificazione finale, senza
confronti fuzzy, somiglianze parziali o attribuzioni tra sedi diverse.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("’", "'")
    return re.sub(r"\s+", " ", value.strip()).upper()


def course_key(text: str) -> str:
    value = _normalize(text)
    # La dicitura abilitante e' descrittiva e non modifica il CdS.
    # NON eliminare altri parentetici né fare confronti startswith.
    value = re.sub(r"\s*\(\s*ABILITANTE\b[^)]*\)\s*$", "", value)
    return value.strip()


def seat_key(text: str) -> str:
    return _normalize(text)


def commission_index(commission_rows):
    """Indice delle commissioni del singolo bando, con collisioni esplicite."""
    result = defaultdict(list)
    for row in commission_rows:
        key = (course_key(row["cds"]), seat_key(row["sede"]))
        result[key].append(dict(row))
    return result


def resolve_commission(index: dict, cds: str, sede: str):
    """Ritorna (stato, commissione) senza attribuzioni non dimostrate.

    Stato: associata | mancante | ambigua.
    """
    matches = index.get((course_key(cds), seat_key(sede)), ())
    if len(matches) == 1:
        return "associata", matches[0]
    if len(matches) > 1:
        return "ambigua", None
    return "mancante", None
