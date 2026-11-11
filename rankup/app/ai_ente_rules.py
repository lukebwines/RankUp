"""Verifica preliminare ente del PICA, basata sul prompt GroqCloud.

RankUP calcola localmente solo categorie prive di identificativi e scarta i
casi in cui non c'e' un requisito da verificare o mancano categorie minime.
Le equivalenze istituzionali vengono invece presentate all'AI nelle
istruzioni configurate dall'Admin. L'esito non attesta mai il rapporto di
lavoro o l'idoneita' amministrativa del candidato.
"""
from __future__ import annotations
from ai_groq import (prepare_minimal_payload, has_institutional_context,
                     role_category, ENT_E_MISSING, ENT_E_PRIVATE)

RULES_VERSION = "ente-contesto-pica-cinque-campi-v5"
NON_APPLICABILE = "NON APPLICABILE"
ENTE_COERENTE = "ENTE COERENTE"
DA_APPROFONDIRE = "DA APPROFONDIRE"


def classifica_ente(entry: dict) -> dict:
    payload=prepare_minimal_payload(entry)
    req=payload["requisito_ente"]
    ente=payload["denominazione_ente_dichiarato"]
    relation=role_category(str(entry.get("occupazione") or "")+" "+
                           str(entry.get("qualifica") or ""),
                           str(entry.get("profilo_professionale") or ""))
    sources={
        "denominazione_ente_dichiarato":"Azienda/ente",
        "ateneo_dichiarato":"Ateneo",
        "occupazione_dichiarata":"Occupazione",
        "profilo_professionale_dichiarato":"Profilo professionale",
        "qualifica_dichiarata":"Qualifica",
    }
    available=[label for field,label in sources.items()
               if payload.get(field) not in (ENT_E_MISSING,ENT_E_PRIVATE,"")]
    base={
        "esito":DA_APPROFONDIRE,
        "requisito":req,
        "categoria_ente":ente,
        "categoria_rapporto":relation,
        "fonti_pica":available,
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
    if not has_institutional_context(payload):
        return {**base,
                "tipo_approfondimento":("ENTE NON DICHIARATO" if ente==ENT_E_MISSING
                                       else "FILTRO PRIVACY"),
                "motivo":"Non risultano indicazioni istituzionali trasmissibili "
                         "nei campi Azienda/ente, Ateneo e Occupazione.",
                "verifiche":"Verificare localmente i campi originali PICA; "
                            "Profilo professionale e Qualifica da soli "
                            "non identificano un ente."}
    missing_bb=ente in (ENT_E_MISSING,ENT_E_PRIVATE)
    return {**base,"richiede_ai":True,
            "tipo_approfondimento":"IN ATTESA AI",
            "motivo":("Azienda/ente mancante o non trasmissibile: esaminare "
                      "Ateneo e Occupazione." if missing_bb else
                      "In attesa del confronto AI dei cinque campi PICA."),
            "verifiche":"Verificare separatamente la natura dell'ente "
                       "e il rapporto lavorativo personale."}


def esito_da_indizio(indizio: str | None) -> str:
    """Il modello puo' suggerire coerenza dell'ente, mai l'idoneita' del candidato."""
    return ENTE_COERENTE if indizio=="INDIZI COERENTI" else DA_APPROFONDIRE
