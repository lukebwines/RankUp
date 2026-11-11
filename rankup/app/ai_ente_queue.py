"""Coda GroqCloud persistente per verifiche ente (schema v7).

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

from ai_ente_rules import RULES_VERSION, classifica_ente
from ai_groq import GroqLimit, prepare_minimal_payload

ACTIVE = ("in_coda", "in_corso", "attesa_quota", "sospesa")
TERMINAL = ("completata", "annullata", "errore")
RATE_DEFAULTS = {"rpm":30,"tpm":8000,"rpd":1000,"tpd":200000,
                 "margine":80,"stima_token":950}
MAX_RETRY_SECONDS = 90_000


def ensure_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS ai_ente_lavori (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            codice TEXT NOT NULL,
            cds TEXT NOT NULL DEFAULT '',
            sede TEXT NOT NULL DEFAULT '',
            modello TEXT NOT NULL,
            versione_regole TEXT NOT NULL,
            stato TEXT NOT NULL CHECK(stato IN
                ('in_coda','in_corso','attesa_quota','sospesa',
                 'completata','annullata','errore')),
            forzato INTEGER NOT NULL CHECK(forzato IN (0,1)),
            chiamate INTEGER NOT NULL DEFAULT 0,
            token INTEGER NOT NULL DEFAULT 0,
            locali INTEGER NOT NULL DEFAULT 0,
            riutilizzati INTEGER NOT NULL DEFAULT 0,
            prossimo_tentativo REAL,
            ultimo_errore TEXT NOT NULL DEFAULT '',
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
        CREATE TABLE IF NOT EXISTS ai_ente_quota_eventi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            eseguito_il REAL NOT NULL,
            modello TEXT NOT NULL,
            token INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_ai_quota_recenti
          ON ai_ente_quota_eventi(modello,eseguito_il);
    """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(payload:dict, model:str) -> str:
    # Cambio regole = invalidazione della cache, senza perdere lo storico.
    blob=json.dumps({"data":payload,"model":model,"rules":RULES_VERSION},
                    sort_keys=True,ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _find_cached(db, bid:int, cid:int, iid:int, digest:str, model:str):
    return db.execute("""
        SELECT id,indizio,motivo,verifiche,token,registrato_il FROM ai_ente_ai_esiti
        WHERE bando_id=? AND candidatura_id=? AND incarico_id=?
          AND input_sha256=? AND modello=? AND versione_regole=?
        ORDER BY id DESC LIMIT 1
    """,(bid,cid,iid,digest,model,RULES_VERSION)).fetchone()


def classifications(candidates: list[dict]) -> list[dict]:
    """Gli esiti istituzionali non dipendono dalla risposta del modello AI."""
    return [dict(classifica_ente(c),
                 id=c["id"],id_pica=str(c["id_pica"]),
                 incarico_key=str(c["incarico_key"]),
                 cds=c["cds"],sede=c["sede"],insegnamento=c["insegnamento"],
                 stato_candidatura=c["stato"])
            for c in candidates]


def cases_with_ai(db,bid:int,candidates:list[dict],model:str) -> list[dict]:
    out=[]
    for entry in classifications(candidates):
        candidate=next(c for c in candidates
                       if c["id"]==entry["id"] and
                       c["incarico_key"]==entry["incarico_key"])
        row=db.execute("""SELECT id FROM incarichi
            WHERE bando_id=? AND key=?""",(bid,entry["incarico_key"])).fetchone()
        if not row:continue
        digest=_hash(entry["payload"],model)
        cached=_find_cached(db,bid,int(entry["id"]),int(row["id"]),digest,model)
        entry["ai"]=dict(cached) if cached else None
        # La categoria resta deterministica anche quando un'AI precedente
        # aveva interpretato male AOUI/ASUIT come incompatibili con SSN.
        entry.pop("payload",None)
        out.append(entry)
    return out


def create_job(db,bid:int,code:str,cds:str,sede:str,model:str,
               candidates:list[dict],who:str,force:bool=False) -> dict:
    now=_now()
    db.execute("BEGIN IMMEDIATE")
    prior=db.execute("""
        SELECT id,stato FROM ai_ente_lavori
        WHERE bando_id=? AND cds=? AND sede=? AND stato IN
          ('in_coda','in_corso','attesa_quota','sospesa')
        ORDER BY id DESC LIMIT 1
    """,(bid,cds,sede)).fetchone()
    if prior:
        db.commit()
        return {"id":prior["id"],"stato":prior["stato"],"gia_esistente":True}
    cur=db.execute("""
        INSERT INTO ai_ente_lavori
          (bando_id,codice,cds,sede,modello,versione_regole,
           stato,forzato,creato_da,creato_il,aggiornato_il)
        VALUES(?,?,?,?,?,?,'in_coda',?,?,?,?)
    """,(bid,code,cds,sede,model,RULES_VERSION,int(force),who,now,now))
    jobid=cur.lastrowid
    nlocal,ncached=0,0
    for candidate,entry in zip(candidates,classifications(candidates)):
        inc=db.execute("""SELECT id FROM incarichi WHERE bando_id=? AND key=?""",
                       (bid,entry["incarico_key"])).fetchone()
        if not inc:continue
        payload=entry["payload"]
        digest=_hash(payload,model)
        cached=None if force else _find_cached(
            db,bid,int(candidate["id"]),int(inc["id"]),digest,model)
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
    by_status={r["stato"]:int(r["tot"]) for r in db.execute("""
        SELECT stato,COUNT(*) tot FROM ai_ente_lavori_elementi
        WHERE lavoro_id=? GROUP BY stato""",(row["id"],)).fetchall()}
    total=sum(by_status.values())
    done=total-by_status.get("in_attesa",0)
    retry=row.get("prossimo_tentativo")
    row.update({"totale":total,"completati":done,
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
        db.execute("""UPDATE ai_ente_lavori
            SET stato='in_coda',prossimo_tentativo=NULL,aggiornato_il=?
            WHERE stato='attesa_quota' AND prossimo_tentativo<=?
        """,(_now(),now))
        job=db.execute("""SELECT * FROM ai_ente_lavori
            WHERE stato IN ('in_coda','in_corso') ORDER BY id LIMIT 1""").fetchone()
        if not job:
            return False
        job=dict(job)
        pending=db.execute("""SELECT * FROM ai_ente_lavori_elementi
            WHERE lavoro_id=? AND stato='in_attesa'
            ORDER BY candidatura_id,incarico_id LIMIT 1
        """,(job["id"],)).fetchone()
        if not pending:
            _set_status(db,job["id"],"completata",prossimo_tentativo=None)
            db.commit()
            return True
        pending=dict(pending)
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
        if not eligible or _hash(prepare_minimal_payload(eligible),job["modello"])!=pending["input_sha256"]:
            raise ValueError("Dati PICA modificati: annullare la coda e avviare una nuova analisi")
        result=send(get_key(),job["modello"],expected)
    except GroqLimit as exc:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO ai_ente_quota_eventi
                          (eseguito_il,modello,token) VALUES(?,?,0)""",
                       (time.time(),job["modello"]))
            # Il server ha un'indicazione del provider, che prevale sulla stima.
            _set_status(db,job["id"],"attesa_quota",
                prossimo_tentativo=time.time()+max(10,min(exc.retry_after,MAX_RETRY_SECONDS)),
                ultimo_errore="Limite GroqCloud HTTP 429: ripresa automatica")
            db.commit()
        return True
    except (ValueError,OSError,TypeError,KeyError) as exc:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
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
        db.execute("""UPDATE ai_ente_lavori SET
            chiamate=chiamate+?,token=token+?,
            aggiornato_il=? WHERE id=?""",
            (1 if origin=="groqcloud" else 0,token,_now(),job["id"]))
        db.commit()
    return True
