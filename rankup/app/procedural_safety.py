"""Controlli procedimentali RankUP, privi di effetti sui dati.

Non determina graduatorie o vincitori: confronta esclusivamente le versioni
che la Commissione ha già validato e quelle congelate nei PDF definitivi.
"""
from __future__ import annotations

import hashlib
import json


def impronta_merito(graduatoria: dict) -> dict:
    """Identifica decisioni, schede e collocazione senza dati anagrafici."""
    risultato = {}
    for incarico in graduatoria.get("incarichi", []):
        candidato = {}
        for item in incarico.get("candidature", []):
            candidato[str(item["candidatura_id"])] = {
                "valutazione_id": item.get("valutazione_id"),
                "decisione": item.get("decisione"),
                "esito": item.get("esito"),
                "punteggio": item.get("punteggio"),
                "posizione": item.get("posizione"),
                "note": item.get("note"),
            }
        risultato[str(incarico["id"])] = {
            "vincitore_id": incarico.get("vincitore_id"),
            "candidature": candidato,
        }
    return risultato


def rettifica_sostanziale(prima: dict, dopo: dict) -> bool:
    """È sostanziale se cambia una scheda, un esito, il merito o il vincitore."""
    return impronta_merito(prima) != impronta_merito(dopo)


def anomalie_approvazione(
    snapshot_cds: dict,
    snapshot_attuale: dict,
    dati_json: str,
    hash_dati: str,
    pdf_definitivo: bytes | None,
    hash_pdf: str | None,
) -> list[str]:
    """Controlla tutto senza modificare il procedimento né sostituire la Commissione."""
    problemi = []
    try:
        salvato = json.loads(dati_json)
    except (TypeError, ValueError):
        return ["Snapshot della graduatoria non leggibile"]
    if hashlib.sha256(dati_json.encode("utf-8")).hexdigest() != hash_dati:
        problemi.append("Impronta dei dati della graduatoria non coerente")
    if salvato != snapshot_cds:
        problemi.append("La bozza di approvazione non coincide con la graduatoria definitiva")
    if not pdf_definitivo or not hash_pdf:
        problemi.append("PDF definitivo mancante")
    elif hashlib.sha256(pdf_definitivo).hexdigest() != hash_pdf:
        problemi.append("Integrità del PDF definitivo non verificata")
    if not snapshot_attuale.get("verbale_generabile"):
        problemi.append("Schede, Commissione o parità non più conformi")
    if rettifica_sostanziale(salvato, snapshot_attuale):
        problemi.append("Le valutazioni attuali differiscono dai dati definitivi")
    return problemi
