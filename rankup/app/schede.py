"""Instradamento dei modelli di valutazione PDF di RankUp.

Le schede sono generate da zero, senza incorporare dati dai PDF compilati
di esempio. Entrambi i modelli restano soggetti a validazione amministrativa.
"""
from __future__ import annotations

from professional import make_professional
from ordinary import make_ordinary


def make_scheda(record: dict, tipo: str) -> bytes:
    if tipo == "professionalizzante":
        return make_professional(record)
    if tipo == "ordinario":
        return make_ordinary(record)
    raise ValueError("Tipologia SSD non classificata: scheda non generabile")
