"""Instradamento dei modelli di valutazione PDF di RankUp.

Le schede sono generate da zero, senza incorporare dati dai PDF compilati
di esempio. I tre modelli restano soggetti a validazione amministrativa.
"""
from __future__ import annotations

from professional import make_professional
from ordinary import make_ordinary
from english import make_english


def make_scheda(record: dict, tipo: str) -> bytes:
    if tipo == "professionalizzante":
        return make_professional(record)
    if tipo == "ordinario":
        return make_ordinary(record)
    if tipo == "inglese":
        return make_english(record)
    raise ValueError("Tipologia SSD non classificata: scheda non generabile")
