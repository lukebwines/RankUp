"""Coda GroqCloud persistente per verifiche ente (schema v9).

Il processo usa un solo worker e un solo invio alla volta. Ogni esito e'
salvato in una transazione propria. Lo stato della coda sopravvive ai
riavvii e non richiede che il browser rimanga aperto.

La quota visualizzata e' una stima locale delle sole richieste della coda
RankUP; altri client/progetti possono consumare quote GroqCloud.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time

from ai_ente_rules import RULES_VERSION, classifica_ente, esito_da_indizio
from ai_groq import (GroqLimit, GroqInvalidResponse, prepare_minimal_payload,
                     DEFAULT_QUOTA, ENT_E_MISSING, ENT_E_PRIVATE)

ACTIVE = ("in_coda", "in_corso", "attesa_quota", "sospesa")
TERMINAL = ("completata", "annullata", "errore")
RATE_DEFAULTS = DEFAULT_QUOTA
MAX_RETRY_SECONDS = 90_000
MAX_INVALID_ATTEMPTS = 3  # primo invio e due tentativi aggiuntivi


def ensure_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS ai_ente_lavori (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            codice TEXT NOT NULL,
            cds TEXT NOT NULL DEFAULT '',
            sede TEXT NOT NULL DEFAULT '',
            modello TEXT NOT NULL,
            modello_attivo TEXT NOT NULL DEFAULT '',
            modello_riserva TEXT NOT NULL DEFAULT '',
            failover_attivo INTEGER NOT NULL DEFAULT 0 CHECK(failover_attivo IN (0,1)),
            limiti_modelli_json TEXT NOT NULL DEFAULT '{}',
            versione_regole TEXT NOT NULL,
            istruzioni_prompt TEXT NOT NULL,
            stato TEXT NOT NULL CHECK(stato IN
                ('in_coda','in_corso','attesa_quota','sospesa',
                 'completata','annullata','errore')),
            forzato INTEGER NOT NULL CHECK(forzato IN (0,1)),
            mirato INTEGER NOT NULL DEFAULT 0 CHECK(mirato IN (0,1)),
            chiamate INTEGER NOT NULL DEFAULT 0,
            token INTEGER NOT NULL DEFAULT 0,
            locali INTEGER NOT NULL DEFAULT 0,
            riutilizzati INTEGER NOT NULL DEFAULT 0,
            prossimo_tentativo REAL,
            ultimo_errore TEXT NOT NULL DEFAULT '',
            quota_provider_json TEXT NOT NULL DEFAULT '{}',
            creato_da TEXT NOT NULL,
            creato_il TEXT NOT NULL,
            aggiornato_il TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_ai_ente_lavori
          ON ai_ente_lavori(bando_id,cds,sede,id);
        CREATE TABLE IF NOT EXISTS ai_ente_ai_esiti (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            candidatura_id INTEGER NOT NULL REFERENCES candidature(id) ON DELETE CASCADE,
            incarico_id INTEGER NOT NULL REFERENCES incarichi(id) ON DELETE CASCADE,
            input_sha256 TEXT NOT NULL,
            modello TEXT NOT NULL,
            versione_regole TEXT NOT NULL,
            indizio TEXT NOT NULL,
            motivo TEXT NOT NULL,
            verifiche TEXT NOT NULL,
            token INTEGER NOT NULL DEFAULT 0,
            lavoro_id INTEGER REFERENCES ai_ente_lavori(id) ON DELETE SET NULL,
            registrato_il TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_ai_ente_ai_esiti_cache
          ON ai_ente_ai_esiti(bando_id,candidatura_id,incarico_id,
                             input_sha256,modello,versione_regole,id);
        CREATE TABLE IF NOT EXISTS ai_ente_lavori_elementi (
            lavoro_id INTEGER NOT NULL REFERENCES ai_ente_lavori(id) ON DELETE CASCADE,
            candidatura_id INTEGER NOT NULL REFERENCES candidature(id) ON DELETE CASCADE,
            incarico_id INTEGER NOT NULL REFERENCES incarichi(id) ON DELETE CASCADE,
            input_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            stato TEXT NOT NULL CHECK(stato IN
              ('in_attesa','completato','riutilizzato','locale')),
            esito_id INTEGER REFERENCES ai_ente_ai_esiti(id) ON DELETE SET NULL,
            PRIMARY KEY(lavoro_id,candidatura_id,incarico_id)
        );
        CREATE INDEX IF NOT EXISTS ix_ai_elementi_pending
          ON ai_ente_lavori_elementi(lavoro_id,stato);
        CREATE TABLE IF NOT EXISTS ai_ente_lavori_anomalie (
            lavoro_id INTEGER NOT NULL REFERENCES ai_ente_lavori(id) ON DELETE CASCADE,
            candidatura_id INTEGER NOT NULL REFERENCES candidature(id) ON DELETE CASCADE,
            incarico_id INTEGER NOT NULL REFERENCES incarichi(id) ON DELETE CASCADE,
            tentativi INTEGER NOT NULL DEFAULT 0,
            ultimo_errore TEXT NOT NULL DEFAULT '',
            esaurito INTEGER NOT NULL DEFAULT 0 CHECK(esaurito IN (0,1)),
            PRIMARY KEY(lavoro_id,candidatura_id,incarico_id)
        );
        CREATE INDEX IF NOT EXISTS ix_ai_anomalie_esaurite
          ON ai_ente_lavori_anomalie(lavoro_id,esaurito);
        CREATE TABLE IF NOT EXISTS ai_ente_quota_eventi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            eseguito_il REAL NOT NULL,
            modello TEXT NOT NULL,
            token INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_ai_quota_recenti
          ON ai_ente_quota_eventi(modello,eseguito_il);
    """)
    # Aggiornamento idempotente dei database esistenti.
    columns={r["name"] for r in db.execute("PRAGMA table_info(ai_ente_lavori)")}
    if "mirato" not in columns:
        db.execute("ALTER TABLE ai_ente_lavori ADD COLUMN mirato INTEGER NOT NULL DEFAULT 0 CHECK(mirato IN (0,1))")
    # La configurazione di fallback e le quote delle code esistenti non cambiano:
    # per gli archivi storici il failover e' sempre disabilitato.
    additions={
        "modello_attivo":"TEXT NOT NULL DEFAULT ''",
        "modello_riserva":"TEXT NOT NULL DEFAULT ''",
        "failover_attivo":"INTEGER NOT NULL DEFAULT 0 CHECK(failover_attivo IN (0,1))",
        "limiti_modelli_json":"TEXT NOT NULL DEFAULT '{}'",
    }
    for name,sql_type in additions.items():
        if name not in columns:
            db.execute("ALTER TABLE ai_ente_lavori ADD COLUMN "+name+" "+sql_type)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


LEGACY_RULES_VERSION = "ente-denominazione-originale-v4"


def _hash(payload:dict, model:str, guidance:str,
          version:str=RULES_VERSION) -> str:
    # Cambio regole = invalidazione della cache, senza perdere lo storico.
    blob=json.dumps({"data":payload,"model":model,"rules":version,
                     "guidance":guidance},
                    sort_keys=True,ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _find_cached(db, bid:int, digest:str, model:str,
                 lavoro_id:int|None=None):
    """Cache globale: mai propagare gli esiti di una revisione MIRATA.

    Una revisione mirata vale solo per le candidature presenti nel lavoro;
    i riusi validi di lavori ordinari o forzati su intero ambito restano
    disponibili per tutti i payload identici.
    """
    sql="""SELECT a.id,a.indizio,a.motivo,a.verifiche,a.token,a.registrato_il
           FROM ai_ente_ai_esiti a
           LEFT JOIN ai_ente_lavori j ON j.id=a.lavoro_id
           WHERE a.bando_id=? AND a.input_sha256=? AND a.modello=?
             AND a.versione_regole=?"""
    params=[bid,digest,model,RULES_VERSION]
    if lavoro_id is None:
        sql+=" AND (j.id IS NULL OR j.mirato=0)"
    else:
        sql+=" AND a.lavoro_id=?"
        params.append(lavoro_id)
    return db.execute(sql+" ORDER BY a.id DESC LIMIT 1",params).fetchone()


def _find_cached_for_candidate(db,bid:int,digest:str,model:str,
                               candidate_id:int,incarico_id:int,
                               version:str=RULES_VERSION):
    """Esito attuale per candidato: priorita' alle revisioni mirate proprie.

    L'esito di una revisione mirata non altera i candidati estranei, anche
    quando l'ente e il requisito dichiarati coincidono esattamente.
    """
    return db.execute("""
        SELECT a.id,a.indizio,a.motivo,a.verifiche,a.token,a.registrato_il
        FROM ai_ente_ai_esiti a
        LEFT JOIN ai_ente_lavori j ON j.id=a.lavoro_id
        WHERE a.bando_id=? AND a.input_sha256=? AND a.modello=?
          AND a.versione_regole=?
          AND (
            j.id IS NULL OR j.mirato=0 OR
            (a.candidatura_id=? AND a.incarico_id=?) OR
            EXISTS(SELECT 1 FROM ai_ente_lavori_elementi e
                   WHERE e.lavoro_id=j.id
                     AND e.candidatura_id=? AND e.incarico_id=?
                     AND e.esito_id=a.id)
          )
        ORDER BY a.id DESC LIMIT 1
    """,(bid,digest,model,version,candidate_id,incarico_id,
         candidate_id,incarico_id)).fetchone()

def classifications(candidates: list[dict]) -> list[dict]:
    """Gli esiti istituzionali non dipendono dalla risposta del modello AI."""
    return [dict(classifica_ente(c),
                 campi_pica_originali={
                     "Azienda/ente":str(c.get("azienda_ente") or ""),
                     "Ateneo":str(c.get("ateneo") or ""),
                     "Occupazione":str(c.get("occupazione") or ""),
                     "Profilo professionale":str(c.get("profilo_professionale") or ""),
                     "Qualifica":str(c.get("qualifica") or ""),
                 },
                 id=c["id"],id_pica=str(c["id_pica"]),
                 cognome=str(c.get("cognome") or "").strip(),
                 nome=str(c.get("nome") or "").strip(),
                 incarico_key=str(c["incarico_key"]),
                 cds=c["cds"],sede=c["sede"],insegnamento=c["insegnamento"],
                 stato_candidatura=c["stato"])
            for c in candidates]


def cases_with_ai(db,bid:int,candidates:list[dict],model:str,guidance:str) -> list[dict]:
    out=[]
    anomalies={}
    for r in db.execute("""
        SELECT a.candidatura_id,a.incarico_id,a.ultimo_errore
        FROM ai_ente_lavori_anomalie a JOIN ai_ente_lavori j
          ON j.id=a.lavoro_id
        WHERE j.bando_id=? AND j.modello=? AND j.versione_regole=?
          AND j.istruzioni_prompt=? AND a.esaurito=1
        ORDER BY j.id ASC
    """,(bid,model,RULES_VERSION,guidance)):
        anomalies[(r["candidatura_id"],r["incarico_id"])]=r["ultimo_errore"]
    for entry in classifications(candidates):
        row=db.execute("""SELECT id FROM incarichi
            WHERE bando_id=? AND key=?""",(bid,entry["incarico_key"])).fetchone()
        if not row:continue
        payload=entry["payload"]
        digest=_hash(payload,model,guidance)
        cached=_find_cached_for_candidate(db,bid,digest,model,entry["id"],row["id"])
        historical=False
        # Mostra lo storico v1.0.15 senza attribuirgli l'analisi dei campi
        # BD/BM/BA/BO. I nuovi lavori utilizzano sempre il payload completo.
        if not cached and entry["richiede_ai"]:
            bb=payload["denominazione_ente_dichiarato"]
            if bb not in (ENT_E_MISSING,ENT_E_PRIVATE):
                original={"requisito_ente":payload["requisito_ente"],
                          "denominazione_ente_dichiarato":bb}
                previous=_hash(original,model,guidance,LEGACY_RULES_VERSION)
                cached=_find_cached_for_candidate(db,bid,previous,model,
                    entry["id"],row["id"],LEGACY_RULES_VERSION)
                historical=cached is not None
        # Recupera anche le analisi effettuate con il modello di riserva
        # della stessa coda, evitando esiti di modelli configurati altrove.
        if not cached and entry["richiede_ai"]:
            fallbacks=db.execute("""
                SELECT DISTINCT j.modello_riserva
                FROM ai_ente_lavori j JOIN ai_ente_lavori_elementi e
                  ON e.lavoro_id=j.id
                WHERE j.bando_id=? AND j.modello=? AND j.versione_regole=?
                  AND j.istruzioni_prompt=? AND j.failover_attivo=1
                  AND e.candidatura_id=? AND e.incarico_id=?
                  AND j.modello_riserva<>''
                ORDER BY j.id DESC
            """,(bid,model,RULES_VERSION,guidance,entry["id"],row["id"])).fetchall()
            for fallback in fallbacks:
                alternate=fallback["modello_riserva"]
                alternate_digest=_hash(payload,alternate,guidance)
                cached=_find_cached_for_candidate(db,bid,alternate_digest,
                    alternate,entry["id"],row["id"])
                if cached:
                    entry["modello_ai"]=alternate
                    break
        if cached and not entry.get("modello_ai"):
            entry["modello_ai"]=model
        entry["ai"]=dict(cached) if cached else None
        if entry["ai"] is not None:
            entry["ai"]["modello"]=entry.get("modello_ai",model)
        if historical:
            entry["valutazione_storica"]=True
        technical=anomalies.get((entry["id"],row["id"]))
        if entry["richiede_ai"] and not cached and technical:
            entry["motivo"]="Errore tecnico AI: nessuna conclusione registrata."
            entry["verifiche"]="Ritentare soltanto questo caso dalla coda AI. "+technical
            entry["errore_tecnico"]=True
            entry["tipo_approfondimento"]="ERRORE TECNICO AI"
        if entry["richiede_ai"] and cached:
            entry["esito"]=esito_da_indizio(cached["indizio"])
            entry["tipo_approfondimento"]=(
                "VALUTAZIONE STORICA (SOLO AZIENDA/ENTE)"
                if historical else cached["indizio"])
            entry["motivo"]=cached["motivo"]
            entry["verifiche"]=(cached["verifiche"]+
                (" Esito storico: Ateneo, Occupazione, Profilo e Qualifica "
                 "NON esaminati; rianalisi completa consigliata."
                 if historical else
                 " Il rapporto di lavoro personale resta da accertare."))
        # Dati PICA per la sola UI Admin: i campi filtrati sono indicati
        # separatamente dai valori originali; nulla di cio' viene inviato
        # ad altri servizi da questa API.
        entry["campi_pica_ai"]={
            "Azienda/ente":payload.get("denominazione_ente_dichiarato"),
            "Ateneo":payload.get("ateneo_dichiarato"),
            "Occupazione":payload.get("occupazione_dichiarata"),
            "Profilo professionale":payload.get("profilo_professionale_dichiarato"),
            "Qualifica":payload.get("qualifica_dichiarata"),
        }
        entry.pop("payload",None)
        out.append(entry)
    return out


def create_job(db,bid:int,code:str,cds:str,sede:str,model:str,
               candidates:list[dict],who:str,guidance:str,force:bool=False,
               targeted:bool=False, fallback_model:str="",
               fallback_enabled:bool=False,
               limits_by_model:dict|None=None) -> dict:
    now=_now()
    db.execute("BEGIN IMMEDIATE")
    # Una coda della versione precedente contiene payload a soli due campi:
    # non e' piu' eseguibile con la nuova richiesta AI a sei chiavi.
    db.execute("""UPDATE ai_ente_lavori SET stato='errore',
           ultimo_errore='Coda obsoleta: avviare una nuova analisi con i campi PICA aggiornati',
           aggiornato_il=?
           WHERE bando_id=? AND versione_regole<>?
             AND stato IN ('in_coda','in_corso','attesa_quota','sospesa')
    """,(now,bid,RULES_VERSION))
    prior=db.execute("""
        SELECT id,stato FROM ai_ente_lavori
        WHERE bando_id=? AND cds=? AND sede=? AND stato IN
          ('in_coda','in_corso','attesa_quota','sospesa')
        ORDER BY id DESC LIMIT 1
    """,(bid,cds,sede)).fetchone()
    if prior:
        db.commit()
        return {"id":prior["id"],"stato":prior["stato"],"gia_esistente":True}
    reserve=fallback_model if fallback_enabled and fallback_model!=model else ""
    if reserve and (not limits_by_model or reserve not in limits_by_model):
        raise ValueError("Manca il profilo di quota del modello di riserva")
    frozen=json.dumps(limits_by_model or {},sort_keys=True)
    cur=db.execute("""
        INSERT INTO ai_ente_lavori
          (bando_id,codice,cds,sede,modello,modello_attivo,modello_riserva,
           failover_attivo,limiti_modelli_json,versione_regole,istruzioni_prompt,
           stato,forzato,mirato,creato_da,creato_il,aggiornato_il)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'in_coda',?,?,?,?,?)
    """,(bid,code,cds,sede,model,model,reserve,int(bool(reserve)),
         frozen,RULES_VERSION,guidance,int(force),int(targeted),who,now,now))
    jobid=cur.lastrowid
    nlocal,ncached=0,0
    for candidate,entry in zip(candidates,classifications(candidates)):
        inc=db.execute("""SELECT id FROM incarichi WHERE bando_id=? AND key=?""",
                       (bid,entry["incarico_key"])).fetchone()
        if not inc:continue
        payload=entry["payload"]
        digest=_hash(payload,model,guidance)
        cached=None if force else _find_cached(db,bid,digest,model)
        if not cached and not force and reserve:
            cached=_find_cached(db,bid,_hash(payload,reserve,guidance),reserve)
        if not entry["richiede_ai"]:
            status="locale"
            nlocal+=1
        elif cached:
            status="riutilizzato"
            ncached+=1
        else:
            status="in_attesa"
        db.execute("""
            INSERT INTO ai_ente_lavori_elementi
            (lavoro_id,candidatura_id,incarico_id,input_sha256,
             payload_json,stato,esito_id) VALUES(?,?,?,?,?,?,?)
        """,(jobid,candidate["id"],inc["id"],digest,
             json.dumps(payload,ensure_ascii=False,sort_keys=True),status,
             cached["id"] if cached else None))
    db.execute("""UPDATE ai_ente_lavori
        SET locali=?,riutilizzati=? WHERE id=?""",(nlocal,ncached,jobid))
    db.commit()
    return {"id":jobid,"stato":"in_coda","gia_esistente":False}


def _set_status(db,jobid:int,status:str,**changes):
    attrs={"stato":status,"aggiornato_il":_now(),**changes}
    db.execute("UPDATE ai_ente_lavori SET "+
               ",".join(k+"=?" for k in attrs)+" WHERE id=?",
               (*attrs.values(),jobid))


def progress(db,job):
    if not job:return None
    row=dict(job)
    # Il prompt integrale e' disponibile solo nella pagina delle impostazioni,
    # non deve essere ripetuto in ogni chiamata di avanzamento.
    row.pop("istruzioni_prompt",None)
    row.pop("limiti_modelli_json",None)
    try:
        row["quota_provider"]=json.loads(row.pop("quota_provider_json") or "{}")
    except (TypeError,ValueError):
        row["quota_provider"]={}
    by_status={r["stato"]:int(r["tot"]) for r in db.execute("""
        SELECT stato,COUNT(*) tot FROM ai_ente_lavori_elementi
        WHERE lavoro_id=? GROUP BY stato""",(row["id"],)).fetchall()}
    total=sum(by_status.values())
    done=total-by_status.get("in_attesa",0)
    retry=row.get("prossimo_tentativo")
    technical=db.execute("""
        SELECT COUNT(*) FROM ai_ente_lavori_anomalie
        WHERE lavoro_id=? AND esaurito=1
    """,(row["id"],)).fetchone()[0]
    row.update({"errori_tecnici":technical,
        "totale":total,"completati":done,
        "da_esaminare":by_status.get("in_attesa",0),
        "riutilizzati":by_status.get("riutilizzato",0),
        "locali":by_status.get("locale",0),
        "riprende_tra":max(0,int(retry-time.time())) if retry else None})
    return row


def last_job(db,bid:int,cds:str,sede:str):
    row=db.execute("""SELECT * FROM ai_ente_lavori
        WHERE bando_id=? AND cds=? AND sede=?
        ORDER BY id DESC LIMIT 1""",(bid,cds,sede)).fetchone()
    return progress(db,row)


def job_action(db,jobid:int,action:str):
    db.execute("BEGIN IMMEDIATE")
    row=db.execute("SELECT stato FROM ai_ente_lavori WHERE id=?",
                   (jobid,)).fetchone()
    if not row:
        raise ValueError("Coda non trovata")
    status=row["stato"]
    if action=="sospendi" and status in ("in_coda","in_corso","attesa_quota"):
        _set_status(db,jobid,"sospesa",prossimo_tentativo=None)
    elif action=="annulla" and status in ACTIVE:
        _set_status(db,jobid,"annullata",prossimo_tentativo=None)
    elif action=="riprova_errori" and status in ("completata","errore"):
        exhausted=db.execute("""SELECT candidatura_id,incarico_id
            FROM ai_ente_lavori_anomalie
            WHERE lavoro_id=? AND esaurito=1""",(jobid,)).fetchall()
        if not exhausted:
            db.rollback()
            raise ValueError("Nessun caso tecnico da riprovare")
        for item in exhausted:
            db.execute("""UPDATE ai_ente_lavori_elementi
                SET stato='in_attesa',esito_id=NULL
                WHERE lavoro_id=? AND candidatura_id=? AND incarico_id=?
                  AND esito_id IS NULL""",
                (jobid,item["candidatura_id"],item["incarico_id"]))
        db.execute("""UPDATE ai_ente_lavori_anomalie
            SET tentativi=0,esaurito=0,ultimo_errore=''
            WHERE lavoro_id=? AND esaurito=1""",(jobid,))
        _set_status(db,jobid,"in_coda",prossimo_tentativo=None,ultimo_errore="")
    elif action=="riprendi" and status in ("sospesa","errore","attesa_quota"):
        # Gli elementi gia' completati o riutilizzati rimangono acquisiti.
        _set_status(db,jobid,"in_coda",prossimo_tentativo=None,ultimo_errore="")
    else:
        db.rollback()
        raise ValueError("Transizione di stato non consentita")
    db.commit()
    return progress(db,db.execute("SELECT * FROM ai_ente_lavori WHERE id=?",
                                  (jobid,)).fetchone())


def _quota_pause(db,model:str,limits:dict,now:float):
    """Rolling windows prudenziali e conservate nel database."""
    use=dict(RATE_DEFAULTS)
    use.update({k:int(v) for k,v in limits.items() if k in use})
    factor=max(50,min(95,use["margine"]))/100
    minute=db.execute("""SELECT eseguito_il,token FROM ai_ente_quota_eventi
        WHERE modello=? AND eseguito_il>? ORDER BY eseguito_il ASC
    """,(model,now-60)).fetchall()
    day=db.execute("""SELECT eseguito_il,token FROM ai_ente_quota_eventi
        WHERE modello=? AND eseguito_il>? ORDER BY eseguito_il ASC
    """,(model,now-86400)).fetchall()
    # Non sommiamo le quote di modelli diversi. Rimane una stima locale,
    # non la quota globale dell'organizzazione GroqCloud.
    estimate=use["stima_token"]
    thresholds={
        "rpm":max(1,int(use["rpm"]*factor)),
        "tpm":max(estimate,int(use["tpm"]*factor)),
        "rpd":max(1,int(use["rpd"]*factor)),
        "tpd":max(estimate,int(use["tpd"]*factor)),
    }
    pauses=[]
    if len(minute)>=thresholds["rpm"]:
        pauses.append(minute[0]["eseguito_il"]+62)
    if sum(r["token"] for r in minute)+estimate>thresholds["tpm"] and minute:
        pauses.append(minute[0]["eseguito_il"]+62)
    if len(day)>=thresholds["rpd"]:
        pauses.append(day[0]["eseguito_il"]+86402)
    if sum(r["token"] for r in day)+estimate>thresholds["tpd"] and day:
        pauses.append(day[0]["eseguito_il"]+86402)
    return (min(pauses) if pauses else None),{
        "richieste_60s":len(minute),"token_60s":sum(r["token"] for r in minute),
        "richieste_24h":len(day),"token_24h":sum(r["token"] for r in day),
        "limiti_effettivi":thresholds,
        "fonte":"stima locale delle richieste della coda RankUP",
    }


def quota_info(db,model:str,limits:dict) -> dict:
    _,stats=_quota_pause(db,model,limits,time.time())
    return stats


def process_one(connect,load_cases,send,get_key,limits:dict) -> bool:
    """Un solo invio per chiamata. Non usa dati PICA completi fuori dal server.

    load_cases(code,cds,sede) ricostruisce il PICA attuale per verificare
    eventuali cambiamenti dal momento dell'accodamento.
    """
    now=time.time()
    with connect() as db:
        # Rivendicazione atomica: un solo worker puo' estrarre una richiesta.
        db.execute("BEGIN IMMEDIATE")
        db.execute("""UPDATE ai_ente_lavori
            SET stato='in_coda',prossimo_tentativo=NULL,aggiornato_il=?
            WHERE stato='attesa_quota' AND prossimo_tentativo<=?
        """,(_now(),now))
        job=db.execute("""SELECT * FROM ai_ente_lavori
            WHERE stato='in_coda' ORDER BY id LIMIT 1""").fetchone()
        if not job:
            db.commit()
            return False
        job=dict(job)
        if job["versione_regole"]!=RULES_VERSION:
            _set_status(db,job["id"],"errore",
                        ultimo_errore="Coda creata con versione precedente: "
                                     "avviare una nuova analisi PICA completa")
            db.commit()
            return True
        pending=db.execute("""SELECT * FROM ai_ente_lavori_elementi
            WHERE lavoro_id=? AND stato='in_attesa'
            ORDER BY candidatura_id,incarico_id LIMIT 1
        """,(job["id"],)).fetchone()
        if not pending:
            _set_status(db,job["id"],"completata",prossimo_tentativo=None)
            db.commit()
            return True
        pending=dict(pending)
        cached=_find_cached(db,job["bando_id"],pending["input_sha256"],
                            job["modello"],job["id"] if job["forzato"] else None)
        if cached:
            # Evitare richieste duplicate anche per categorie identiche
            # incontrate DOPO l'avvio della coda, inclusa la rianalisi forzata.
            db.execute("""UPDATE ai_ente_lavori_elementi
                SET stato='riutilizzato',esito_id=?
                WHERE lavoro_id=? AND candidatura_id=? AND incarico_id=?""",
                (cached["id"],job["id"],pending["candidatura_id"],pending["incarico_id"]))
            db.execute("""UPDATE ai_ente_lavori SET
                riutilizzati=riutilizzati+1,aggiornato_il=? WHERE id=?""",
                (_now(),job["id"]))
            db.commit()
            return True
        # Lo stesso ente/requisito non deve generare altri tre tentativi
        # dopo un fallimento tecnico definitivo della medesima richiesta.
        # Non si tratta di una valutazione riutilizzabile: resta DA APPROFONDIRE.
        failed=db.execute("""
            SELECT a.ultimo_errore FROM ai_ente_lavori_anomalie a
            JOIN ai_ente_lavori_elementi e
              ON e.lavoro_id=a.lavoro_id
             AND e.candidatura_id=a.candidatura_id
             AND e.incarico_id=a.incarico_id
            WHERE a.lavoro_id=? AND e.input_sha256=? AND a.esaurito=1
            LIMIT 1
        """,(job["id"],pending["input_sha256"])).fetchone()
        if failed:
            db.execute("""INSERT INTO ai_ente_lavori_anomalie
                (lavoro_id,candidatura_id,incarico_id,tentativi,ultimo_errore,esaurito)
                VALUES(?,?,?,?,?,1)""",
                (job["id"],pending["candidatura_id"],pending["incarico_id"],
                 0,failed["ultimo_errore"]))
            db.execute("""UPDATE ai_ente_lavori_elementi
                SET stato='completato',esito_id=NULL
                WHERE lavoro_id=? AND candidatura_id=? AND incarico_id=?""",
                (job["id"],pending["candidatura_id"],pending["incarico_id"]))
            db.commit()
            return True
        pause,stat=_quota_pause(db,job["modello"],limits,now)
        if pause:
            _set_status(db,job["id"],"attesa_quota",prossimo_tentativo=pause,
                        ultimo_errore="Pausa preventiva per limiti locali di richieste/token")
            db.commit()
            return True
        _set_status(db,job["id"],"in_corso",ultimo_errore="")
        db.commit()
    # La rete deve operare fuori dalla transazione SQLite.
    try:
        current=load_cases(job["codice"],job["cds"],job["sede"])
        eligible=next((c for c in current if int(c["id"])==pending["candidatura_id"]
                      and c["stato"]=="Attiva"),None)
        expected=json.loads(pending["payload_json"])
        if not eligible or _hash(prepare_minimal_payload(eligible),job["modello"],job["istruzioni_prompt"])!=pending["input_sha256"]:
            raise ValueError("Dati PICA modificati: annullare la coda e avviare una nuova analisi")
        result=send(get_key(),job["modello"],expected,guidance=job["istruzioni_prompt"])
    except GroqLimit as exc:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state=db.execute("SELECT stato FROM ai_ente_lavori WHERE id=?",
                             (job["id"],)).fetchone()
            if state and state["stato"]=="in_corso":
                db.execute("""INSERT INTO ai_ente_quota_eventi
                              (eseguito_il,modello,token) VALUES(?,?,0)""",
                           (time.time(),job["modello"]))
                _set_status(db,job["id"],"attesa_quota",
                    prossimo_tentativo=time.time()+max(10,min(exc.retry_after,MAX_RETRY_SECONDS)),
                    ultimo_errore="Limite GroqCloud HTTP 429: ripresa automatica")
            db.commit()
        return True
    except GroqInvalidResponse as exc:
        # Una risposta non conforme non e' un esito AI.
        # Sono consentiti 2 retry, poi il singolo caso resta da approfondire.
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state=db.execute("SELECT stato FROM ai_ente_lavori WHERE id=?",
                             (job["id"],)).fetchone()
            if state and state["stato"]=="in_corso":
                db.execute("""INSERT INTO ai_ente_lavori_anomalie
                    (lavoro_id,candidatura_id,incarico_id,tentativi,ultimo_errore)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(lavoro_id,candidatura_id,incarico_id)
                    DO UPDATE SET tentativi=tentativi+1,
                                  ultimo_errore=excluded.ultimo_errore
                """,(job["id"],pending["candidatura_id"],pending["incarico_id"],
                     1,str(exc)[:200]))
                attempts=db.execute("""SELECT tentativi
                    FROM ai_ente_lavori_anomalie WHERE lavoro_id=?
                    AND candidatura_id=? AND incarico_id=?""",
                    (job["id"],pending["candidatura_id"],
                     pending["incarico_id"])).fetchone()[0]
                db.execute("""INSERT INTO ai_ente_quota_eventi
                    (eseguito_il,modello,token) VALUES(?,?,?)""",
                    (time.time(),job["modello"],exc.tokens))
                rate=exc.rate if isinstance(exc.rate,dict) else {}
                safe_rate={k:str(v)[:80] for k,v in rate.items()
                           if k.startswith("x-ratelimit-")
                           and isinstance(v,(str,int,float))}
                db.execute("""UPDATE ai_ente_lavori SET
                    chiamate=chiamate+1,token=token+?,
                    quota_provider_json=CASE WHEN ?!='{}' THEN ?
                                             ELSE quota_provider_json END
                    WHERE id=?""",(exc.tokens,json.dumps(safe_rate),
                                  json.dumps(safe_rate),job["id"]))
                if attempts>=MAX_INVALID_ATTEMPTS:
                    db.execute("""UPDATE ai_ente_lavori_anomalie
                        SET esaurito=1 WHERE lavoro_id=? AND candidatura_id=?
                        AND incarico_id=?""",
                        (job["id"],pending["candidatura_id"],pending["incarico_id"]))
                    db.execute("""UPDATE ai_ente_lavori_elementi
                        SET stato='completato',esito_id=NULL
                        WHERE lavoro_id=? AND candidatura_id=? AND incarico_id=?""",
                        (job["id"],pending["candidatura_id"],pending["incarico_id"]))
                    _set_status(db,job["id"],"in_coda",
                        ultimo_errore="Un caso senza esito AI dopo 3 tentativi: "
                                     "segnalato e coda proseguita.")
                else:
                    _set_status(db,job["id"],"in_coda",
                        ultimo_errore="Risposta AI non valida: nuovo tentativo "+
                                     str(attempts+1)+" di 3.")
            db.commit()
        return True
    except (ValueError,OSError,TypeError,KeyError) as exc:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state=db.execute("SELECT stato FROM ai_ente_lavori WHERE id=?",
                             (job["id"],)).fetchone()
            if state and state["stato"]=="in_corso":
                _set_status(db,job["id"],"errore",
                            ultimo_errore=str(exc)[:250],prossimo_tentativo=None)
            db.commit()
        return True
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT stato FROM ai_ente_lavori WHERE id=?",
                       (job["id"],)).fetchone()
        if not row:return True
        if row["stato"] not in (*ACTIVE,"annullata"):
            db.commit()
            return True
        token=max(0,int(result.get("token_usati") or 0))
        origin=result.get("origine")
        if origin=="groqcloud":
            db.execute("""INSERT INTO ai_ente_quota_eventi
                (eseguito_il,modello,token) VALUES(?,?,?)""",
                       (time.time(),job["modello"],token))
        cur=db.execute("""
            INSERT INTO ai_ente_ai_esiti
            (bando_id,candidatura_id,incarico_id,input_sha256,modello,
             versione_regole,indizio,motivo,verifiche,token,lavoro_id,registrato_il)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,(job["bando_id"],pending["candidatura_id"],pending["incarico_id"],
             pending["input_sha256"],job["modello"],RULES_VERSION,
             str(result["indizio"])[:120],str(result["motivo"])[:440],
             str(result["verifiche"])[:440],token,job["id"],_now()))
        db.execute("""UPDATE ai_ente_lavori_elementi
            SET stato='completato',esito_id=?
            WHERE lavoro_id=? AND candidatura_id=? AND incarico_id=?""",
            (cur.lastrowid,job["id"],pending["candidatura_id"],pending["incarico_id"]))
        rate=result.get("quota_provider") or {}
        if not isinstance(rate,dict):rate={}
        safe_rate={k:str(v)[:80] for k,v in rate.items()
                   if k.startswith("x-ratelimit-") and isinstance(v,(str,int,float))}
        db.execute("""UPDATE ai_ente_lavori SET
            chiamate=chiamate+?,token=token+?,
            quota_provider_json=CASE WHEN ?!='{}' THEN ?
                                     ELSE quota_provider_json END,
            aggiornato_il=? WHERE id=?""",
            (1 if origin=="groqcloud" else 0,token,
             json.dumps(safe_rate),json.dumps(safe_rate),_now(),job["id"]))
        # Libera il lavoro per il prossimo elemento, solo se non sospeso
        # o annullato dall'Admin durante la chiamata di rete.
        if row["stato"]=="in_corso":
            _set_status(db,job["id"],"in_coda")
        db.commit()
    return True
