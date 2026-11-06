"""Controllo ISTRUTTORIO dell'appartenenza all'ente (non determina ammissione).

La valutazione è conservativa: non usa servizi cloud e non deduce lo stato di
dipendente da nomi generici di strutture o dal solo profilo professionale.
"""
from __future__ import annotations

import re
import unicodedata


def norm(value: str | None) -> str:
    raw=unicodedata.normalize("NFKD",str(value or ""))
    return re.sub(r"\s+"," ","".join(ch for ch in raw if not unicodedata.combining(ch)).upper()).strip()


def evaluate_ente(requisito: str | None, occupazione: str | None,
                  azienda: str | None, profilo: str | None) -> dict:
    req=norm(requisito)
    occ=norm(occupazione)
    company=norm(azienda)
    role=norm(profilo)
    if not req:
        return {"stato":"DA CONFIGURARE","motivo":"Requisito ente non ancora verificato sul bando."}
    if req=="NESSUNA RISERVA":
        return {"stato":"NESSUNA RISERVA","motivo":"L'incarico non ha una riserva di ente confermata."}
    if not company and not occ:
        return {"stato":"DA VERIFICARE","motivo":"Dichiarazione occupazionale o ente non presente nell'esportazione PICA."}

    # Il nome dell'ente è solo un'indicazione, non una prova di un rapporto
    # attivo. AOUI e ASUIT NON sono equivalenti automaticamente.
    is_aoui=any(x in company for x in (
        "AZIENDA OSPEDALIERA UNIVERSITARIA INTEGRATA DI VERONA",
        "AZIENDA OSPEDALIERA UNIVERSITARIA INTEGRATA VERONA",
        "AOUI VERONA",
    ))
    is_asuit=any(x in company for x in (
        "AZIENDA SANITARIA UNIVERSITARIA INTEGRATA DEL TRENTINO",
        "ASUIT",
    ))
    is_unitn=any(x in company for x in (
        "UNIVERSITA DEGLI STUDI DI TRENTO","UNIVERSITA DI TRENTO","UNITN",
    ))
    qualification=any(x in (" "+occ+" "+role+" ") for x in (
        " PROFESSORE"," PROFESSORESSA"," RICERCATORE"," RICERCATRICE",
    ))
    eligible=(
        (req=="AOUI" and is_aoui)
        or (req=="ASUIT" and is_asuit)
        or (req=="UNITN" and is_unitn and qualification)
        or (req=="ASUIT O UNITN" and (is_asuit or (is_unitn and qualification)))
    )
    if eligible:
        return {"stato":"COERENTE (PRELIMINARE)",
                "motivo":"Denominazione ente e, se richiesta, qualifica compatibili; verificare rapporto attivo alla scadenza."}
    if req=="SSN":
        if any(x in company for x in ("AZIENDA ULSS","AZIENDA USL","ASL ","AZIENDA SANITARIA",
                                       "AZIENDA OSPEDALIERA","OSPEDALE","ASUIT","AOUI")):
            return {"stato":"DA VERIFICARE",
                    "motivo":"Struttura potenzialmente SSN: verificare impiego attivo e, se privata, convenzionamento."}
    if req=="SSP" and any(x in company for x in ("AZIENDA SANITARIA DELL'ALTO ADIGE","SABES",
                                                    "SUDTIROLER SANITATSBETRIEB")):
        return {"stato":"DA VERIFICARE",
                "motivo":"Ente potenzialmente SSP: verificare rapporto di lavoro e requisiti alla scadenza."}
    return {"stato":"DA VERIFICARE",
            "motivo":"Non è possibile confermare la corrispondenza con l'ente richiesto dai soli dati PICA."}
