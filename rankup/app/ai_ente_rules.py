"""Verifica preliminare dell'ente dichiarato nei dati PICA.

Classificazione istituzionale deterministica, separata dal rapporto di lavoro.
Non si pronuncia mai sull'idoneita' o sull'esclusione di una candidatura.

Fonti istituzionali per ASUIT/APSS e Servizio sanitario:
https://www.asuit.tn.it/azienda-sanitaria-universitaria-integrata-del-trentino
Gli enti vengono riconosciuti solo attraverso le categorie gia' estratte
localmente, non in base alla somiglianza semantica del modello AI.
"""
from __future__ import annotations

from ai_groq import prepare_minimal_payload

RULES_VERSION = "ente-istituzionale-v2"
NON_APPLICABILE = "NON APPLICABILE"
ENTE_COERENTE = "ENTE COERENTE"
DA_APPROFONDIRE = "DA APPROFONDIRE"

# Organizzazioni pubbliche che partecipano al sistema sanitario nazionale
# o provinciale; l'appartenenza non prova il rapporto subordinato del singolo.
SANITARI_RICONOSCIUTI = frozenset({"AOUI", "ASUIT", "APSS", "SSP"})

def _compatibile(requisito: str, ente: str) -> bool:
    if requisito == "SSN":
        return ente in SANITARI_RICONOSCIUTI
    if requisito == "ASUIT O UNITN":
        return ente in ("ASUIT","APSS","UNITN")
    if requisito == "ALTRO ATENEO":
        return ente in ("ALTRO ATENEO","UNITN")
    if requisito in ("AOUI","ASUIT","APSS","SSP","UNITN"):
        if requisito in ("ASUIT","APSS") and ente in ("ASUIT","APSS"):
            return True  # continuita' giuridica dell'azienda trentina
        return requisito == ente
    return False


def classifica_ente(entry: dict) -> dict:
    """Restituisce un esito istituzionale motivato e il requisito sul rapporto.

    L'AI puo' intervenire solo se entrambe le categorie sono definite,
    ma non risultano riconciliate con le regole note.
    """
    payload = prepare_minimal_payload(entry)
    requisito = payload["requisito_ente"]
    ente = payload["categoria_ente_dichiarato"]
    rapporto = payload["categoria_rapporto_dichiarato"]
    base = {
        "esito": DA_APPROFONDIRE,
        "requisito": requisito,
        "categoria_ente": ente,
        "categoria_rapporto": rapporto,
        "verifica_rapporto": ("Rapporto di lavoro da verificare sulla dichiarazione "
                             "e sulla documentazione ufficiale."),
        "richiede_ai": False,
        "motivo": "",
        "verifiche": "",
        "payload": payload,
    }
    if requisito == "NESSUNA RISERVA":
        return {**base,"esito":NON_APPLICABILE,
                "verifica_rapporto":"Non applicabile al requisito di ente.",
                "motivo":"L'incarico non prevede una riserva di ente da confrontare.",
                "verifiche":"Nessun controllo dell'ente richiesto."}
    if requisito == "NON CLASSIFICATO":
        return {**base,
                "motivo":"Il requisito dell'ente non e' classificato.",
                "verifiche":"Verificare il requisito nell'incarico e nella fonte ufficiale."}
    if ente in ("NON DICHIARATO","ENTE NON CLASSIFICATO"):
        return {**base,
                "motivo":"L'ente dichiarato e' assente o non classificabile.",
                "verifiche":"Verificare localmente il campo azienda/ente del report PICA."}
    if ente == "STRUTTURA SANITARIA (SSN DA VERIFICARE)":
        return {**base,
                "motivo":"La denominazione della struttura non prova l'appartenenza al SSN.",
                "verifiche":"Identificare la struttura e verificarne la natura giuridica."}
    if _compatibile(requisito,ente):
        return {**base,"esito":ENTE_COERENTE,
                "motivo":"La categoria istituzionale dell'ente e' coerente con il requisito.",
                "verifiche":"La coerenza dell'ente NON prova che il candidato ne sia dipendente. "
                            "Verificare separatamente il rapporto di lavoro dichiarato."}
    return {**base,"richiede_ai":True,
            "motivo":"Le categorie di ente e requisito non risultano riconciliate "
                     "dalle regole istituzionali certe.",
            "verifiche":"Verificare l'eventuale rapporto assistenziale, "
                        "convenzionamento o altra condizione prevista dal bando."}
