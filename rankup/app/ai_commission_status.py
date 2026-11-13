"""Stato istruttorio della Commissione per la pagina Verifica requisito ente.

Dati ricavati esclusivamente dalle schede acquisite e dalle graduatorie
RankUP, senza alcuna inferenza dall'AI o dai dati PICA.
La graduatoria provvisoria non costituisce un'ammissione definitiva.
"""
from __future__ import annotations

import json

from graduatorie import build_graduatoria

STATO_GRADUATORIA_FILTRI={"","inserito","non_inserito","da_valutare"}


def describe_commission(candidate:dict|None,definitive:bool=False,
                        pending_sheet:bool=False) -> dict:
    """Restituisce etichette e valori solo quando verificati da RankUP.

    Un candidato unico idoneo e con punteggio ha posizione 1 nel calcolo,
    ma diventa definitivamente 'ammesso' solo dopo formalizzazione del CdS.
    """
    item=candidate or {}
    decision=str(item.get("decisione") or "")
    validated=item.get("valutazione_id") is not None
    base={"stato_graduatoria":"da_valutare",
          "stato_commissione":"Da valutare",
          "posizione":None,"punteggio":None,
          "graduatoria_definitiva":False}
    if not validated:
        if pending_sheet:
            base["stato_commissione"]="In verifica"
        return base
    if decision=="non_idoneo":
        return {**base,"stato_graduatoria":"non_inserito",
                "stato_commissione":"Non idoneo"}
    if decision=="escluso":
        return {**base,"stato_graduatoria":"non_inserito",
                "stato_commissione":"Escluso"}
    if decision!="idoneo":
        return {**base,"stato_commissione":"In verifica"}
    points=item.get("punteggio")
    if points is None:
        return {**base,"stato_commissione":"In verifica"}
    if item.get("verifica_parita"):
        return {**base,"stato_graduatoria":"inserito",
                "stato_commissione":"In graduatoria · parità da risolvere",
                "punteggio":str(points)}
    position=item.get("posizione")
    if not isinstance(position,int) or position<1:
        return {**base,"stato_commissione":"In verifica"}
    return {**base,"stato_graduatoria":"inserito",
            "stato_commissione":("Ammesso in graduatoria" if definitive
                                else "In graduatoria · provvisoria"),
            "posizione":position,"punteggio":str(points),
            "graduatoria_definitiva":bool(definitive)}


def enrich_commission_status(db,bando_id:int,rows:list[dict]) -> None:
    """Arricchisce in sola lettura le righe AI con le decisioni della Commissione.

    Per un CdS definitivo usa i dati del verbale acquisito (snapshot), non
    una nuova classifica ricostruita dai punteggi correnti. Un CdS riaperto
    torna provvisorio. Nessun esito AI puo' alterare lo stato Commissione.
    """
    if not rows:
        return
    pending={r["candidatura_id"] for r in db.execute("""
        SELECT DISTINCT candidatura_id FROM valutazioni_pdf
        WHERE bando_id=? AND candidatura_id IS NOT NULL AND stato='Da verificare'
    """,(bando_id,))}
    definitive={}
    for saved in db.execute("""
        SELECT g.cds,g.sede,g.dati_json FROM graduatorie_cds g
        WHERE g.bando_id=? AND g.stato IN ('definitiva','approvata')
        AND NOT EXISTS(SELECT 1 FROM graduatorie_cds_riaperte x
                       WHERE x.graduatoria_id=g.id)
    """,(bando_id,)):
        try:
            snapshot=json.loads(saved["dati_json"])
            entries={}
            for assignment in snapshot.get("incarichi",[]):
                for item in assignment.get("candidature",[]):
                    entries[int(item["candidatura_id"])]=item
            definitive[(saved["cds"],saved["sede"])]=entries
        except (ValueError,KeyError,TypeError,AttributeError):
            # Snapshot malformato: non dichiarare mai una posizione definitiva.
            continue
    current={}
    for cds,sede in {(r["cds"],r["sede"]) for r in rows}:
        if (cds,sede) in definitive:
            continue
        ranked=build_graduatoria(db,bando_id,cds,sede)
        current[(cds,sede)]={
            int(c["candidatura_id"]):c
            for assignment in ranked["incarichi"]
            for c in assignment["candidature"]
        }
    for row in rows:
        key=(row["cds"],row["sede"])
        official=key in definitive
        source=(definitive if official else current).get(key,{})
        candidate=source.get(int(row["id"]))
        state=describe_commission(candidate,definitive=official,
                                  pending_sheet=int(row["id"]) in pending)
        if official and candidate is None:
            state={**state,"stato_commissione":"Da verificare"}
        row["commissione"]=state
