"""Registri sicurezza e operazioni RankUP, senza password, token o dati PICA.

Gli eventi HTTP descrivono l'esito della richiesta, non sostituiscono gli
storici amministrativi già transazionali. La lettura è riservata all'Admin.
"""
from __future__ import annotations

from datetime import datetime,timezone
import ipaddress
import re
import sqlite3

EVENT_TYPES=frozenset(("sicurezza","operazioni"))
MAX_PAGE_SIZE=100
TYPES={
    "login_ok":"Accesso riuscito",
    "login_fallito":"Accesso non riuscito",
    "logout":"Uscita",
    "password_personale":"Cambio password operatore",
    "password_reset":"Reimpostazione password operatore",
    "password_riattivazione":"Ripristino account operatore",
    "utente":"Gestione account",
    "policy":"Modifica politica password",
    "http_modifica":"Richiesta di modifica",
    "http_download":"Download protetto",
}

def create_schema(db:sqlite3.Connection)->None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS rankup_audit_eventi (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ambito TEXT NOT NULL CHECK(ambito IN ('sicurezza','operazioni')),
        evento TEXT NOT NULL,
        esito TEXT NOT NULL CHECK(esito IN ('ok','negato','errore')),
        utente TEXT NOT NULL DEFAULT '',
        metodo TEXT NOT NULL DEFAULT '',
        percorso TEXT NOT NULL DEFAULT '',
        oggetto TEXT NOT NULL DEFAULT '',
        origine TEXT NOT NULL DEFAULT '',
        quando TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_audit_data
        ON rankup_audit_eventi(quando DESC,id DESC);
    CREATE INDEX IF NOT EXISTS ix_audit_filtro
        ON rankup_audit_eventi(ambito,evento,quando DESC);
    CREATE TABLE IF NOT EXISTS rankup_politiche_sicurezza (
        id INTEGER PRIMARY KEY CHECK(id=1),
        password_max_giorni INTEGER NOT NULL DEFAULT 0
            CHECK(password_max_giorni IN (0,180)),
        aggiornato_il TEXT NOT NULL DEFAULT ''
    );
    INSERT OR IGNORE INTO rankup_politiche_sicurezza
      (id,password_max_giorni,aggiornato_il) VALUES(1,0,'');
    """)

def coarse_origin(host:str|None)->str:
    """Origine ASGI, deliberatamente non fidarsi di header arbitrari."""
    try:
        address=ipaddress.ip_address(host or "")
        if address.version==4:
            return str(ipaddress.ip_network(str(address)+"/24",strict=False))
        return str(ipaddress.ip_network(str(address)+"/64",strict=False))
    except ValueError:
        return "non disponibile"

def insert_event(db:sqlite3.Connection,ambito:str,evento:str,esito:str,
                 utente:str="",metodo:str="",percorso:str="",
                 oggetto:str="",origine:str="")->None:
    if ambito not in EVENT_TYPES or esito not in {"ok","negato","errore"}:
        raise ValueError("Valori evento non consentiti")
    db.execute("""INSERT INTO rankup_audit_eventi
       (ambito,evento,esito,utente,metodo,percorso,oggetto,origine,quando)
       VALUES(?,?,?,?,?,?,?,?,?)""",(
        ambito,str(evento)[:80],esito,str(utente)[:50],
        str(metodo)[:8],str(percorso)[:150],str(oggetto)[:80],
        str(origine)[:90],datetime.now(timezone.utc).isoformat(timespec="microseconds")
    ))

def route_event(method:str,route_path:str)->tuple[str,str]|None:
    """Nessun log di lettura delle schermate, filtri o polling."""
    if route_path.startswith("/api/auth/"):
        return None
    if method in {"POST","PUT","PATCH","DELETE"} and route_path.startswith("/api/"):
        return ("http_modifica","operazioni")
    if method=="GET" and route_path.startswith("/api/"):
        if re.search(r"(?:/zip|/pdf|/documento|/xlsx|/download|/backup/)",route_path,re.I):
            return ("http_download","operazioni")
    return None

def target_for_path(path:str)->str:
    found=re.match(r"^/api/bandi/([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)(?:/|$)",path)
    return found.group(1).upper() if found else ""

def get_events(db:sqlite3.Connection,ambito:str="",evento:str="",utente:str="",
               esito:str="",dal:str="",al:str="",page:int=1,limit:int=50)->dict:
    if ambito and ambito not in EVENT_TYPES:
        raise ValueError("Ambito non valido")
    if esito and esito not in {"ok","negato","errore"}:
        raise ValueError("Esito non valido")
    if not 1<=page<=100_000 or not 1<=limit<=MAX_PAGE_SIZE:
        raise ValueError("Paginazione non valida")
    cond=[];args=[]
    for col,value in (("ambito",ambito),("evento",evento),("utente",utente),("esito",esito)):
        if value:
            cond.append(f"{col}=?");args.append(value)
    if dal:
        cond.append("quando>=?");args.append(dal)
    if al:
        cond.append("quando<?");args.append(al)
    where=" WHERE "+" AND ".join(cond) if cond else ""
    total=db.execute("SELECT COUNT(*) FROM rankup_audit_eventi"+where,args).fetchone()[0]
    rows=db.execute("""SELECT id,ambito,evento,esito,utente,metodo,percorso,
                             oggetto,origine,quando FROM rankup_audit_eventi"""+where+
        " ORDER BY quando DESC,id DESC LIMIT ? OFFSET ?",
        [*args,limit,(page-1)*limit]).fetchall()
    return {"totale":total,"pagina":page,"per_pagina":limit,
            "eventi":[dict(r) for r in rows]}
