"""Credenziali Admin protette e recupero d'emergenza avviato solo da Home Assistant.

Un solo account amministratore RankUP; le opzioni HA possono inizializzarlo ma,
dopo la migrazione, non sono più una fonte valida per la password. Solo un cambio
del contatore di richiesta recupero in HA abilita un codice monouso temporaneo.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import time
from datetime import datetime,timezone

from db_lifecycle import AutoClosingConnection

ITERATIONS=390_000
RECOVERY_LIFETIME=10*60
RECOVERY_MAX_ATTEMPTS=5
ADMIN_MIN_PASSWORD=12


def schema(db:sqlite3.Connection)->None:
    db.execute("""
       CREATE TABLE IF NOT EXISTS rankup_admin_auth (
         id INTEGER PRIMARY KEY CHECK(id=1),
         username TEXT NOT NULL,
         password_salt TEXT NOT NULL DEFAULT '',
         password_hash TEXT NOT NULL DEFAULT '',
         password_modificata_il TEXT NOT NULL DEFAULT '',
         recovery_request INTEGER NOT NULL DEFAULT 0,
         recovery_sha256 TEXT NOT NULL DEFAULT '',
         recovery_scade REAL NOT NULL DEFAULT 0,
         recovery_tentativi INTEGER NOT NULL DEFAULT 0
       )""")


def options(data_dir:Path)->dict:
    try:
        loaded=json.loads((data_dir/"options.json").read_text(encoding="utf-8"))
        return loaded if isinstance(loaded,dict) else {}
    except (FileNotFoundError,OSError,ValueError,TypeError):
        return {}


def derive(password:str,salt:bytes)->bytes:
    return hashlib.pbkdf2_hmac("sha256",password.encode("utf-8"),salt,ITERATIONS)


def password_record(password:str)->tuple[str,str]:
    if (not isinstance(password,str) or
            not ADMIN_MIN_PASSWORD<=len(password)<=128 or not password.strip()):
        raise ValueError("Password Admin: da 12 a 128 caratteri")
    salt=secrets.token_bytes(24)
    return salt.hex(),derive(password,salt).hex()


def bootstrap(db_path:Path,settings:dict)->dict:
    """Migrazione una tantum. Mai riutilizzare admin_password HA dopo il primo inserimento."""
    name=str(settings.get("admin_username") or "admin").strip()
    if not 3<=len(name)<=50:
        name="admin"
    password=settings.get("admin_password","")
    with sqlite3.connect(db_path,timeout=20,factory=AutoClosingConnection) as db:
        db.row_factory=sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT * FROM rankup_admin_auth WHERE id=1").fetchone()
        if current is None:
            if isinstance(password,str) and 12<=len(password)<=128 and password.strip():
                salt,digest=password_record(password)
                when=datetime.now(timezone.utc).isoformat(timespec="microseconds")
            else:
                salt,digest,when="","",""
            db.execute("""INSERT INTO rankup_admin_auth
                (id,username,password_salt,password_hash,password_modificata_il)
                VALUES(1,?,?,?,?)""",(name,salt,digest,when))
            current=db.execute("SELECT * FROM rankup_admin_auth WHERE id=1").fetchone()
        return dict(current)


def get_admin(db_path:Path)->dict:
    with sqlite3.connect(db_path,timeout=20,factory=AutoClosingConnection) as db:
        db.row_factory=sqlite3.Row
        row=db.execute("SELECT * FROM rankup_admin_auth WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("Credenziali Admin non inizializzate")
        return dict(row)


def change_password(db_path:Path,old_password:str,new_password:str)->None:
    salt,digest=password_record(new_password)
    with sqlite3.connect(db_path,timeout=20,factory=AutoClosingConnection) as db:
        db.row_factory=sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT password_salt,password_hash FROM rankup_admin_auth WHERE id=1").fetchone()
        if row is None or not row["password_hash"]:
            raise PermissionError("Account Admin da inizializzare")
        old_ok=hmac.compare_digest(
            derive(old_password,bytes.fromhex(row["password_salt"])),
            bytes.fromhex(row["password_hash"]))
        if not old_ok:
            raise PermissionError("Password Admin corrente non valida")
        if hmac.compare_digest(derive(new_password,bytes.fromhex(row["password_salt"])),bytes.fromhex(row["password_hash"])):
            raise ValueError("La nuova password deve essere diversa")
        db.execute("""UPDATE rankup_admin_auth
            SET password_salt=?,password_hash=?,password_modificata_il=? WHERE id=1""",
            (salt,digest,datetime.now(timezone.utc).isoformat(timespec="microseconds")))


def issue_recovery(db_path:Path,request_number:int)->str|None:
    """Avvio soltanto con richiesta HA crescente; non crea alcun accesso o sessione."""
    if not isinstance(request_number,int) or not 1<=request_number<=999999999:
        return None
    with sqlite3.connect(db_path,timeout=20,factory=AutoClosingConnection) as db:
        db.row_factory=sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT recovery_request FROM rankup_admin_auth WHERE id=1").fetchone()
        if row is None or request_number<=row["recovery_request"]:
            return None
        code=secrets.token_urlsafe(32)
        digest=hashlib.sha256(code.encode("ascii")).hexdigest()
        db.execute("""UPDATE rankup_admin_auth SET recovery_request=?,
            recovery_sha256=?,recovery_scade=?,recovery_tentativi=0 WHERE id=1""",
            (request_number,digest,time.time()+RECOVERY_LIFETIME))
    return code


def consume_recovery(db_path:Path,code:str,new_password:str)->str:
    """Unico commit: verifica, consumo codice e nuova password Admin."""
    salt,digest=password_record(new_password)
    supplied=hashlib.sha256(str(code).encode("utf-8")).hexdigest()
    with sqlite3.connect(db_path,timeout=20,factory=AutoClosingConnection) as db:
        db.row_factory=sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("""SELECT username,recovery_sha256,recovery_scade,
                                 recovery_tentativi
                          FROM rankup_admin_auth WHERE id=1""").fetchone()
        if not row:
            raise PermissionError("Codice non valido o scaduto")
        valid=(bool(row["recovery_sha256"]) and
               row["recovery_scade"]>time.time() and
               row["recovery_tentativi"]<RECOVERY_MAX_ATTEMPTS and
               hmac.compare_digest(supplied,row["recovery_sha256"]))
        if not valid:
            if row["recovery_sha256"]:
                attempt=row["recovery_tentativi"]+1
                db.execute("""UPDATE rankup_admin_auth SET recovery_tentativi=?,
                    recovery_sha256=CASE WHEN ?>=? THEN '' ELSE recovery_sha256 END
                    WHERE id=1""",(attempt,attempt,RECOVERY_MAX_ATTEMPTS))
            raise PermissionError("Codice non valido o scaduto")
        db.execute("""UPDATE rankup_admin_auth SET password_salt=?,password_hash=?,
            password_modificata_il=?,recovery_sha256='',recovery_scade=0,
            recovery_tentativi=0 WHERE id=1""",
            (salt,digest,datetime.now(timezone.utc).isoformat(timespec="microseconds")))
        return str(row["username"])
