"""Segnali di possibili duplicati PICA fra bandi, senza decisioni automatiche.

ID PICA condivisi possono avere spiegazioni legittime: la corrispondenza
non e' una prova che i bandi o i candidati siano duplicati.
"""
from __future__ import annotations
from collections import defaultdict


def overlap_alerts(db, parsed_rows: list[dict], code: str) -> list[dict]:
    ids=sorted({str(r.get("id_pica") or "").strip()
                for r in parsed_rows if str(r.get("id_pica") or "").strip()})
    if len(ids)<2:
        return []
    matches=defaultdict(int)
    for index in range(0,len(ids),400):
        subset=ids[index:index+400]
        query="""SELECT b.codice,COUNT(DISTINCT c.id_pica) AS in_comune
                 FROM candidature c JOIN bandi b ON b.id=c.bando_id
                 WHERE b.codice<>? AND c.id_pica IN (""" + ",".join("?" for _ in subset) + """)
                 GROUP BY b.codice"""
        for row in db.execute(query,[code,*subset]):
            matches[row["codice"]]+=int(row["in_comune"])
    return [
        {"codice":other,"id_pica_in_comune":count,"id_pica_del_file":len(ids)}
        for other,count in sorted(matches.items(),key=lambda item:-item[1])
        if count>=2 and count*100>=80*len(ids)
    ][:8]
