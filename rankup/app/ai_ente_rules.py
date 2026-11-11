"""Verifica preliminare ente del PICA, basata sul prompt GroqCloud.

RankUP calcola localmente solo categorie prive di identificativi e scarta i
casi in cui non c'e' un requisito da verificare o mancano categorie minime.
Le equivalenze istituzionali vengono invece presentate all'AI nelle
istruzioni configurate dall'Admin. L'esito non attesta mai il rapporto di
lavoro o l'idoneita' amministrativa del candidato.
"""
from __future__ import annotations
from ai_groq import prepare_minimal_payload, role_category

RULES_VERSION = "ente-denominazione-originale-v4"
NON_APPLICABILE = "NON APPLICABILE"
ENTE_COERENTE = "ENTE COERENTE"
DA_APPROFONDIRE = "DA APPROFONDIRE"


def classifica_ente(entry: dict) -> dict:
    payload=prepare_minimal_payload(entry)
    req=payload["requisito_ente"]
    ente=payload["denominazione_ente_dichiarato"]
    relation=role_category(str(entry.get("occupazione") or ""),
                           str(entry.get("profilo_professionale") or ""))
    base={
        "esito":DA_APPROFONDIRE,
        "requisito":req,
        "categoria_ente":ente,
        "categoria_rapporto":relation,
        "verifica_rapporto":"La posizione lavorativa individuale deve essere verificata separatamente.",
        "richiede_ai":False,
        "tipo_approfondimento":"",
        "motivo":"",
        "verifiche":"",
        "payload":payload,
    }
    if req=="NESSUNA RISERVA":
        return {**base,"esito":NON_APPLICABILE,
                "tipo_approfondimento":"NON APPLICABILE",
                "verifica_rapporto":"Non applicabile al requisito di ente.",
                "motivo":"Nessuna riserva di ente per l'incarico.",
                "verifiche":"Nessuna verifica di appartenenza istituzionale necessaria."}
    if req=="NON CLASSIFICATO":
        return {**base,"tipo_approfondimento":"REQUISITO NON CLASSIFICATO",
                "motivo":"Il requisito dell'ente non e' classificato.",
                "verifiche":"Controllare il requisito ufficiale associato all'incarico."}
    if ente in ("NON DICHIARATO","DENOMINAZIONE NON TRASMETTIBILE"):
        return {**base,
                "tipo_approfondimento":("ENTE NON DICHIARATO" if ente=="NON DICHIARATO"
                                       else "FILTRO PRIVACY"),
                "motivo":"Ente non dichiarato o campo contenente dettagli non trasmettibili.",
                "verifiche":"Verificare localmente la denominazione azienda/ente del PICA."}
    return {**base,"richiede_ai":True,
            "tipo_approfondimento":"IN ATTESA AI",
            "motivo":"In attesa del riconoscimento AI della denominazione istituzionale.",
            "verifiche":"Verificare sempre separatamente il rapporto lavorativo dichiarato."}


def esito_da_indizio(indizio: str | None) -> str:
    """Il modello puo' suggerire coerenza dell'ente, mai l'idoneita' del candidato."""
    return ENTE_COERENTE if indizio=="INDIZI COERENTI" else DA_APPROFONDIRE
