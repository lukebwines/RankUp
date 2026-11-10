"""Unica fonte tecnica dei massimali delle tre schede RankUP.

I valori riproducono senza variazioni quelli già presenti nei tre generatori
PDF e nel validatore prima di questa centralizzazione. Sono regole di
validazione dei modelli esistenti, NON nuovi criteri amministrativi.
"""
from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType

ORDINARY_MAX = MappingProxyType({
    **dict(zip((f"P{i:02d}" for i in range(1, 9)),
               (2, 2, 3, 2, 1, 1, 2, 2))),
    "P09": 10, "P10": 10, "P11": 2, "P12": 3,
    "P13": 10, "P14": 3, "P15": 7,
})
ENGLISH_MAX = MappingProxyType({
    **dict(zip((f"P{i:02d}" for i in range(1, 8)),
               (2, 3, 2, 1, 1, 2, 2))),
    "P08": 10, "P09": 5, "P10": 2, "P11": 3,
    "P12": 10, "P13": 7,
})
PROFESSIONAL_MAX = MappingProxyType({
    **dict(zip((f"P{i:02d}" for i in range(1, 7)),
               (3, 1, 2, 2, 3, 4))),
    "P14": 10, "P15": 2, "P16": 3, "P17": 7,
    "P18": 3, "P19": 10,
})

INDICATOR_LIMITS = MappingProxyType({
    "ordinario": ORDINARY_MAX,
    "inglese": ENGLISH_MAX,
    "professionalizzante": PROFESSIONAL_MAX,
})
AREA_LIMITS = MappingProxyType({
    "ordinario": (15, 10, 15, 10, 3, 7),
    "inglese": (13, 20, 10, 7),
    "professionalizzante": (15, 10, 15, 7, 3, 10),
})
TOTAL_LIMITS = MappingProxyType({
    "ordinario": 60,
    "inglese": 50,
    "professionalizzante": 60,
})
# Queste voci delle schede non consentono valori intermedi.
FIXED_VALUE_FIELDS = MappingProxyType({
    "ordinario": frozenset(("P14",)),
    "inglese": frozenset(("P02", "P05")),
    "professionalizzante": frozenset(("P18",)),
})

PRO_ACTIVITY_OPTIONS = (
    ("0", "NESSUNA (0 pt)"),
    ("1.5", "Dirigente Professioni Sanitarie (1.5 pt)"),
    ("1", "Coordinatore reparto/servizio (1 pt)"),
    ("0.5", "Collaboratore professionale (0.5 pt)"),
    ("0.25", "Libero professionista (0.25 pt)"),
    ("1.5", "Coordinatore Corso di Laurea (1.5 pt)"),
)
PRO_PLUS_OPTIONS = (
    ("0", "NESSUNA (0 pt)"),
    ("0.5", "Tutor dedicato (0.5 pt)"),
    ("0.5", "Guida/supervis. tirocin. (0.5 pt)"),
)
PRO_ACTIVITY_ALLOWED = frozenset(Decimal(v) for v, _ in PRO_ACTIVITY_OPTIONS)
PRO_PLUS_ALLOWED = frozenset(Decimal(v) for v, _ in PRO_PLUS_OPTIONS)


def indicator_limit(model: str, field: str) -> int:
    return INDICATOR_LIMITS[model][field]


def area_limit(model: str, position: int) -> int:
    """Posizione area 1-based, come visualizzata nel PDF."""
    return AREA_LIMITS[model][position - 1]


def total_limit(model: str) -> int:
    return TOTAL_LIMITS[model]


def fixed_value(model: str, field: str) -> bool:
    return field in FIXED_VALUE_FIELDS[model]
