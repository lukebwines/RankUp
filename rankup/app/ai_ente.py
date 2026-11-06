"""Opt-in OpenAI Responses API per suggerire requisiti di ente da bando PUBBLICO.

NON inviare dati PICA, candidature, nomi candidati, CV o allegati personali.
L'output non viene salvato né applicato senza conferma umana.
"""
from __future__ import annotations

import json
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


VALID={"SSN","AOUI","ASUIT","UNITN","SSP","ASUIT O UNITN","NESSUNA RISERVA"}


class AIEmployerError(RuntimeError):
    pass


def propose(public_bando_text: str, assignments: list[dict], api_key: str,
            model: str = "gpt-6-luna") -> list[dict]:
    if not api_key or len(api_key)<15:
        raise AIEmployerError("Chiave API IA non configurata")
    if not public_bando_text.strip():
        raise AIEmployerError("Il PDF non contiene testo estraibile")
    rows=[
        {"key":str(r["key"]),"cds":str(r.get("cds") or "")[:180],
         "sede":str(r.get("sede") or "")[:80],
         "insegnamento":str(r.get("insegnamento") or "")[:180],
         "modulo":str(r.get("modulo") or "")[:180]}
        for r in assignments[:200]
    ]
    # Limitare il contesto alle parti finali, dove è normalmente l'allegato.
    # Se un requisito non appare in testo leggibile, restituire null.
    excerpt=public_bando_text[-75000:]
    prompt=(
        "Estrai esclusivamente i requisiti di ENTE DI APPARTENENZA richiesto "
        "per le coperture elencate dall'allegato del bando. "
        "Non valutare persone o candidature. Non inventare dati. "
        "Restituisci SOLTANTO JSON nella forma "
        '{"proposte":[{"key":"chiave incarico","ente":"SSN/AOUI/ASUIT/UNITN/SSP/ASUIT O UNITN/NESSUNA RISERVA oppure null",'
        '"citazione":"brevi parole identiche presenti nel PDF"}]}. '
        "Non attribuire NESSUNA RISERVA se la colonna dell'ente non è leggibile: usa null. "
        "La citazione deve essere testualmente presente nel PDF, "
        "altrimenti null. In caso di dubbio usa null. "
        "Insegnamenti similari o omonimi non devono essere confusi.\n\n"
        "INCARICHI:\n"+json.dumps(rows,ensure_ascii=False)+
        "\n\nTESTO PUBBLICO BANDO:\n"+excerpt
    )
    payload={
        "model":model,"store":False,
        "input":[{"role":"user","content":prompt}],
    }
    request=Request("https://api.openai.com/v1/responses",
        data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),
        headers={"Authorization":"Bearer "+api_key,"Content-Type":"application/json"},
        method="POST")
    try:
        with urlopen(request,timeout=55) as response:
            body=json.loads(response.read(800000))
    except (HTTPError,URLError,TimeoutError,ValueError) as exc:
        raise AIEmployerError("Servizio IA non disponibile o risposta non valida") from exc
    texts=[]
    for output in body.get("output",[]):
        if isinstance(output,dict):
            texts.extend(x.get("text","") for x in output.get("content",[])
                         if isinstance(x,dict) and x.get("type")=="output_text")
    answer="\n".join(texts).strip()
    if answer.startswith("```"):
        answer=re.sub(r"^```(?:json)?\s*|\s*```$","",answer,flags=re.I)
    try:
        parsed=json.loads(answer)
    except (ValueError,TypeError) as exc:
        raise AIEmployerError("Il modello non ha restituito un JSON verificabile") from exc
    allowed={r["key"] for r in rows}
    proposals=[]
    seen=set()
    for item in (parsed.get("proposte") or []):
        if not isinstance(item,dict):continue
        key=str(item.get("key") or "")
        ente=str(item.get("ente") or "").upper().strip()
        quote=str(item.get("citazione") or "").strip()
        if key not in allowed or key in seen or ente not in VALID:continue
        # Il modello NON può fabbricare una citazione assente nel documento.
        if len(quote)<3 or quote.casefold() not in excerpt.casefold():continue
        seen.add(key)
        proposals.append({"key":key,"ente_proposto":ente,"citazione":quote[:250],
                          "stato":"DA CONFERMARE"})
    return proposals
