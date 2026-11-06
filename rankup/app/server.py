"""RankUp - prima milestone operativa (Home Assistant Ingress).

Accesso diretto possibile soltanto dopo autenticazione RankUp.
Home Assistant Ingress rimane disponibile come ulteriore percorso di accesso.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
import zipfile
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from contextvars import ContextVar

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader

from pica import MAX_FILE_BYTES, parse_pica
from ente_check import evaluate_ente
from ai_ente import status as ai_ollama_status, review_occupation, AIEmployerError
from pdf_import import (
    evaluate_pdf, MAX_PDF_BYTES, MAX_BATCH_BYTES, MAX_BATCH_FILES,
    classify_esito, ESITI_IDONEI, ESITI_NON_IDONEI, ESITI_ESCLUSI,
)
from commissioni import MAX_COMMISSION_FILE, parse_commissioni, make_commission_template
from commission_match import commission_index, resolve_commission, course_key, seat_key
from auth import Authentication, COOKIE_NAME, SESSION_SECONDS, cookie_path, secure_cookie
from schede import make_scheda
from graduatorie import build_graduatoria, render_graduatoria_pdf
from occupazione_report import report_cds_pdf
from incarichi_fields import EXTRA_DB_FIELDS, incarico_extra_db

BASE = Path(os.getenv("RANKUP_DATA_DIR", "/data"))
PENDING = BASE / "pending"
SOURCES = BASE / "sources"
DB = BASE / "rankup.sqlite3"
ASSETS = Path(__file__).resolve().parent / "static"
CODE_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$")
USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,49}$")
CURRENT_ACTOR: ContextVar[str | None] = ContextVar("rankup_actor",default=None)


def request_actor() -> str:
    """L'autore degli eventi è l'utente della richiesta corrente."""
    return CURRENT_ACTOR.get() or "sistema"


for folder in (BASE, PENDING, SOURCES):
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:
        pass


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=20000")
    return db


def initialize() -> None:
    with connect() as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS bandi (
            id INTEGER PRIMARY KEY,
            codice TEXT NOT NULL UNIQUE,
            stato TEXT NOT NULL DEFAULT 'Candidature importate',
            created_at TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            import_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS incarichi (
            id INTEGER PRIMARY KEY,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            cds TEXT NOT NULL,
            sede TEXT NOT NULL,
            insegnamento TEXT NOT NULL,
            modulo TEXT NOT NULL,
            ul TEXT,
            codice_copertura TEXT,
            id_copertura TEXT,
            UNIQUE(bando_id, key)
        );
        CREATE TABLE IF NOT EXISTS candidature (
            id INTEGER PRIMARY KEY,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            incarico_id INTEGER NOT NULL REFERENCES incarichi(id) ON DELETE CASCADE,
            id_pica TEXT NOT NULL,
            nome TEXT NOT NULL,
            cognome TEXT NOT NULL,
            codice_fiscale TEXT,
            stato_pica TEXT,
            stato TEXT NOT NULL DEFAULT 'Attiva',
            source_row INTEGER NOT NULL,
            source_json TEXT NOT NULL,
            UNIQUE(bando_id, incarico_id, id_pica)
        );
        CREATE TABLE IF NOT EXISTS commissioni (
            id INTEGER PRIMARY KEY,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            cds TEXT NOT NULL,
            sede TEXT NOT NULL,
            presidente TEXT NOT NULL,
            componenti TEXT NOT NULL,
            requisiti TEXT,
            anno TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(bando_id, cds, sede)
        );
        CREATE TABLE IF NOT EXISTS commissioni_eventi (
            id INTEGER PRIMARY KEY,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            commissione_id INTEGER,
            azione TEXT NOT NULL,
            precedente_json TEXT,
            successivo_json TEXT,
            autore TEXT NOT NULL,
            quando TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_comm_eventi_bando ON commissioni_eventi(bando_id);
        CREATE TABLE IF NOT EXISTS rankup_utenti (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            attivo INTEGER NOT NULL DEFAULT 1 CHECK(attivo IN (0,1)),
            credenziali_versione INTEGER NOT NULL DEFAULT 1,
            creato_il TEXT NOT NULL,
            aggiornato_il TEXT NOT NULL,
            creato_da TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rankup_utenti_eventi (
            id INTEGER PRIMARY KEY,
            utente_id INTEGER NOT NULL REFERENCES rankup_utenti(id),
            azione TEXT NOT NULL,
            autore TEXT NOT NULL,
            quando TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_rankup_utenti_eventi
          ON rankup_utenti_eventi(utente_id,quando);
        CREATE TABLE IF NOT EXISTS bandi_riferimenti (
            bando_id INTEGER PRIMARY KEY REFERENCES bandi(id) ON DELETE CASCADE,
            repertorio TEXT NOT NULL DEFAULT '',
            protocollo TEXT NOT NULL DEFAULT '',
            data_avviso TEXT NOT NULL DEFAULT '',
            anno_accademico TEXT NOT NULL DEFAULT '',
            aggiornato_il TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bandi_riferimenti_eventi (
            id INTEGER PRIMARY KEY,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            prima_json TEXT NOT NULL,
            dopo_json TEXT NOT NULL,
            autore TEXT NOT NULL,
            quando TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ssd_classificazioni (
            ssd TEXT PRIMARY KEY,
            tipo TEXT NOT NULL CHECK(tipo IN ('professionalizzante','ordinario')),
            riferimento TEXT NOT NULL,
            aggiornato_il TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ssd_import_migrations (
            bando_id INTEGER PRIMARY KEY REFERENCES bandi(id) ON DELETE CASCADE,
            aggiornato_il TEXT NOT NULL,
            incarichi_verificati INTEGER NOT NULL,
            incarichi_corretti INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ssd_import_rettifiche (
            id INTEGER PRIMARY KEY,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            key_incarico TEXT NOT NULL,
            ssd_precedente TEXT NOT NULL,
            ssd_corretto TEXT NOT NULL,
            corretto_il TEXT NOT NULL,
            UNIQUE(bando_id,key_incarico)
        );
        CREATE TABLE IF NOT EXISTS incarichi_metadata_migrations (
            bando_id INTEGER PRIMARY KEY REFERENCES bandi(id) ON DELETE CASCADE,
            source_sha256 TEXT NOT NULL,
            metodo TEXT NOT NULL,
            incarichi_verificati INTEGER NOT NULL,
            incarichi_aggiornati INTEGER NOT NULL,
            campi_compilati INTEGER NOT NULL,
            aggiornato_il TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS incarichi_metadata_audit (
            id INTEGER PRIMARY KEY,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            key_incarico TEXT NOT NULL,
            prima_json TEXT NOT NULL,
            dopo_json TEXT NOT NULL,
            aggiornato_il TEXT NOT NULL,
            UNIQUE(bando_id, key_incarico)
        );
        CREATE INDEX IF NOT EXISTS ix_comm_bando ON commissioni(bando_id);
        CREATE INDEX IF NOT EXISTS ix_inc_bando ON incarichi(bando_id);
        CREATE TABLE IF NOT EXISTS valutazioni_pdf (
            id INTEGER PRIMARY KEY,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            candidatura_id INTEGER REFERENCES candidature(id) ON DELETE SET NULL,
            stato TEXT NOT NULL CHECK(stato IN ('Acquisita','Da verificare')),
            versione INTEGER NOT NULL,
            attiva INTEGER NOT NULL DEFAULT 0 CHECK(attiva IN (0,1)),
            sha256 TEXT NOT NULL,
            nome_originale TEXT NOT NULL,
            percorso TEXT NOT NULL,
            modello TEXT NOT NULL,
            punteggi_json TEXT NOT NULL DEFAULT '{}',
            totale_calcolato REAL,
            totale_pdf TEXT NOT NULL DEFAULT '',
            esito TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            motivo TEXT NOT NULL DEFAULT '',
            confermata_da TEXT NOT NULL,
            confermata_il TEXT NOT NULL,
            UNIQUE(bando_id, sha256)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_val_attiva
          ON valutazioni_pdf(candidatura_id)
          WHERE stato='Acquisita' AND attiva=1;
        CREATE TABLE IF NOT EXISTS valutazioni_pdf_eventi (
            id INTEGER PRIMARY KEY,
            bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
            valutazione_pdf_id INTEGER NOT NULL REFERENCES valutazioni_pdf(id),
            candidatura_id INTEGER NOT NULL REFERENCES candidature(id),
            azione TEXT NOT NULL,
            precedente_json TEXT NOT NULL,
            successivo_json TEXT NOT NULL,
            autore TEXT NOT NULL,
            quando TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_val_eventi
          ON valutazioni_pdf_eventi(bando_id,valutazione_pdf_id);
        CREATE INDEX IF NOT EXISTS ix_val_bando ON valutazioni_pdf(bando_id,candidatura_id);
        CREATE INDEX IF NOT EXISTS ix_cand_bando ON candidature(bando_id);
        """)
        user_columns={r["name"] for r in db.execute("PRAGMA table_info(rankup_utenti)")}
        if "eliminato" not in user_columns:
            db.execute("ALTER TABLE rankup_utenti ADD COLUMN eliminato INTEGER NOT NULL DEFAULT 0")
        db.execute("""
            CREATE TABLE IF NOT EXISTS incarichi_ente_requisiti (
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                key_incarico TEXT NOT NULL,
                ente_richiesto TEXT NOT NULL,
                fonte TEXT NOT NULL,
                confermato_da TEXT NOT NULL,
                confermato_il TEXT NOT NULL,
                PRIMARY KEY(bando_id,key_incarico)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS incarichi_ente_eventi (
                id INTEGER PRIMARY KEY,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                key_incarico TEXT NOT NULL,
                precedente_json TEXT NOT NULL,
                successivo_json TEXT NOT NULL,
                autore TEXT NOT NULL,
                quando TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS pica_tipo_cds_dizionario (
                chiave TEXT PRIMARY KEY,
                dicitura TEXT NOT NULL,
                requisito TEXT NOT NULL,
                fonte TEXT NOT NULL,
                autore TEXT NOT NULL,
                aggiornato_il TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS pica_tipo_cds_eventi (
                id INTEGER PRIMARY KEY,
                chiave TEXT NOT NULL,
                precedente_json TEXT NOT NULL,
                successivo_json TEXT NOT NULL,
                autore TEXT NOT NULL,
                quando TEXT NOT NULL
            )
        """)
        # Cinque corrispondenze dichiarate dall'amministratore, non desunte da PDF.
        for label, requirement in (
            ("contratto esterno oneroso CSA", "NESSUNA RISERVA"),
            ("contratto esterno oneroso UGOV C", "NESSUNA RISERVA"),
            ("contratto esterno oneroso a carico APSS TN CSA", "SSN"),
            ("contratto esterno oneroso dip. SSN CSA", "SSN"),
            ("supplenza esterna retribuita", "ALTRO ATENEO"),
        ):
            db.execute("""
                INSERT OR IGNORE INTO pica_tipo_cds_dizionario
                (chiave,dicitura,requisito,fonte,autore,aggiornato_il)
                VALUES(?,?,?,?,?,?)
            """, (" ".join(label.split()).casefold(), label, requirement,
                  "Corrispondenza comunicata dall'amministratore",
                  "configurazione iniziale", datetime.now(timezone.utc).isoformat()))
        db.execute("""
            CREATE TABLE IF NOT EXISTS valutazioni_occupazione_ai (
                id INTEGER PRIMARY KEY,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                valutazione_pdf_id INTEGER NOT NULL REFERENCES valutazioni_pdf(id),
                candidatura_id INTEGER NOT NULL REFERENCES candidature(id),
                requisito_analizzato TEXT NOT NULL,
                tipo_cds_pica TEXT NOT NULL,
                occupazione_pica TEXT NOT NULL,
                azienda_pica TEXT NOT NULL,
                profilo_pica TEXT NOT NULL,
                stato TEXT NOT NULL,
                motivo TEXT NOT NULL,
                evidenza TEXT NOT NULL,
                modello_locale INTEGER NOT NULL DEFAULT 0,
                autore TEXT NOT NULL,
                quando TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS ix_ai_occupazione_pdf
            ON valutazioni_occupazione_ai(valutazione_pdf_id, id DESC)
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS graduatorie_cds (
                id INTEGER PRIMARY KEY,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                cds TEXT NOT NULL,
                sede TEXT NOT NULL,
                stato TEXT NOT NULL CHECK(stato IN ('definitiva','approvata')),
                dati_json TEXT NOT NULL,
                hash_dati TEXT NOT NULL,
                confermata_da TEXT NOT NULL,
                confermata_il TEXT NOT NULL,
                approvazione_id INTEGER,
                UNIQUE(bando_id,cds,sede)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS graduatorie_approvazioni (
                id INTEGER PRIMARY KEY,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                stato TEXT NOT NULL CHECK(stato IN ('bozza','approvata')),
                dati_json TEXT NOT NULL,
                estremi_atto TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                creata_da TEXT NOT NULL,
                creata_il TEXT NOT NULL,
                approvata_da TEXT,
                approvata_il TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS graduatorie_eventi (
                id INTEGER PRIMARY KEY,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                oggetto TEXT NOT NULL,
                oggetto_id INTEGER NOT NULL,
                evento TEXT NOT NULL,
                dettagli_json TEXT NOT NULL,
                autore TEXT NOT NULL,
                quando TEXT NOT NULL
            )
        """)
        # Migrazione incrementale non distruttiva: ogni scheda già acquisita
        # conserva il PDF e lo storico; nessuna idoneità viene dedotta dal voto.
        existing = {r["name"] for r in db.execute("PRAGMA table_info(valutazioni_pdf)")}
        for field, definition in (
            ("decisione", "TEXT NOT NULL DEFAULT 'da_verificare'"),
            ("metodo", "TEXT NOT NULL DEFAULT 'automatico'"),
            ("motivazione_tecnica", "TEXT NOT NULL DEFAULT ''"),
        ):
            if field not in existing:
                db.execute("ALTER TABLE valutazioni_pdf ADD COLUMN " + field + " " + definition)
        # Esiti storici già esplicitamente registrati: classificazione esatta.
        db.execute("""UPDATE valutazioni_pdf SET decisione=
            CASE
              WHEN esito IN (?,?) THEN 'idoneo'
              WHEN esito IN (?,?) THEN 'non_idoneo'
              WHEN esito IN (?,?,?) THEN 'escluso'
              ELSE decisione
            END
            WHERE decisione='da_verificare' AND stato='Acquisita'
        """, ESITI_IDONEI+ESITI_NON_IDONEI+ESITI_ESCLUSI)
        # Riesame UNA VOLTA delle acquisizioni delle vecchie versioni.
        # Il precedente parser poteva acquisire 15 punti senza una decisione
        # della commissione. Non si può dedurre "idoneo" dal solo totale.
        # L'originale, il totale, l'esito e il timestamp storici non cambiano.
        # Anche le versioni non attive vengono riclassificate, per evitare
        # uno stato 'Acquisita' ingannevole nello storico.
        legacy=db.execute("""
            SELECT * FROM valutazioni_pdf
            WHERE stato='Acquisita' AND (
                decisione='da_verificare'
                OR (decisione IN ('non_idoneo','escluso') AND TRIM(note)='')
                OR (decisione='idoneo' AND totale_calcolato IS NULL)
            )
        """).fetchall()
        if legacy:
            now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
            for old in legacy:
                prior=dict(old)
                reason=(
                    "Riesame di compatibilità: valutazione acquisita in una versione precedente "
                    "senza esito o requisiti di validazione completi. "
                    "Punteggio storico conservato, ma non convalidato."
                )
                db.execute("""
                    UPDATE valutazioni_pdf
                    SET stato='Da verificare',attiva=0,decisione='da_verificare',motivo=?
                    WHERE id=? AND stato='Acquisita'
                """,(reason,old["id"]))
                updated=dict(db.execute(
                    "SELECT * FROM valutazioni_pdf WHERE id=?",(old["id"],)
                ).fetchone())
                if old["candidatura_id"] is not None:
                    db.execute("""
                        INSERT INTO valutazioni_pdf_eventi
                        (bando_id,valutazione_pdf_id,candidatura_id,azione,
                         precedente_json,successivo_json,autore,quando)
                        VALUES(?,?,?,?,?,?,?,?)
                    """,(
                        old["bando_id"],old["id"],old["candidatura_id"],
                        "Riesame automatico acquisizione precedente",
                        json.dumps(prior,ensure_ascii=False),
                        json.dumps(updated,ensure_ascii=False),
                        "migrazione-applicativa",now
                    ))
        columns = {row["name"] for row in db.execute("PRAGMA table_info(incarichi)")}
        if "ssd" not in columns:
            db.execute("ALTER TABLE incarichi ADD COLUMN ssd TEXT NOT NULL DEFAULT ''")
        for field in EXTRA_DB_FIELDS:
            if field not in columns:
                db.execute("ALTER TABLE incarichi ADD COLUMN " + field +
                           " TEXT NOT NULL DEFAULT ''")


def backfill_ssd_from_saved_pica() -> None:
    """Corregge UNA VOLTA gli SSD gia importati dalla colonna incarico di PICA.

    La migrazione corregge anche i valori erronei non vuoti ottenuti in
    precedenza dal campo professionale del candidato. Non reimporta e
    non modifica candidature, classificazioni SSD o altri dati.
    """
    with connect() as db:
        pending = db.execute("""
            SELECT b.id,b.codice FROM bandi b
            LEFT JOIN ssd_import_migrations m ON m.bando_id=b.id
            WHERE m.bando_id IS NULL
        """).fetchall()

    for b in pending:
        source = None
        for extension in (".xls", ".xlsx"):
            candidate = SOURCES / b["codice"] / ("pica_originale" + extension)
            if candidate.is_file():
                source = candidate
                break
        if source is None:
            continue  # Non modificare SSD senza l'originale PICA.

        try:
            parsed = parse_pica(source.name, source.read_bytes())
        except (ValueError, OSError):
            continue
        if parsed["code"] != b["codice"]:
            continue

        expected = {}
        for item in parsed["rows"]:
            inc = item["incarico"]
            key = inc["key"]
            value = inc.get("ssd", "").strip().upper()
            if key in expected and expected[key] != value:
                # Nessun valore viene assegnato in caso di dati incoerenti.
                expected = {}
                break
            expected[key] = value

        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT key,ssd FROM incarichi WHERE bando_id=?", (b["id"],)
            ).fetchall()
            if not current or not expected or any(row["key"] not in expected for row in current):
                db.rollback()
                continue  # Evitare correzioni parziali.
            changes = 0
            for row in current:
                correct = expected[row["key"]]
                if (row["ssd"] or "").strip().upper() != correct:
                    db.execute("""
                        INSERT INTO ssd_import_rettifiche
                        (bando_id,key_incarico,ssd_precedente,ssd_corretto,corretto_il)
                        VALUES(?,?,?,?,?)
                    """, (
                        b["id"], row["key"], row["ssd"] or "", correct,
                        datetime.now(timezone.utc).isoformat(timespec="seconds")
                    ))
                    db.execute(
                        "UPDATE incarichi SET ssd=? WHERE bando_id=? AND key=?",
                        (correct, b["id"], row["key"])
                    )
                    changes += 1
            db.execute("""
                INSERT INTO ssd_import_migrations
                (bando_id,aggiornato_il,incarichi_verificati,incarichi_corretti)
                VALUES(?,?,?,?)
            """, (
                b["id"], datetime.now(timezone.utc).isoformat(timespec="seconds"),
                len(current), changes
            ))
            db.commit()


def backfill_incarichi_metadata() -> None:
    """Integra una sola volta gli incarichi precedenti dalla fonte PICA verificata.

    Non modifica candidature, chiavi degli incarichi, SSD o campi già
    valorizzati. Se fonte, record o valori non corrispondono, non interviene.
    """
    with connect() as db:
        bandi = db.execute("""
            SELECT b.id,b.codice,b.source_hash
            FROM bandi b
            LEFT JOIN incarichi_metadata_migrations m ON m.bando_id=b.id
            WHERE m.bando_id IS NULL
        """).fetchall()

    for b in bandi:
        source = None
        for extension in (".xls", ".xlsx"):
            candidate = SOURCES / b["codice"] / ("pica_originale" + extension)
            if candidate.is_file():
                source = candidate
                break
        if source is None:
            continue

        try:
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != b["source_hash"]:
                continue  # File diverso da quello confermato all'importazione.
            parsed = parse_pica(source.name, raw)
        except (OSError, ValueError):
            continue
        if parsed["code"] != b["codice"]:
            continue

        expected = {}
        consistent = True
        for row in parsed["rows"]:
            key = row["incarico"]["key"]
            values = incarico_extra_db(row["incarico"])
            previous = expected.get(key)
            if previous is not None and values != previous:
                consistent = False
                break
            expected[key] = values
        if not consistent or not expected:
            continue

        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute(
                "SELECT 1 FROM incarichi_metadata_migrations WHERE bando_id=?",
                (b["id"],)
            ).fetchone()
            if exists:
                db.rollback()
                continue
            current = db.execute(
                "SELECT id,key," + ",".join(EXTRA_DB_FIELDS) +
                " FROM incarichi WHERE bando_id=?", (b["id"],)
            ).fetchall()
            if (len(current) != len(expected)
                    or any(row["key"] not in expected for row in current)):
                db.rollback()
                continue

            # Rifiuta l'intera migrazione se un campo già acquisito differisce
            # dalla fonte: non sovrascrivere mai integrazioni o rettifiche.
            incompatible = any(
                row[field] and row[field] != expected[row["key"]][field]
                for row in current for field in EXTRA_DB_FIELDS
            )
            if incompatible:
                db.rollback()
                continue

            changed_items = 0
            field_count = 0
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for row in current:
                before = {field: row[field] for field in EXTRA_DB_FIELDS}
                target = expected[row["key"]]
                changes = {field: target[field] for field in EXTRA_DB_FIELDS
                           if not row[field] and target[field]}
                if not changes:
                    continue
                db.execute(
                    "UPDATE incarichi SET " +
                    ", ".join(field + "=?" for field in changes) +
                    " WHERE id=?", (*changes.values(), row["id"])
                )
                after = dict(before)
                after.update(changes)
                db.execute("""
                    INSERT INTO incarichi_metadata_audit
                    (bando_id,key_incarico,prima_json,dopo_json,aggiornato_il)
                    VALUES(?,?,?,?,?)
                """, (b["id"], row["key"],
                      json.dumps(before, ensure_ascii=False),
                      json.dumps(after, ensure_ascii=False), now))
                changed_items += 1
                field_count += len(changes)

            db.execute("""
                INSERT INTO incarichi_metadata_migrations
                (bando_id,source_sha256,metodo,incarichi_verificati,
                 incarichi_aggiornati,campi_compilati,aggiornato_il)
                VALUES(?,?,?,?,?,?,?)
            """, (b["id"], b["source_hash"], "migrazione",
                  len(current), changed_items, field_count, now))
            db.commit()


initialize()
backfill_ssd_from_saved_pica()
backfill_incarichi_metadata()
app = FastAPI(title="RankUp", version="0.9.0", docs_url=None, redoc_url=None)
AUTH = Authentication(BASE)
app.mount("/static", StaticFiles(directory=str(ASSETS)), name="static")

@app.middleware("http")
async def require_rankup_login(request: Request, call_next):
    path = request.url.path
    public = (
        path in ("/", "/api/health", "/api/auth/status", "/api/auth/login")
        or path.startswith("/static/")
    )
    # Evita che un sito esterno invii form non autorizzati usando i cookie.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-rankup-request") != "1":
            return JSONResponse({"detail": "Richiesta non autorizzata"}, status_code=403)
    principal = AUTH.principal(request.cookies.get(COOKIE_NAME))
    if not public and principal is None:
        return JSONResponse({"detail": "Autenticazione richiesta"}, status_code=401)
    token = CURRENT_ACTOR.set(principal["username"] if principal else None)
    try:
        response = await call_next(request)
    finally:
        CURRENT_ACTOR.reset(token)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=128)


@app.get("/api/auth/status")
def auth_status(request: Request):
    principal = AUTH.principal(request.cookies.get(COOKIE_NAME))
    return {
        "configured": AUTH.configured,
        "authenticated": principal is not None,
        "username": principal["username"] if principal else None,
        "role": principal["role"] if principal else None,
    }


@app.post("/api/auth/login")
def auth_login(credentials: LoginRequest, request: Request):
    if not AUTH.configured:
        fail("Configurare amministratore e password nella scheda Configurazione dell'add-on", 503)
    token = AUTH.authenticate(credentials.username, credentials.password)
    if token is None:
        fail("Credenziali non valide oppure troppi tentativi: riprovare più tardi", 401)
    identity=AUTH.principal(token)
    if not identity:
        AUTH.revoke(token)
        fail("Sessione non disponibile: ripetere l'accesso",401)
    response = JSONResponse({"ok": True, "username": identity["username"],
                             "role": identity["role"]})
    response.set_cookie(
        key=COOKIE_NAME, value=token, path=cookie_path(request),
        max_age=SESSION_SECONDS, secure=secure_cookie(request),
        httponly=True, samesite="lax",
    )
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    AUTH.revoke(request.cookies.get(COOKIE_NAME))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path=cookie_path(request))
    return response


# Account operativi: gestione riservata all'Admin, non delegabile agli utenti.
def require_admin(request: Request) -> dict:
    principal = AUTH.principal(request.cookies.get(COOKIE_NAME))
    if not principal or principal["role"] != "admin":
        fail("Gestione degli utenti riservata all'amministratore",403)
    return principal


class CreaUtente(BaseModel):
    username: str = Field(min_length=3,max_length=50)
    password: str = Field(min_length=8,max_length=128)


class CambiaStatoUtente(BaseModel):
    attivo: bool


class ReimpostaPasswordUtente(BaseModel):
    password: str = Field(min_length=8,max_length=128)


@app.get("/api/utenti")
def elenco_utenti(request: Request):
    require_admin(request)
    with connect() as db:
        rows=db.execute("""
            SELECT id,username,attivo,creato_il,aggiornato_il,creato_da
            FROM rankup_utenti WHERE eliminato=0 ORDER BY username COLLATE NOCASE
        """).fetchall()
    return {"utenti":[dict(r) for r in rows],
            "admin":{"username":AUTH.username,"gestito_da":"Configurazione Home Assistant"},
            "password_minima":8}


@app.post("/api/utenti")
def crea_utente(body: CreaUtente,request: Request):
    who=require_admin(request)
    username=body.username.strip()
    if not USER_RE.fullmatch(username):
        fail("Username: 3-50 caratteri; sono ammessi lettere, numeri, punto, trattino e underscore")
    if username.casefold()==AUTH.username.casefold():
        fail("Nome utente già riservato all'amministratore",409)
    try:
        salt,digest=AUTH.password_record(body.password)
    except ValueError as exc:
        fail(str(exc))
    now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            cur=db.execute("""
                INSERT INTO rankup_utenti
                 (username,password_salt,password_hash,attivo,credenziali_versione,
                  creato_il,aggiornato_il,creato_da)
                VALUES(?,?,?,1,1,?,?,?)
            """,(username,salt,digest,now,now,who["username"]))
        except sqlite3.IntegrityError:
            fail("Nome utente già presente; riattivalo o reimposta la sua password",409)
        uid=cur.lastrowid
        db.execute("""
            INSERT INTO rankup_utenti_eventi(utente_id,azione,autore,quando)
            VALUES(?,?,?,?)
        """,(uid,"Creato account operativo",who["username"],now))
        db.commit()
    return {"ok":True,"utente":{"id":uid,"username":username,"attivo":1}}


@app.delete("/api/utenti/{user_id}")
def elimina_utente(user_id: int,request: Request):
    who=require_admin(request)
    if user_id<=0:fail("Identificativo utente non valido")
    now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT id,username,eliminato FROM rankup_utenti WHERE id=?",(user_id,)).fetchone()
        if not row or row["eliminato"]:fail("Utente non trovato",404)
        db.execute("UPDATE rankup_utenti SET eliminato=1,attivo=0,credenziali_versione=credenziali_versione+1,aggiornato_il=? WHERE id=?",(now,user_id))
        db.execute("INSERT INTO rankup_utenti_eventi(utente_id,azione,autore,quando) VALUES(?,?,?,?)",(user_id,"Eliminato (storico conservato)",who["username"],now))
        db.commit()
    AUTH.revoke_user(user_id)
    return {"ok":True,"id":user_id,"username":row["username"],"eliminato":True}


@app.put("/api/utenti/{user_id}/stato")
def stato_utente(user_id: int,body: CambiaStatoUtente,request: Request):
    who=require_admin(request)
    if user_id<=0:fail("Identificativo utente non valido")
    now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT id,username,attivo FROM rankup_utenti WHERE id=? AND eliminato=0",(user_id,)).fetchone()
        if not row:fail("Utente non trovato",404)
        changed=bool(row["attivo"])!=body.attivo
        if changed:
            db.execute("""
                UPDATE rankup_utenti
                   SET attivo=?,credenziali_versione=credenziali_versione+1,
                       aggiornato_il=? WHERE id=?
            """,(int(body.attivo),now,user_id))
            db.execute("""
                INSERT INTO rankup_utenti_eventi(utente_id,azione,autore,quando)
                VALUES(?,?,?,?)
            """,(user_id,"Riattivato" if body.attivo else "Disattivato",who["username"],now))
        db.commit()
    if changed:
        AUTH.revoke_user(user_id)
    return {"ok":True,"id":user_id,"username":row["username"],
            "attivo":body.attivo,"modificato":changed}


@app.put("/api/utenti/{user_id}/password")
def password_utente(user_id: int,body: ReimpostaPasswordUtente,request: Request):
    who=require_admin(request)
    if user_id<=0:fail("Identificativo utente non valido")
    try:
        salt,digest=AUTH.password_record(body.password)
    except ValueError as exc:
        fail(str(exc))
    now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT username FROM rankup_utenti WHERE id=? AND eliminato=0",(user_id,)).fetchone()
        if not row:fail("Utente non trovato",404)
        db.execute("""
            UPDATE rankup_utenti SET
                password_salt=?,password_hash=?,
                credenziali_versione=credenziali_versione+1,
                aggiornato_il=?
            WHERE id=?
        """,(salt,digest,now,user_id))
        db.execute("""
            INSERT INTO rankup_utenti_eventi(utente_id,azione,autore,quando)
            VALUES(?,?,?,?)
        """,(user_id,"Password reimpostata",who["username"],now))
        db.commit()
    AUTH.revoke_user(user_id)
    return {"ok":True,"id":user_id,"username":row["username"],
            "avviso":"Password aggiornata. Tutte le sessioni precedenti dell'utente sono revocate."}





def fail(message: str, status: int = 400):
    raise HTTPException(status_code=status, detail=message)


def valid_code(code: str) -> str:
    code = code.upper()
    if not CODE_RE.fullmatch(code):
        fail("Codice bando non valido")
    return code


@app.get("/")
def index():
    return FileResponse(ASSETS / "index.html", media_type="text/html")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.9.0"}


class NuovoBandoInput(BaseModel):
    codice: str = Field(min_length=3,max_length=50)


@app.post("/api/bandi")
def prepara_bando(body: NuovoBandoInput,request: Request):
    """Crea contenitore vuoto per PDF e requisiti PRIMA delle candidature PICA."""
    require_admin(request)
    code=valid_code(body.codice.strip().upper())
    now=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        try:
            db.execute("""INSERT INTO bandi
                (codice,stato,created_at,source_filename,source_hash,import_at)
                VALUES(?,'In preparazione',?,'','',?)""",(code,now,now))
            db.commit()
        except sqlite3.IntegrityError:
            fail("Bando già presente",409)
    return {"ok":True,"codice":code,"stato":"In preparazione"}


@app.get("/api/bandi")
def list_bandi():
    with connect() as db:
        rows = db.execute("""
          SELECT b.codice, b.stato, b.import_at, b.source_filename,
                 (SELECT COUNT(*) FROM candidature c WHERE c.bando_id=b.id) AS candidature,
                 (SELECT COUNT(*) FROM incarichi i WHERE i.bando_id=b.id) AS incarichi,
                 (SELECT COUNT(DISTINCT i.cds) FROM incarichi i WHERE i.bando_id=b.id) AS cds,
                 (SELECT COUNT(*) FROM valutazioni_pdf v
                   WHERE v.bando_id=b.id AND v.stato='Acquisita' AND v.attiva=1
                     AND v.decisione IN ('idoneo','non_idoneo','escluso'))
                   AS valutazioni_acquisite
          FROM bandi b ORDER BY b.import_at DESC, b.id DESC
        """).fetchall()
    return {"bandi": [dict(x) for x in rows]}


@app.get("/api/bandi/{code}")
def detail_bando(code: str):
    code = valid_code(code)
    with connect() as db:
        b = db.execute("SELECT * FROM bandi WHERE codice=?", (code,)).fetchone()
        if b is None:
            fail("Bando non trovato", 404)
        groups = db.execute("""
          SELECT i.cds, i.sede, COUNT(DISTINCT i.id) AS incarichi,
                 COUNT(c.id) AS candidature
          FROM incarichi i LEFT JOIN candidature c ON c.incarico_id=i.id
          WHERE i.bando_id=?
          GROUP BY i.cds, i.sede ORDER BY i.cds COLLATE NOCASE, i.sede COLLATE NOCASE
        """, (b["id"],)).fetchall()
    return {"bando": {k: b[k] for k in ("codice", "stato", "source_filename", "import_at")},
            "cds_sedi": [dict(row) for row in groups]}



class RiferimentiBando(BaseModel):
    repertorio: str = Field(default="", max_length=90)
    protocollo: str = Field(default="", max_length=90)
    data_avviso: str = Field(default="", max_length=20)
    anno_accademico: str = Field(default="", max_length=20)
    expected_updated_at: str | None = None


@app.get("/api/bandi/{code}/riferimenti")
def get_riferimenti_bando(code: str):
    code = valid_code(code)
    with connect() as db:
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not b:
            fail("Bando non trovato", 404)
        row = db.execute("SELECT repertorio,protocollo,data_avviso,anno_accademico,aggiornato_il "
                         "FROM bandi_riferimenti WHERE bando_id=?", (b["id"],)).fetchone()
    return {"riferimenti": dict(row) if row else {
        "repertorio": "", "protocollo": "", "data_avviso": "",
        "anno_accademico": "", "aggiornato_il": None,
    }}


@app.put("/api/bandi/{code}/riferimenti")
def save_riferimenti_bando(code: str, body: RiferimentiBando):
    """Solo dati comunicati dall'operatore, non estratti dagli esempi PDF."""
    code = valid_code(code)
    values = {k: str(v or "").strip() for k, v in body.model_dump().items()
              if k != "expected_updated_at"}
    data = values["data_avviso"]
    if data:
        try:
            if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", data):
                raise ValueError()
            datetime.strptime(data, "%d/%m/%Y")
        except ValueError:
            fail("Data avviso: utilizzare GG/MM/AAAA, senza valori presunti")
    anno = values["anno_accademico"]
    if anno:
        if not re.fullmatch(r"\d{4}-\d{4}", anno):
            fail("Anno accademico: utilizzare AAAA-AAAA")
        first, second = [int(x) for x in anno.split("-")]
        if second != first + 1:
            fail("Anno accademico incoerente")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not b:
            fail("Bando non trovato", 404)
        current = db.execute("""
          SELECT repertorio,protocollo,data_avviso,anno_accademico
          FROM bandi_riferimenti WHERE bando_id=?
        """, (b["id"],)).fetchone()
        previous = dict(current) if current else {key: "" for key in values}
        version = db.execute("SELECT aggiornato_il FROM bandi_riferimenti WHERE bando_id=?",
                             (b["id"],)).fetchone()
        current_time = version["aggiornato_il"] if version else None
        if current_time != body.expected_updated_at:
            fail("I riferimenti sono stati modificati: ricarica la pagina", 409)
        if previous != values:
            db.execute("""
              INSERT INTO bandi_riferimenti(bando_id,repertorio,protocollo,data_avviso,
                  anno_accademico,aggiornato_il)
              VALUES(?,?,?,?,?,?)
              ON CONFLICT(bando_id) DO UPDATE SET
                repertorio=excluded.repertorio,
                protocollo=excluded.protocollo,
                data_avviso=excluded.data_avviso,
                anno_accademico=excluded.anno_accademico,
                aggiornato_il=excluded.aggiornato_il
            """, (b["id"], values["repertorio"], values["protocollo"],
                  values["data_avviso"], values["anno_accademico"], now))
            db.execute("""
              INSERT INTO bandi_riferimenti_eventi
              (bando_id,prima_json,dopo_json,autore,quando)
              VALUES(?,?,?,?,?)
            """, (b["id"], json.dumps(previous,ensure_ascii=False),
                  json.dumps(values,ensure_ascii=False), request_actor(), now))
        db.commit()
    return {"ok": True, "riferimenti": values, "modificato": previous != values}


def bando_notice_reference(ref: dict | None) -> str:
    """Compila un riferimento solo se repertorio, protocollo e data esistono."""
    if not ref:
        return ""
    rep, prot, data = [str(ref.get(k) or "").strip()
                       for k in ("repertorio", "protocollo", "data_avviso")]
    if not rep or not prot or not data:
        return ""
    return ("Come indicato nell'avviso di selezione per il conferimento "
            "degli incarichi: Rep. " + rep + " Prot. " + prot + " del " + data)


@app.get("/api/bandi/{code}/incarichi")
def list_incarichi(code: str):
    code = valid_code(code)
    with connect() as db:
        rows = db.execute("""
          SELECT i.key, i.cds, i.sede, i.insegnamento, i.modulo, i.ul,
                 i.id_copertura, i.codice_copertura, i.ssd,
                 i.anno_corso, i.tipo_corso, i.tipo_cds, i.ciclo,
                 i.inizio_pica, i.fine_pica, i.inizio_iso, i.fine_iso,
                 i.codice_partizione, i.partizione_studenti,
                 i.peso_copertura, i.ore, i.stato_coperture,
                 cl.tipo AS modello_ssd,
                 er.ente_richiesto AS ente_richiesto,
                 er.fonte AS ente_fonte,
                 COUNT(c.id) AS candidature
          FROM incarichi i
          JOIN bandi b ON b.id=i.bando_id
          LEFT JOIN candidature c ON c.incarico_id=i.id
          LEFT JOIN ssd_classificazioni cl ON cl.ssd=UPPER(TRIM(i.ssd))
          LEFT JOIN incarichi_ente_requisiti er ON er.bando_id=b.id AND er.key_incarico=i.key
          WHERE b.codice=?
          GROUP BY i.id
          ORDER BY i.cds COLLATE NOCASE, i.sede COLLATE NOCASE,
                   i.insegnamento COLLATE NOCASE, i.modulo COLLATE NOCASE
        """, (code,)).fetchall()
        migration = db.execute("""
            SELECT m.metodo, m.incarichi_verificati, m.incarichi_aggiornati,
                   m.campi_compilati, m.aggiornato_il
            FROM incarichi_metadata_migrations m
            JOIN bandi b ON b.id=m.bando_id
            WHERE b.codice=?
        """, (code,)).fetchone()
        mappings = db.execute("SELECT chiave,requisito FROM pica_tipo_cds_dizionario").fetchall()
        if not db.execute("SELECT 1 FROM bandi WHERE codice=?", (code,)).fetchone():
            fail("Bando non trovato", 404)
    dictionary = {r["chiave"]: r["requisito"] for r in mappings}
    output = []
    for row in rows:
        item = dict(row)
        mapped = dictionary.get(tipo_cds_key(item.get("tipo_cds")))
        item["requisito_pica"] = mapped
        item["stato_requisito"] = ("CONFERMATO MANUALMENTE" if item["ente_richiesto"]
                                   else "DIZIONARIO PICA" if mapped else "DA VERIFICARE")
        if not item["ente_richiesto"] and mapped:
            item["ente_richiesto"] = mapped
            item["ente_fonte"] = ("Colonna L PICA: " + (item.get("tipo_cds") or "") +
                                  " · Dizionario approvato")
        output.append(item)
    return {
        "incarichi": output,
        "acquisizione_metadati": dict(migration) if migration else None,
    }


@app.post("/api/bandi/{code}/ente/bando-pdf")
async def importa_bando_pdf_ente(code: str, request: Request, file: UploadFile = File(...)):
    """Conserva l'originale e presenta EVIDENZE TESTUALI, senza assegnare riserve automaticamente.

    Il testo PDF non viene inviato a servizi esterni. Per PDF senza testo
    estraibile va effettuata una lettura documentale manuale.
    """
    require_admin(request)
    code=valid_code(code)
    filename=(file.filename or "").split("/")[-1].split("\\\\")[-1]
    if not filename.lower().endswith(".pdf"):
        fail("Caricare il bando ufficiale in formato PDF")
    content=await file.read(12*1024*1024+1)
    if not content or len(content)>12*1024*1024:
        fail("PDF vuoto o superiore al limite di 12 MB")
    try:
        reader=PdfReader(BytesIO(content))
        if len(reader.pages)>100:fail("Il PDF supera il limite di 100 pagine")
        evidence=[]
        for page_no,page in enumerate(reader.pages,start=1):
            text=page.extract_text() or ""
            for line_no,line in enumerate(text.splitlines(),start=1):
                line=" ".join(line.split())
                if re.search(r"(?<![A-Z])(SSN|AOUI|ASUIT|UNITN|SSP)(?![A-Z])",line,re.I):
                    evidence.append({"pagina":page_no,"riga":line_no,"testo":line[:450]})
                    if len(evidence)>=150:break
            if len(evidence)>=150:break
    except HTTPException:
        raise
    except Exception:
        fail("Impossibile estrarre il testo del PDF: verificare il documento",400)
    with connect() as db:
        if not db.execute("SELECT 1 FROM bandi WHERE codice=?",(code,)).fetchone():
            fail("Bando non trovato",404)
    folder=SOURCES / code
    folder.mkdir(parents=True,exist_ok=True)
    dest=folder / "bando_requisiti_originale.pdf"
    digest=hashlib.sha256(content).hexdigest()
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest()!=digest:
        fail("È già presente un bando PDF diverso: conservare la versione originale e verificare prima della sostituzione",409)
    if not dest.exists():
        dest.write_bytes(content)
    return {"ok":True,"file":filename[:150],"sha256":digest,"pagine":len(reader.pages),
            "evidenze":evidence,"avviso":
            "Le occorrenze sono indizi testuali, non requisiti automaticamente assegnati agli incarichi. Confermare ogni requisito con la tabella ufficiale."}


def _ollama_settings():
    try:
        options = json.loads((BASE / "options.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        options = {}
    endpoint = (options.get("ollama_url") or "").strip()
    if not endpoint:
        fail("Configurare Ollama locale nelle opzioni di RankUP", 503)
    return endpoint


@app.get("/api/ai/ollama/status")
def stato_ollama(request: Request):
    require_admin(request)
    try:
        return ai_ollama_status(_ollama_settings())
    except AIEmployerError as exc:
        fail(str(exc), 503)


# Il PDF ufficiale non viene analizzato da Ollama.
def tipo_cds_key(label: str | None) -> str:
    return " ".join(str(label or "").split()).casefold()


ENTE_AMMESSI = {"SSN", "AOUI", "ASUIT", "UNITN", "SSP",
                "ASUIT O UNITN", "NESSUNA RISERVA", "ALTRO ATENEO", "APSS"}


def _dizionario(db):
    return {r["chiave"]: dict(r) for r in
            db.execute("SELECT * FROM pica_tipo_cds_dizionario")}


def _tipologie(parsed):
    items = {}
    for record in parsed["rows"]:
        inc = record["incarico"]
        key = tipo_cds_key(inc.get("tipo_cds"))
        if not key:
            continue  # Valore assente: nessun requisito desumibile, non creare una regola.
        entry = items.setdefault(key, {"chiave": key, "dicitura": inc.get("tipo_cds") or "",
                                       "incarichi": set()})
        entry["incarichi"].add(inc["key"])
    return items


class VocePICA(BaseModel):
    chiave: str = Field(min_length=1, max_length=400)
    requisito: str = Field(min_length=1, max_length=50)
    motivo: str = Field(default="", max_length=500)


def _salva_dizionario(db, chiave: str, raw: str, requirement: str,
                       who: str, reason: str = "") -> None:
    if requirement not in ENTE_AMMESSI:
        fail("Requisito non riconosciuto")
    previous = db.execute("SELECT * FROM pica_tipo_cds_dizionario WHERE chiave=?",
                          (chiave,)).fetchone()
    if previous and previous["requisito"] == requirement:
        return
    if previous and not reason.strip():
        fail("Motivazione obbligatoria per cambiare una corrispondenza già approvata")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    source = ("Conferma amministratore da importazione PICA" if not previous
              else "Rettifica amministrativa: " + reason.strip())
    db.execute("""INSERT INTO pica_tipo_cds_dizionario
        (chiave,dicitura,requisito,fonte,autore,aggiornato_il)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(chiave) DO UPDATE SET
          requisito=excluded.requisito,fonte=excluded.fonte,
          autore=excluded.autore,aggiornato_il=excluded.aggiornato_il
    """, (chiave, raw, requirement, source, who, now))
    db.execute("""INSERT INTO pica_tipo_cds_eventi
        (chiave,precedente_json,successivo_json,autore,quando)
        VALUES(?,?,?,?,?)
    """, (chiave, json.dumps(dict(previous) if previous else {}, ensure_ascii=False),
          json.dumps({"dicitura": raw, "requisito": requirement, "fonte": source},
                     ensure_ascii=False), who, now))


@app.get("/api/pica/tipologie")
def elenco_tipologie_pica(request: Request):
    require_admin(request)
    with connect() as db:
        rows = db.execute("""SELECT * FROM pica_tipo_cds_dizionario
                             ORDER BY dicitura COLLATE NOCASE""").fetchall()
    return {"tipologie": [dict(r) for r in rows]}


@app.put("/api/pica/tipologie")
def aggiorna_tipologia_pica(body: VocePICA, request: Request):
    who = require_admin(request)["username"]
    key = tipo_cds_key(body.chiave)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT dicitura FROM pica_tipo_cds_dizionario WHERE chiave=?",
                              (key,)).fetchone()
        if not previous:
            fail("Dicitura sconosciuta: acquisirla attraverso l'anteprima PICA", 404)
        _salva_dizionario(db, key, previous["dicitura"], body.requisito,
                          who, body.motivo)
        db.commit()
    return {"ok": True}


class RequisitoEnteInput(BaseModel):
    ente_richiesto: str = Field(max_length=50)
    fonte: str = Field(min_length=1,max_length=500)


@app.put("/api/bandi/{code}/incarichi/{key}/ente")
def conferma_ente_incarico(code: str,key: str,body: RequisitoEnteInput,request: Request):
    """Eccezione manuale tracciata: prevale sempre sul dizionario PICA."""
    who=require_admin(request)
    code=valid_code(code)
    if not re.fullmatch(r"[0-9a-f]{24}",key):fail("Identificativo incarico non valido")
    ente=" ".join(body.ente_richiesto.upper().strip().split())
    if ente not in ENTE_AMMESSI:fail("Ente richiesto non riconosciuto")
    fonte=body.fonte.strip()
    if not fonte:fail("Indicare la fonte ufficiale del requisito")
    now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("""SELECT i.bando_id FROM incarichi i
                          JOIN bandi b ON b.id=i.bando_id
                          WHERE b.codice=? AND i.key=?""",(code,key)).fetchone()
        if not row:fail("Incarico non trovato",404)
        prior=db.execute("SELECT ente_richiesto,fonte FROM incarichi_ente_requisiti WHERE bando_id=? AND key_incarico=?",
                         (row["bando_id"],key)).fetchone()
        db.execute("""INSERT INTO incarichi_ente_requisiti
                 (bando_id,key_incarico,ente_richiesto,fonte,confermato_da,confermato_il)
                 VALUES(?,?,?,?,?,?)
                 ON CONFLICT(bando_id,key_incarico) DO UPDATE SET
                 ente_richiesto=excluded.ente_richiesto,fonte=excluded.fonte,
                 confermato_da=excluded.confermato_da,confermato_il=excluded.confermato_il
                 """,(row["bando_id"],key,ente,fonte,who["username"],now))
        db.execute("""INSERT INTO incarichi_ente_eventi
                    (bando_id,key_incarico,precedente_json,successivo_json,autore,quando)
                    VALUES(?,?,?,?,?,?)""",(
                    row["bando_id"],key,
                    json.dumps(dict(prior) if prior else {},ensure_ascii=False),
                    json.dumps({"ente_richiesto":ente,"fonte":fonte},ensure_ascii=False),
                    who["username"],now))
        db.commit()
    return {"ok":True,"key":key,"ente_richiesto":ente,"fonte":fonte}


@app.get("/api/bandi/{code}/candidature")
def list_candidature(code: str):
    code = valid_code(code)
    with connect() as db:
        rows = db.execute("""
           SELECT c.id_pica, c.cognome, c.nome, c.stato, c.source_json,
                  i.key AS incarico_key, i.cds, i.sede, i.insegnamento, i.modulo, i.tipo_cds,
                  er.ente_richiesto
           FROM candidature c
           JOIN incarichi i ON i.id=c.incarico_id
           JOIN bandi b ON b.id=c.bando_id
           LEFT JOIN incarichi_ente_requisiti er ON er.bando_id=b.id AND er.key_incarico=i.key
           WHERE b.codice=?
           ORDER BY i.cds COLLATE NOCASE, i.sede COLLATE NOCASE,
                    i.insegnamento COLLATE NOCASE, i.modulo COLLATE NOCASE,
                    c.cognome COLLATE NOCASE, c.nome COLLATE NOCASE
           LIMIT 10000
        """, (code,)).fetchall()
        dictionary = _dizionario(db)
    # Vecchie importazioni: recupera le dichiarazioni dal PICA originale
    # conservato nell'add-on, SENZA reimportare le candidature.
    employment={}
    folder=SOURCES / code
    for path in (folder / "pica_originale.xls",folder / "pica_originale.xlsx"):
        if not path.is_file():continue
        try:
            original=parse_pica(path.name,path.read_bytes())
            employment={(str(r["id_pica"]),r["incarico"]["key"]):r for r in original["rows"]}
        except (ValueError,OSError):
            employment={}
        break
    output=[]
    for row in rows:
        d=dict(row)
        try:
            record=json.loads(d.pop("source_json") or "{}")
        except (TypeError,ValueError):
            record={}
        original=employment.get((str(d["id_pica"]),d["incarico_key"]),{})
        candidate={name:(record.get(name) or original.get(name) or "") for name in
                   ("occupazione","azienda_ente","profilo_professionale")}
        d.update(candidate)
        if not d.get("ente_richiesto"):
            mapping = dictionary.get(tipo_cds_key(d.get("tipo_cds")))
            d["ente_richiesto"] = mapping["requisito"] if mapping else None
        d["verifica_ente"]=evaluate_ente(d.get("ente_richiesto"),**{
            "occupazione":candidate["occupazione"],
            "azienda":candidate["azienda_ente"],
            "profilo":candidate["profilo_professionale"]})
        output.append(d)
    return {"candidature":output}



@app.post("/api/bandi/{code}/valutazioni/{pdf_id}/ai/occupazione")
def valuta_occupazione_scheda_restituita(code: str, pdf_id: int, request: Request):
    """Controllo amministrativo soltanto DOPO la ricezione di una scheda.

    Solo Admin. Nessun PDF, nome, CF, ID PICA o esito della commissione
    viene trasmesso a Ollama. La risposta non modifica valutazioni o graduatorie.
    """
    require_admin(request)
    code = valid_code(code)
    if pdf_id <= 0:
        fail("Identificativo scheda non valido")
    with connect() as db:
        archived = db.execute("""
            SELECT c.id AS candidatura_id, c.id_pica, i.key AS incarico_key, v.bando_id
            FROM valutazioni_pdf v
            JOIN bandi b ON b.id=v.bando_id
            JOIN candidature c ON c.id=v.candidatura_id
            JOIN incarichi i ON i.id=c.incarico_id
            WHERE b.codice=? AND v.id=? AND v.candidatura_id IS NOT NULL
              AND v.stato IN ('Acquisita','Da verificare')
        """, (code, pdf_id)).fetchone()
    if not archived:
        fail("Scheda restituita non trovata o candidatura non associata", 404)
    records = list_candidature(code)["candidature"]
    matches = [r for r in records
               if str(r["id_pica"]) == str(archived["id_pica"])
               and r["incarico_key"] == archived["incarico_key"]]
    if len(matches) != 1:
        fail("Dichiarazioni occupazionali non associabili univocamente", 409)
    candidate = matches[0]
    requirement = candidate.get("ente_richiesto") or ""
    if not requirement:
        fail("Requisito ente non classificato: verificare il dizionario PICA", 409)
    if requirement == "NESSUNA RISERVA":
        fail("Nessuna riserva occupazionale da controllare", 400)
    try:
        result = review_occupation(
            requirement, candidate.get("occupazione") or "",
            candidate.get("azienda_ente") or "",
            candidate.get("profilo_professionale") or "",
            _ollama_settings())
    except AIEmployerError as exc:
        fail(str(exc), 503)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # Confermare il collegamento anche al momento della registrazione.
        if not db.execute("""SELECT 1 FROM valutazioni_pdf
            WHERE id=? AND bando_id=? AND candidatura_id=?
        """, (pdf_id, archived["bando_id"], archived["candidatura_id"])).fetchone():
            fail("Scheda variata: ripetere la verifica", 409)
        db.execute("""INSERT INTO valutazioni_occupazione_ai
            (bando_id,valutazione_pdf_id,candidatura_id,requisito_analizzato,
             tipo_cds_pica,occupazione_pica,azienda_pica,profilo_pica,
             stato,motivo,evidenza,modello_locale,autore,quando)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (archived["bando_id"], pdf_id, archived["candidatura_id"],
              requirement, str(candidate.get("tipo_cds") or ""),
              str(candidate.get("occupazione") or ""),
              str(candidate.get("azienda_ente") or ""),
              str(candidate.get("profilo_professionale") or ""),
              result["stato"], result["motivo"], result.get("evidenza") or "",
              1 if result.get("modello_locale") else 0,
              request_actor(), now))
        db.commit()
    return {"risultato": result, "registrato_il": now,
            "avviso": "Controllo riservato ad Admin, istruttorio e non vincolante. "
                      "Storico conservato; nessun punteggio o esito della commissione modificato."}




@app.get("/api/bandi/{code}/valutazioni/ai/occupazione/storico")
def storico_occupazione_ai(code: str, request: Request):
    """Soli amministratori vedono storico e risultati delle verifiche IA."""
    require_admin(request)
    code = valid_code(code)
    with connect() as db:
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not b:
            fail("Bando non trovato", 404)
        records = db.execute("""
            SELECT a.valutazione_pdf_id, a.requisito_analizzato,
                a.stato,a.motivo,a.evidenza,a.autore,a.quando,
                a.id AS audit_id
            FROM valutazioni_occupazione_ai a
            WHERE a.bando_id=?
            ORDER BY a.id DESC
        """, (b["id"],)).fetchall()
    latest = {}
    counts = {}
    for row in records:
        key = str(row["valutazione_pdf_id"])
        counts[key] = counts.get(key, 0) + 1
        if key not in latest:
            latest[key] = dict(row)
    for key, value in latest.items():
        value["numero_controlli"] = counts[key]
    return {"controlli": latest}


@app.get("/api/bandi/{code}/valutazioni/ai/occupazione/report.pdf")
def report_occupazione_cds(code: str, cds: str, request: Request, sede: str = ""):
    """Report per CdS, riservato Admin; soltanto controlli IA registrati."""
    require_admin(request)
    code = valid_code(code)
    cds = cds.strip()
    sede = sede.strip()
    if not cds or len(cds) > 250 or len(sede) > 100:
        fail("Selezionare un Corso di Studio valido", 400)
    with connect() as db:
        bando = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not bando:
            fail("Bando non trovato", 404)
        conditions = ["i.bando_id=?", "i.cds=?", "c.stato='Attiva'"]
        args = [bando["id"], cds]
        if sede:
            conditions.append("i.sede=?")
            args.append(sede)
        rows = db.execute("""
            SELECT c.id AS candidatura_id,c.id_pica,c.cognome,c.nome,
                i.cds,i.sede,i.insegnamento,i.modulo,i.tipo_cds,i.key,
                er.ente_richiesto AS requisito_manuale,
                v.id AS pdf_id,v.stato AS scheda_stato
            FROM candidature c JOIN incarichi i ON i.id=c.incarico_id
            LEFT JOIN incarichi_ente_requisiti er
              ON er.bando_id=i.bando_id AND er.key_incarico=i.key
            LEFT JOIN valutazioni_pdf v ON v.id=(
                SELECT vv.id FROM valutazioni_pdf vv
                WHERE vv.candidatura_id=c.id AND vv.bando_id=i.bando_id
                ORDER BY CASE WHEN vv.attiva=1 THEN 0 ELSE 1 END, vv.id DESC
                LIMIT 1)
            WHERE """+" AND ".join(conditions)+"""
            ORDER BY i.sede COLLATE NOCASE, i.insegnamento COLLATE NOCASE,
                     i.modulo COLLATE NOCASE, c.cognome COLLATE NOCASE,
                     c.nome COLLATE NOCASE, c.id
        """, args).fetchall()
        dictionary = _dizionario(db)
        latest = {}
        for record in rows:
            pdf_id = record["pdf_id"]
            if pdf_id is not None and pdf_id not in latest:
                audit = db.execute("""
                    SELECT requisito_analizzato,stato,motivo,evidenza,autore,quando
                    FROM valutazioni_occupazione_ai WHERE valutazione_pdf_id=?
                    ORDER BY id DESC LIMIT 1
                """, (pdf_id,)).fetchone()
                latest[pdf_id] = dict(audit) if audit else None
    if not rows:
        fail("Nessuna candidatura attiva per il CdS e la sede selezionati", 404)
    candidates = {
        (str(x["id_pica"]), x["incarico_key"]): x
        for x in list_candidature(code)["candidature"]
    }
    entries = []
    for record in rows:
        item = dict(record)
        candidate = candidates.get((str(item["id_pica"]), item["key"]), {})
        match = dictionary.get(tipo_cds_key(item["tipo_cds"]))
        item["requisito"] = (item["requisito_manuale"] or
                            (match["requisito"] if match else None))
        for field in ("occupazione", "azienda_ente", "profilo_professionale"):
            item[field] = candidate.get(field) or ""
        item["ai"] = latest.get(item["pdf_id"])
        entries.append(item)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = report_cds_pdf(code, cds, entries, now)
    filename = "RankUP_" + _safe_filename(code, 40) + "_occupazione_" + _safe_filename(cds, 55) + ".pdf"
    return Response(content=report, media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="' + filename + '"',
                 "Cache-Control": "no-store"})


@app.post("/api/pica/preview")
async def preview_pica(request: Request, file: UploadFile = File(...)):
    require_admin(request)
    filename = (file.filename or "").split("/")[-1].split("\\")[-1]
    if not filename.lower().endswith((".xls", ".xlsx")):
        fail("Carica un file Excel PICA .xls o .xlsx")
    content = await file.read(MAX_FILE_BYTES + 1)
    try:
        parsed = parse_pica(filename, content)
    except Exception as error:
        if isinstance(error, ValueError):
            fail(str(error))
        # Evitare di esporre a browser dettagli di parser/file.
        fail("Impossibile leggere il file PICA: verifica che sia un Excel valido")
    with connect() as db:
        exists = db.execute("SELECT stato FROM bandi WHERE codice=?", (parsed["code"],)).fetchone()
        dictionary = _dizionario(db)
    distinct = _tipologie(parsed)
    typelist = [
        {"chiave": key, "dicitura": info["dicitura"],
         "incarichi": len(info["incarichi"]),
         "requisito": dictionary[key]["requisito"] if key in dictionary else "",
         "nuova": key not in dictionary}
        for key, info in sorted(distinct.items())]
    if exists and exists["stato"]!="In preparazione":
        fail("Il bando " + parsed["code"] + " è già stato importato. Nessuna sovrascrittura automatica.", 409)
    token = secrets.token_urlsafe(24)
    metadata = {"parsed": parsed, "filename": filename, "sha256": hashlib.sha256(content).hexdigest()}
    (PENDING / (token + ".json")).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (PENDING / (token + ".input")).write_bytes(content)
    return {"token": token, "codice_bando": parsed["code"],
            "titolo_pica": parsed["source_title"], "riepilogo": parsed["summary"],
            "file": filename, "gia_presente": False, "tipologie": typelist}


class CommitRequest(BaseModel):
    token: str
    tipologie: list[VocePICA] = Field(default_factory=list)


@app.post("/api/pica/commit")
def commit_pica(body: CommitRequest, request: Request):
    who = require_admin(request)["username"]
    token = body.token
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", token):
        fail("Token importazione non valido")
    metadata_path = PENDING / (token + ".json")
    source_path = PENDING / (token + ".input")
    if not metadata_path.exists() or not source_path.exists():
        fail("Anteprima scaduta o non disponibile", 404)
    if time.time() - metadata_path.stat().st_mtime > 60 * 60:
        metadata_path.unlink(missing_ok=True)
        source_path.unlink(missing_ok=True)
        fail("Anteprima scaduta: ripeti l'importazione", 410)
    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    parsed = meta["parsed"]
    code = valid_code(parsed["code"])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            mapping = _dizionario(db)
            distinct = _tipologie(parsed)
            supplied = {}
            for row in body.tipologie:
                key = tipo_cds_key(row.chiave)
                if key not in distinct or key in supplied:
                    fail("Dicitura PICA duplicata o non presente nel file")
                supplied[key] = row
            unresolved = [x for x in distinct if x not in mapping and x not in supplied]
            if unresolved:
                fail("Classificare tutte le nuove diciture della colonna L prima dell'importazione")
            for key, choice in supplied.items():
                previous = mapping.get(key)
                if previous and previous["requisito"] != choice.requisito and not choice.motivo.strip():
                    fail("Motivare la rettifica: " + distinct[key]["dicitura"])
                _salva_dizionario(db, key, distinct[key]["dicitura"],
                                  choice.requisito, who, choice.motivo)
            existing=db.execute("SELECT id,stato FROM bandi WHERE codice=?",(code,)).fetchone()
            if existing:
                if existing["stato"]!="In preparazione":
                    fail("Candidature già importate: nessuna sovrascrittura",409)
                if db.execute("SELECT 1 FROM candidature WHERE bando_id=?",(existing["id"],)).fetchone():
                    fail("Il bando contiene già candidature",409)
                if db.execute("SELECT 1 FROM incarichi WHERE bando_id=?",(existing["id"],)).fetchone():
                    fail("Il bando contiene già incarichi: importazione PICA non automatica",409)
                bando_id=existing["id"]
                db.execute("""UPDATE bandi SET stato='Candidature importate',
                    source_filename=?,source_hash=?,import_at=? WHERE id=?""",
                    (meta["filename"],meta["sha256"],now,bando_id))
            else:
                cur = db.execute("""
                    INSERT INTO bandi(codice, created_at, source_filename, source_hash, import_at)
                    VALUES(?,?,?,?,?)
                """, (code, now, meta["filename"], meta["sha256"], now))
                bando_id = cur.lastrowid
            assignment_ids = {}
            for record in parsed["rows"]:
                item = record["incarico"]
                key = item["key"]
                if key not in assignment_ids:
                    details = incarico_extra_db(item)
                    columns = (
                        "bando_id", "key", "cds", "sede", "insegnamento",
                        "modulo", "ul", "codice_copertura", "id_copertura",
                        "ssd", *EXTRA_DB_FIELDS
                    )
                    values = (
                        bando_id, key, item["cds"], item["sede"],
                        item["insegnamento"], item["modulo"], item["ul"],
                        item["codice_copertura"], item["id_copertura"],
                        item.get("ssd", "").strip().upper(),
                        *(details[field] for field in EXTRA_DB_FIELDS)
                    )
                    cur = db.execute(
                        "INSERT INTO incarichi (" + ",".join(columns) + ") VALUES (" +
                        ",".join("?" for _ in columns) + ")", values
                    )
                    assignment_ids[key] = cur.lastrowid
                db.execute("""
                    INSERT INTO candidature(bando_id,incarico_id,id_pica,nome,cognome,
                                            codice_fiscale,stato_pica,source_row,source_json)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (bando_id, assignment_ids[key], record["id_pica"], record["nome"],
                      record["cognome"], record["codice_fiscale"], record["stato_pica"],
                      record["source_row"], json.dumps(record, ensure_ascii=False)))
            distinct_metadata = {}
            for record in parsed["rows"]:
                inc = record["incarico"]
                distinct_metadata[inc["key"]] = incarico_extra_db(inc)
            metadata_count = sum(
                1 for info in distinct_metadata.values()
                for value in info.values() if value
            )
            db.execute("""
                INSERT INTO incarichi_metadata_migrations
                (bando_id,source_sha256,metodo,incarichi_verificati,
                 incarichi_aggiornati,campi_compilati,aggiornato_il)
                VALUES(?,?,?,?,?,?,?)
            """, (bando_id, meta["sha256"], "importazione",
                  len(assignment_ids), 0, metadata_count, now))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            fail("Importazione già esistente oppure dati duplicati: nessun dato sovrascritto", 409)
        except Exception:
            db.rollback()
            raise
    destination = SOURCES / code
    destination.mkdir(parents=True, exist_ok=True)
    extension = ".xlsx" if meta["filename"].lower().endswith(".xlsx") else ".xls"
    shutil.copy2(source_path, destination / ("pica_originale" + extension))
    metadata_path.unlink(missing_ok=True)
    source_path.unlink(missing_ok=True)
    return {"ok": True, "codice_bando": code, "riepilogo": parsed["summary"]}



class CommissioneInput(BaseModel):
    cds: str = Field(min_length=1, max_length=250)
    sede: str = Field(min_length=1, max_length=100)
    presidente: str = Field(min_length=1, max_length=250)
    componenti: str = Field(default="", max_length=2000)
    requisiti: str = Field(default="", max_length=2000)
    anno: str = Field(default="", max_length=30)
    expected_updated_at: str | None = None


def _commission_fields(row: dict) -> dict[str, str]:
    keys = ("cds", "sede", "presidente", "componenti", "requisiti", "anno")
    result = {key: str(row.get(key) or "").strip() for key in keys}
    if not all(result[key] for key in ("cds", "sede", "presidente")):
        fail("CdS, sede e presidente sono obbligatori")
    bounds = {"cds": 250, "sede": 100, "presidente": 250,
              "componenti": 2000, "requisiti": 2000, "anno": 30}
    for key, limit in bounds.items():
        if len(result[key]) > limit:
            fail("Campo commissione troppo lungo: " + key)
    return result


def _match_commission_row(rows, cds: str, sede: str):
    matches = [
        row for row in rows
        if course_key(row["cds"]) == course_key(cds)
        and seat_key(row["sede"]) == seat_key(sede)
    ]
    if len(matches) > 1:
        fail("Commissioni duplicate per la stessa sede dopo normalizzazione: verificare i dati", 409)
    return matches[0] if matches else None


def _validate_commission_course(db, bando_id: int, values: dict):
    groups = db.execute(
        "SELECT DISTINCT cds,sede FROM incarichi WHERE bando_id=?", (bando_id,)
    ).fetchall()
    if not any(
        course_key(x["cds"]) == course_key(values["cds"])
        and seat_key(x["sede"]) == seat_key(values["sede"])
        for x in groups
    ):
        fail("Il CdS e la sede non risultano negli incarichi PICA del bando: verificare i dati")


def _log_commission_change(db, bando_id: int, commission_id: int, action: str,
                           before: dict | None, after: dict):
    db.execute("""
        INSERT INTO commissioni_eventi
        (bando_id,commissione_id,azione,precedente_json,successivo_json,autore,quando)
        VALUES(?,?,?,?,?,?,?)
    """, (
        bando_id, commission_id, action,
        json.dumps(before, ensure_ascii=False) if before is not None else None,
        json.dumps(after, ensure_ascii=False), request_actor(),
        datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    ))


def _insert_commission(db, bando_id: int, values: dict, action: str) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    cursor = db.execute("""
        INSERT INTO commissioni
        (bando_id,cds,sede,presidente,componenti,requisiti,anno,updated_at)
        VALUES(?,?,?,?,?,?,?,?)
    """, (
        bando_id, values["cds"], values["sede"], values["presidente"],
        values["componenti"], values["requisiti"], values["anno"], now,
    ))
    _log_commission_change(db, bando_id, cursor.lastrowid, action, None, values)
    return cursor.lastrowid


def _update_commission(db, bando_id: int, original: dict, values: dict,
                       action: str, keep_blank: bool = True) -> bool:
    keys = ("cds", "sede", "presidente", "componenti", "requisiti", "anno")
    updated = {
        key: values[key] if keep_blank or values[key] else original[key]
        for key in keys
    }
    previous = {key: original[key] for key in keys}
    if updated == previous:
        return False
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    db.execute("""
      UPDATE commissioni
      SET cds=?,sede=?,presidente=?,componenti=?,requisiti=?,anno=?,updated_at=?
      WHERE id=? AND bando_id=?
    """, (
        updated["cds"], updated["sede"], updated["presidente"],
        updated["componenti"], updated["requisiti"], updated["anno"],
        now, original["id"], bando_id,
    ))
    _log_commission_change(db, bando_id, original["id"], action, previous, updated)
    return True


@app.post("/api/bandi/{code}/commissioni/manuale")
def add_commissione(code: str, body: CommissioneInput):
    code = valid_code(code)
    values = _commission_fields(body.model_dump())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not b:
            fail("Bando non trovato", 404)
        _validate_commission_course(db, b["id"], values)
        current = db.execute("SELECT * FROM commissioni WHERE bando_id=?", (b["id"],)).fetchall()
        if _match_commission_row(current, values["cds"], values["sede"]) is not None:
            fail("La commissione per questo CdS/sede esiste già: usa Modifica", 409)
        commission_id = _insert_commission(db, b["id"], values, "inserimento_manuale")
        db.commit()
    return {"ok": True, "id": commission_id, "action": "inserita"}


@app.put("/api/bandi/{code}/commissioni/{commission_id}")
def edit_commissione(code: str, commission_id: int, body: CommissioneInput):
    code = valid_code(code)
    if commission_id <= 0:
        fail("Identificativo commissione non valido")
    if not body.expected_updated_at:
        fail("Ricaricare la commissione prima di modificarla", 409)
    values = _commission_fields(body.model_dump())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not b:
            fail("Bando non trovato", 404)
        old = db.execute(
            "SELECT * FROM commissioni WHERE id=? AND bando_id=?",
            (commission_id, b["id"]),
        ).fetchone()
        if not old:
            fail("Commissione non trovata", 404)
        if old["updated_at"] != body.expected_updated_at:
            fail("La commissione è stata modificata da un altro operatore: aggiorna la pagina", 409)
        _validate_commission_course(db, b["id"], values)
        others = db.execute(
            "SELECT * FROM commissioni WHERE bando_id=? AND id<>?",
            (b["id"], commission_id),
        ).fetchall()
        if _match_commission_row(others, values["cds"], values["sede"]) is not None:
            fail("Esiste già una commissione per questo CdS/sede", 409)
        changed = _update_commission(db, b["id"], dict(old), values, "modifica_manuale")
        db.commit()
    return {"ok": True, "modificata": changed,
            "avviso": "Rigenerare le schede PDF già scaricate per il CdS interessato" if changed else ""}


@app.get("/api/bandi/{code}/commissioni")
def list_commissioni(code: str):
    code = valid_code(code)
    with connect() as db:
        rows = db.execute("""
            SELECT cm.id, cm.cds, cm.sede, cm.presidente, cm.componenti,
                   cm.requisiti, cm.anno, cm.updated_at
            FROM commissioni cm JOIN bandi b ON cm.bando_id=b.id
            WHERE b.codice=? ORDER BY cm.cds COLLATE NOCASE, cm.sede COLLATE NOCASE
        """, (code,)).fetchall()
    return {"commissioni": [dict(x) for x in rows]}


@app.get("/api/bandi/{code}/commissioni/associazioni")
def associazioni_commissioni(code: str):
    """Report per CdS/sede: confronto verificabile, nessuna modifica ai dati."""
    code = valid_code(code)
    with connect() as db:
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if b is None:
            fail("Bando non trovato", 404)
        assignments = db.execute("""
            SELECT cds,sede,COUNT(*) AS incarichi
            FROM incarichi WHERE bando_id=?
            GROUP BY cds,sede
            ORDER BY cds COLLATE NOCASE,sede COLLATE NOCASE
        """, (b["id"],)).fetchall()
        commissions = db.execute("""
            SELECT cds,sede,presidente,componenti,anno,requisiti
            FROM commissioni WHERE bando_id=?
        """, (b["id"],)).fetchall()
    index = commission_index(commissions)
    result=[]
    for item in assignments:
        status, commission = resolve_commission(index, item["cds"], item["sede"])
        if status == "associata" and (
            not commission.get("presidente") or not commission.get("componenti")
        ):
            status = "incompleta"
        result.append({
            "cds": item["cds"],
            "sede": item["sede"],
            "incarichi": item["incarichi"],
            "stato": status,
            "cds_commissione": commission["cds"] if commission else None,
            "sede_commissione": commission["sede"] if commission else None,
        })
    return {
        "totale": len(result),
        "associate": sum(x["stato"] == "associata" for x in result),
        "mancanti": sum(x["stato"] == "mancante" for x in result),
        "ambigue": sum(x["stato"] == "ambigua" for x in result),
        "incomplete": sum(x["stato"] == "incompleta" for x in result),
        "associazioni": result,
    }



@app.get("/api/bandi/{code}/commissioni/modello")
def download_modello_commissioni(code: str, tipo: str = "cds_sedi"):
    code = valid_code(code)
    if tipo not in ("vuoto", "cds_sedi", "esistenti"):
        fail("Tipo di modello non riconosciuto")
    with connect() as db:
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not b:
            fail("Bando non trovato", 404)
        if tipo == "vuoto":
            values = []
        elif tipo == "cds_sedi":
            values = [
                {"cds": r["cds"], "sede": r["sede"]}
                for r in db.execute("""
                    SELECT cds,sede FROM incarichi WHERE bando_id=?
                    GROUP BY cds,sede ORDER BY cds COLLATE NOCASE,sede COLLATE NOCASE
                """, (b["id"],)).fetchall()
            ]
        else:
            values = [
                dict(r) for r in db.execute("""
                    SELECT cds,sede,presidente,componenti,requisiti,anno
                    FROM commissioni WHERE bando_id=?
                    ORDER BY cds COLLATE NOCASE,sede COLLATE NOCASE
                """, (b["id"],)).fetchall()
            ]
    data = make_commission_template(code, values)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="RankUp_' +
                                   code + '_commissioni_' + tipo + '.xlsx"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/bandi/{code}/commissioni/import")
async def import_commissioni(
    code: str, file: UploadFile = File(...), mode: str = Form("solo_nuove")
):
    """Import incrementale: non cancellare righe non presenti nel file.

    Le commissioni già presenti non vengono sovrascritte salvo scelta
    esplicita aggiorna; in quel caso i campi vuoti dell'Excel non
    eliminano informazioni già registrate.
    """
    code = valid_code(code)
    if mode not in ("solo_nuove", "aggiorna"):
        fail("Modalità importazione non valida")
    filename = (file.filename or "").split("/")[-1].split("\\")[-1]
    content = await file.read(MAX_COMMISSION_FILE + 1)
    try:
        parsed = parse_commissioni(filename, content, code)
    except ValueError as error:
        fail(str(error))
    except Exception:
        fail("Impossibile leggere l'Excel delle commissioni")
    to_add = to_update = skipped = 0
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not b:
            fail("Bando non trovato", 404)
        all_existing = db.execute(
            "SELECT * FROM commissioni WHERE bando_id=?", (b["id"],)
        ).fetchall()
        # L'Excel ufficiale puo' comprendere commissioni di CdS/sedi
        # senza candidature in questo specifico bando: le conserviamo.
        # Solo l'associazione alle schede richiede corrispondenza PICA.
        for item in parsed["rows"]:
            values = _commission_fields(item)
            previous = _match_commission_row(all_existing, values["cds"], values["sede"])
            if previous:
                if mode == "solo_nuove":
                    skipped += 1
                    continue
                if _update_commission(
                    db, b["id"], dict(previous), values, "aggiornamento_excel",
                    keep_blank=False,
                ):
                    to_update += 1
                else:
                    skipped += 1
            else:
                row_id = _insert_commission(db, b["id"], values, "importazione_excel")
                all_existing.append(dict(values, id=row_id))
                to_add += 1
        db.commit()
    # Conservare tutti i file importati, senza sovrascrivere l'originale.
    destination = SOURCES / code
    destination.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha256(content).hexdigest()[:16]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    (destination / ("commissioni_import_" + timestamp + "_" + checksum + ".xlsx")).write_bytes(content)
    return {
        "ok": True, "aggiunte": to_add, "aggiornate": to_update,
        "gia_presenti_o_inalterate": skipped,
        "righe_da_compilare_omesse": parsed.get("righe_da_compilare_omesse", 0),
        "avviso": "Rigenerare le schede PDF dei CdS interessati in caso di modifiche",
    }


class ClassificazioneSSD(BaseModel):
    tipo: str
    riferimento: str = ""


@app.get("/api/bandi/{code}/ssd")
def elenco_ssd_bando(code: str):
    code = valid_code(code)
    with connect() as db:
        rows = db.execute("""
           SELECT UPPER(TRIM(i.ssd)) AS ssd,
                  CASE WHEN UPPER(TRIM(i.ssd)) IN ('', 'ND', 'N-D', 'NON-INDICATO')
                       THEN NULL
                       WHEN UPPER(TRIM(i.ssd))='NN' THEN 'ordinario'
                       ELSE COALESCE(cl.tipo, 'ordinario') END AS tipo,
                  cl.tipo AS tipo_salvato, cl.riferimento,
                  COUNT(i.id) AS incarichi,
                  COUNT(DISTINCT i.cds) AS cds
           FROM incarichi i JOIN bandi b ON b.id=i.bando_id
           LEFT JOIN ssd_classificazioni cl ON cl.ssd=UPPER(TRIM(i.ssd))
           WHERE b.codice=?
           GROUP BY UPPER(TRIM(i.ssd))
           ORDER BY UPPER(TRIM(i.ssd))
        """, (code,)).fetchall()
        if not db.execute("SELECT 1 FROM bandi WHERE codice=?", (code,)).fetchone():
            fail("Bando non trovato", 404)
        migration = db.execute("""
            SELECT m.incarichi_verificati,m.incarichi_corretti,m.aggiornato_il
            FROM ssd_import_migrations m
            JOIN bandi b ON b.id=m.bando_id
            WHERE b.codice=?
        """, (code,)).fetchone()
    return {"ssd": [dict(x) for x in rows],
            "migrazione": dict(migration) if migration else None}


@app.put("/api/ssd/{ssd:path}/classificazione")
def assegna_classificazione(ssd: str, body: ClassificazioneSSD):
    sector = ssd.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+(?:[-/][A-Z0-9]+)*", sector) or len(sector)>32:
        fail("Codice SSD non valido")
    if sector in ("NN", "ND", "N-D", "NON-INDICATO"):
        fail("PICA non attribuisce un SSD specifico a questo incarico: verificare la copertura prima di associare un modello PDF")
    if body.tipo not in ("professionalizzante", "ordinario"):
        fail("Selezionare professionalizzante oppure ordinario")
    reference = body.riferimento.strip()
    if len(reference)>400:
        fail("Il riferimento facoltativo non può superare 400 caratteri")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        if not db.execute("SELECT 1 FROM incarichi WHERE UPPER(TRIM(ssd))=? LIMIT 1",(sector,)).fetchone():
            fail("SSD non trovato nei bandi importati", 404)
        db.execute("""
            INSERT INTO ssd_classificazioni(ssd,tipo,riferimento,aggiornato_il)
            VALUES(?,?,?,?)
            ON CONFLICT(ssd) DO UPDATE SET tipo=excluded.tipo,
                riferimento=excluded.riferimento, aggiornato_il=excluded.aggiornato_il
        """, (sector, body.tipo, reference, now))
    return {"ssd": sector, "tipo": body.tipo, "riferimento": reference}


def _safe_filename(value: str, maxlen=65) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_")
    return result[:maxlen] or "non_indicato"


def _candidate_pdf_filename(record: dict) -> str:
    """Nome leggibile per la commissione; l'identità tecnica resta nel PDF.

    La chiave finale identifica l'incarico: uno stesso ID PICA può
    partecipare a più insegnamenti nel medesimo bando.
    """
    def readable(value: str, limit: int) -> str:
        plain = unicodedata.normalize("NFKD", str(value or ""))
        plain = "".join(char for char in plain if not unicodedata.combining(char))
        return _safe_filename(plain, limit)

    return ("_".join((
        readable(record["id_pica"], 48),
        readable(record["cognome"], 26),
        readable(record["nome"], 26),
        record["key"][:10],
    )) + ".pdf")



@app.get("/api/bandi/{code}/schede/zip")
def esporta_schede_pdf(code: str, cds: str = "", sede: str = ""):
    """Genera gli AcroForm singoli e lo ZIP, solo se i settori sono classificati.

    Questo generatore usa fac-simile tecnici da validare sulle schede ufficiali
    prima dell'uso amministrativo.
    """
    code = valid_code(code)
    with connect() as db:
        bando = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not bando:
            fail("Bando non trovato", 404)
        conditions=["b.id=?","c.stato='Attiva'"]
        params=[bando["id"]]
        if cds:
            conditions.append("i.cds=?")
            params.append(cds)
        if sede:
            conditions.append("i.sede=?")
            params.append(sede)
        rows=db.execute("""
          SELECT c.id_pica,c.cognome,c.nome,c.codice_fiscale,c.stato,
                 i.key,i.cds,i.sede,i.insegnamento,i.modulo,i.ul,i.ssd,i.tipo_cds,
                 CASE WHEN UPPER(TRIM(i.ssd)) IN ('', 'ND', 'N-D', 'NON-INDICATO')
                      THEN NULL
                      WHEN UPPER(TRIM(i.ssd))='NN' THEN 'ordinario'
                      ELSE COALESCE(cl.tipo, 'ordinario') END AS tipo_ssd
          FROM candidature c
          JOIN incarichi i ON i.id=c.incarico_id
          JOIN bandi b ON b.id=c.bando_id
          LEFT JOIN ssd_classificazioni cl ON cl.ssd=UPPER(TRIM(i.ssd))
          WHERE """+" AND ".join(conditions)+"""
          ORDER BY i.cds COLLATE NOCASE, i.sede COLLATE NOCASE,
                   i.insegnamento COLLATE NOCASE, i.modulo COLLATE NOCASE,
                   c.cognome COLLATE NOCASE,c.nome COLLATE NOCASE
        """,params).fetchall()
        commission_rows = db.execute("""
            SELECT cds,sede,presidente,componenti,anno,requisiti
            FROM commissioni WHERE bando_id=?
        """, (bando["id"],)).fetchall()
        bando_ref_row = db.execute("""
            SELECT repertorio,protocollo,data_avviso,anno_accademico
            FROM bandi_riferimenti WHERE bando_id=?
        """, (bando["id"],)).fetchone()
    bando_ref = dict(bando_ref_row) if bando_ref_row else {}
    notice_ref = bando_notice_reference(bando_ref)
    if not rows:
        fail("Nessuna candidatura attiva trovata per la selezione")
    if len(rows)>5000:
        fail("Numero schede troppo elevato: selezionare uno o piu CdS")
    unclassified=sorted({r["ssd"] or "(SSD mancante)" for r in rows if not r["tipo_ssd"]})
    if unclassified:
        fail("SSD non identificato nel PICA; verificare l'incarico prima della generazione: "+", ".join(unclassified[:20]))
    # Il file PICA usa spesso la denominazione estesa "(ABILITANTE ...)".
    # Usare soltanto il nome canonico CdS + sede. Non applicare somiglianze
    # parziali e non ereditare una commissione da un'altra sede.
    # La stessa valutazione istruttoria gia esposta in "Candidature"
    # accompagna la scheda del candidato. Non invocare Ollama per creare PDF,
    # non usare i suoi esiti come decisione e non variare le schede archiviate.
    verification = {
        (str(r["id_pica"]), r["incarico_key"]): r
        for r in list_candidature(code)["candidature"]
    }
    index = commission_index(commission_rows)
    associated = []
    missing = set()
    ambiguous = set()
    for row in rows:
        status, commission = resolve_commission(index, row["cds"], row["sede"])
        if (status != "associata" or not commission
                or not commission.get("presidente") or not commission.get("componenti")):
            label = row["cds"] + " / " + row["sede"]
            (ambiguous if status == "ambigua" else missing).add(label)
            continue
        record = dict(row)
        selected = verification.get((str(record["id_pica"]), record["key"]), {})
        requested = selected.get("ente_richiesto") or ""
        preliminary = selected.get("verifica_ente") or {}
        # Il punto di attenzione permane anche con dichiarazioni plausibili:
        # il rapporto di lavoro attivo deve comunque essere verificato.
        if requested and requested != "NESSUNA RISERVA":
            record["attenzione_occupazione"] = (
                "ATTENZIONE - Verificare occupazione e appartenenza: " +
                requested + " (PICA: " +
                str(preliminary.get("stato") or "DA VERIFICARE") + ")"
            )
            record["attenzione_motivo"] = str(preliminary.get("motivo") or "")
        elif not requested:
            record["attenzione_occupazione"] = (
                "ATTENZIONE - Requisito di appartenenza non classificato [DA VERIFICARE]"
            )
            record["attenzione_motivo"] = "Verificare la dicitura originale PICA e il bando."
        else:
            record["attenzione_occupazione"] = ""
            record["attenzione_motivo"] = ""
        record.update({
            "presidente": commission["presidente"],
            "componenti": commission["componenti"],
            "anno": bando_ref.get("anno_accademico") or commission["anno"],
            # Il riferimento del bando, se verificato e completo, prevale
            # soltanto nella casella dell'avviso. I requisiti CdS importati
            # restano conservati nel database delle commissioni.
            "requisiti": notice_ref or commission["requisiti"],
            "requisiti_specifici": commission["requisiti"],
        })
        associated.append(record)
    if missing or ambiguous:
        messages = []
        if missing:
            messages.append("Commissioni assenti o non corrispondenti per CdS/sede: " +
                            "; ".join(sorted(missing)[:15]))
        if ambiguous:
            messages.append("Commissioni con associazione ambigua: " +
                            "; ".join(sorted(ambiguous)[:15]))
        fail(" | ".join(messages))
    buffer=BytesIO()
    with zipfile.ZipFile(buffer,"w",compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for row in associated:
            record=dict(row)
            record["bando"]=code
            record["scheda_id"]="RU-"+code+"-"+record["id_pica"]+"-"+record["key"][:10]
            try:
                pdf=make_scheda(record,record["tipo_ssd"])
            except (ValueError, TypeError) as exc:
                fail("Errore generazione PDF per ID PICA "+record["id_pica"]+": "+str(exc))
            path=(_safe_filename(record["cds"])+"/"+
                  _safe_filename(record["sede"])+"/"+
                  _candidate_pdf_filename(record))
            archive.writestr(path,pdf)
    filename = "RankUp_"+code+"_schede"
    if cds:
        filename += "_"+_safe_filename(cds,30)
    return Response(content=buffer.getvalue(),media_type="application/zip",headers={
        "Content-Disposition":'attachment; filename="'+filename+'.zip"',
        "Cache-Control":"no-store",
    })


# === Restituzione delle schede PDF: anteprima e conferma separata ===
class ConfermaValutazioni(BaseModel):
    token: str = Field(min_length=24, max_length=80)
    conferma_rettifiche: bool = False


def _val_context(db: sqlite3.Connection, bando_id: int) -> tuple[list[dict], dict]:
    candidates = [
        dict(r) for r in db.execute("""
            SELECT c.id,c.id_pica,c.nome,c.cognome,c.codice_fiscale,c.stato,
                   i.key,i.cds,i.sede,i.insegnamento,i.modulo,
                   CASE WHEN UPPER(TRIM(i.ssd)) IN ('', 'ND', 'N-D', 'NON-INDICATO')
                     THEN NULL
                     WHEN UPPER(TRIM(i.ssd))='NN' THEN 'ordinario'
                     ELSE COALESCE(cl.tipo,'ordinario') END AS modello
            FROM candidature c JOIN incarichi i ON i.id=c.incarico_id
            LEFT JOIN ssd_classificazioni cl ON cl.ssd=UPPER(TRIM(i.ssd))
            WHERE c.bando_id=?
        """, (bando_id,))
    ]
    pdf_rows = db.execute(
        "SELECT sha256,candidatura_id,versione FROM valutazioni_pdf WHERE bando_id=?",
        (bando_id,)
    ).fetchall()
    active = db.execute("""
        SELECT candidatura_id,versione,sha256 FROM valutazioni_pdf
        WHERE bando_id=? AND stato='Acquisita' AND attiva=1
    """, (bando_id,)).fetchall()
    return candidates, {
        "hashes": {r["sha256"] for r in pdf_rows},
        "hash_owner": {r["sha256"]:r["candidatura_id"] for r in pdf_rows},
        "active": {r["candidatura_id"]: dict(r) for r in active}
    }


def _pdf_batch_read(uploads: list[UploadFile]) -> list[tuple[str,bytes]]:
    if not uploads or len(uploads) > MAX_BATCH_FILES:
        fail("Seleziona da 1 a 120 PDF, oppure un archivio ZIP", 400)
    output=[]
    total=0
    for upload in uploads:
        name = str(upload.filename or "").replace("\\","/").rsplit("/",1)[-1][:150]
        if not name:
            fail("Nome del file non valido")
        data = upload.file.read(MAX_BATCH_BYTES + 1)
        total += len(data)
        if total > MAX_BATCH_BYTES:
            fail("Caricamento superiore al limite complessivo di 40 MB")
        if name.lower().endswith(".pdf"):
            output.append((name,data))
        elif name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(BytesIO(data)) as package:
                    for info in package.infolist():
                        if info.is_dir():
                            continue
                        # Gli ZIP prodotti da Finder su macOS possono contenere
                        # metadati AppleDouble, che non sono schede della commissione.
                        parts=info.filename.replace("\\","/").split("/")
                        basename=parts[-1]
                        if (parts[0].upper()=="__MACOSX" or
                                basename in (".DS_Store","Thumbs.db")):
                            continue
                        if not info.filename.lower().endswith(".pdf"):
                            fail("Lo ZIP deve contenere esclusivamente schede PDF")
                        if info.file_size > MAX_PDF_BYTES:
                            fail("Una scheda PDF nello ZIP supera 5 MB")
                        total += info.file_size
                        if total > MAX_BATCH_BYTES:
                            fail("Dimensione dei PDF decompressi superiore a 40 MB")
                        filename = info.filename.replace("\\","/").rsplit("/",1)[-1][:150]
                        with package.open(info) as source:
                            payload = source.read(MAX_PDF_BYTES+1)
                        output.append((filename,payload))
                        if len(output) > MAX_BATCH_FILES:
                            fail("Troppi PDF: limite 120 schede per caricamento")
            except (zipfile.BadZipFile, RuntimeError, OSError):
                fail("Archivio ZIP non leggibile")
        else:
            fail("Sono consentiti soltanto file PDF o ZIP")
        if len(output)>MAX_BATCH_FILES:
            fail("Troppi PDF: limite 120 schede per caricamento")
    if not output:
        fail("Nessuna scheda PDF trovata nel caricamento")
    return output


@app.post("/api/bandi/{code}/valutazioni/anteprima")
async def preview_valutazioni(code: str, files: list[UploadFile] = File(...),
                             cds: str = "", sede: str = ""):
    code = valid_code(code)
    with connect() as db:
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if b is None:
            fail("Bando non trovato",404)
        candidates, already = _val_context(db,b["id"])
    uploads = _pdf_batch_read(files)
    # Le anteprime non confermate scadono dopo un'ora. La pulizia riguarda
    # esclusivamente le directory temporanee del lettore PDF.
    for previous in PENDING.glob("pdf_*"):
        if previous.is_dir():
            try:
                if time.time()-previous.stat().st_mtime > 3600:
                    shutil.rmtree(previous,ignore_errors=True)
            except OSError:
                pass
    token = secrets.token_urlsafe(30)
    staging = PENDING / ("pdf_" + token)
    staging.mkdir(mode=0o700)
    seen_hashes=set()
    for_index={}
    items=[]
    try:
        for index,(filename,data) in enumerate(uploads):
            result = evaluate_pdf(data,code,candidates,already)
            # Un filtro CdS/sede non deve consentire di acquisire per errore
            # documenti riconosciuti come appartenenti a un altro corso.
            if ((cds and result["cds"] and result["cds"]!=cds) or
                    (sede and result["sede"] and result["sede"]!=sede)):
                fail("Caricamento di "+filename+" riferito a CdS/sede diversi da quelli selezionati: "
                     +str(result["cds"])+" / "+str(result["sede"]),400)
            result["nome_originale"] = filename
            result["temp_file"] = str(index)+".pdf"
            if result["sha256"] in seen_hashes:
                result.update(stato="duplicato",motivo="File identico ripetuto nel caricamento")
            else:
                seen_hashes.add(result["sha256"])
                if result["candidatura_id"] is not None and result["stato"] in ("regolare","rettifica"):
                    for_index.setdefault(result["candidatura_id"],[]).append(index)
            (staging/result["temp_file"]).write_bytes(data)
            items.append(result)
        # La preview non modifica i documenti, ma non deve consentire
        # una successiva conferma su posizioni gia definite.
        with connect() as lock_db:
            for item in items:
                if item.get("candidatura_id") is None:
                    continue
                belonging=lock_db.execute("""
                    SELECT i.cds,i.sede FROM candidature c
                    JOIN incarichi i ON i.id=c.incarico_id
                    WHERE c.id=? AND c.bando_id=?
                """,(item["candidatura_id"],b["id"])).fetchone()
                if belonging:
                    _assert_graduatoria_open(lock_db,b["id"],belonging["cds"],belonging["sede"])
        for positions in for_index.values():
            if len(positions)>1:
                for idx in positions:
                    items[idx].update(
                        stato="da_verificare",
                        motivo="Più PDF differenti per la stessa candidatura: verificare quale scheda sia valida"
                    )
        package = {"code":code, "created_at":time.time(), "items":items}
        (staging/"preview.json").write_text(
            json.dumps(package,ensure_ascii=False),encoding="utf-8"
        )
        (staging/"preview.json").chmod(0o600)
    except Exception:
        shutil.rmtree(staging,ignore_errors=True)
        raise
    summary={status:sum(x["stato"]==status for x in items)
             for status in ("regolare","rettifica","da_verificare","duplicato")}
    summary["pdf_rilevati"]=len(uploads)
    summary["pdf_analizzati"]=len(items)
    summary["totale_classificati"]=sum(summary[status] for status in
                                      ("regolare","rettifica","da_verificare","duplicato"))
    if summary["pdf_rilevati"]!=summary["pdf_analizzati"] or summary["pdf_analizzati"]!=summary["totale_classificati"]:
        fail("Conteggio PDF non coerente: l'anteprima non può essere confermata",409)
    return {
        "token":token,
        "schede":[{k:v for k,v in item.items() if k not in ("punteggi","temp_file")}
                  for item in items],
        "riepilogo":summary,
        "avvertenza":"Nessuna valutazione è stata acquisita. I PDF sono conservati soltanto nell'area temporanea in attesa di conferma.",
    }


@app.post("/api/bandi/{code}/valutazioni/conferma")
def confirm_valutazioni(code: str, body: ConfermaValutazioni):
    code=valid_code(code)
    if not re.fullmatch(r"[A-Za-z0-9_-]+",body.token):
        fail("Token anteprima non valido")
    staging = PENDING / ("pdf_"+body.token)
    metadata = staging/"preview.json"
    if not metadata.is_file():
        fail("Anteprima scaduta o già acquisita: caricare nuovamente i PDF",409)
    # Rinomina atomica per impedire che due conferme usino lo stesso token.
    processing = PENDING / ("in_elaborazione_"+body.token)
    try:
        staging.rename(processing)
    except OSError:
        fail("Anteprima già in fase di acquisizione",409)
    saved=[]
    try:
        document=json.loads((processing/"preview.json").read_text(encoding="utf-8"))
        if document.get("code")!=code or time.time()-document.get("created_at",0)>3600:
            fail("Anteprima scaduta: caricare nuovamente i PDF",409)
        items=document["items"]
        if any(x["stato"]=="rettifica" for x in items) and not body.conferma_rettifiche:
            fail("Per acquisire una nuova versione, confermare esplicitamente le rettifiche",409)
        counts={"acquisite":0,"rettificate":0,"da_verificare":0,"duplicate":0}
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
            if not b:
                fail("Bando non trovato",404)
            candidates,already=_val_context(db,b["id"])
            fresh=[]
            for item in items:
                if item["stato"]=="duplicato":
                    counts["duplicate"]+=1
                    continue
                if item.get("candidatura_id") is not None:
                    location=db.execute("""
                        SELECT i.cds,i.sede FROM candidature c
                        JOIN incarichi i ON i.id=c.incarico_id
                        WHERE c.id=? AND c.bando_id=?
                    """,(item["candidatura_id"],b["id"])).fetchone()
                    if location:
                        _assert_graduatoria_open(db,b["id"],location["cds"],location["sede"])
                data=(processing/item["temp_file"]).read_bytes()
                if hashlib.sha256(data).hexdigest()!=item["sha256"]:
                    fail("Il PDF dell'anteprima è cambiato: interrompere l'acquisizione",409)
                revised=evaluate_pdf(data,code,candidates,already)
                if item["stato"]=="da_verificare" and item["motivo"].startswith("Più PDF differenti"):
                    revised.update(stato="da_verificare",motivo=item["motivo"])
                keys=("stato","candidatura_id","id_pica","modello","punteggio",
                      "totale_pdf","esito","note","decisione")
                if any(item.get(k)!=revised.get(k) for k in keys):
                    fail("Dati variati dopo l'anteprima: ripetere il caricamento",409)
                fresh.append((item,data,revised))
            now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
            store=SOURCES/code/"schede_restituite"
            store.mkdir(parents=True,exist_ok=True)
            for item,data,revised in fresh:
                filepath=store/(item["sha256"]+".pdf")
                if not filepath.exists():
                    with filepath.open("xb") as target:
                        target.write(data)
                    filepath.chmod(0o600)
                    saved.append(filepath)
                status=item["stato"]
                if status=="rettifica":
                    db.execute("""
                        UPDATE valutazioni_pdf SET attiva=0
                        WHERE candidatura_id=? AND stato='Acquisita' AND attiva=1
                    """,(item["candidatura_id"],))
                accepted=status in ("regolare","rettifica")
                db.execute("""
                    INSERT INTO valutazioni_pdf
                    (bando_id,candidatura_id,stato,versione,attiva,sha256,
                     nome_originale,percorso,modello,punteggi_json,totale_calcolato,
                     totale_pdf,esito,note,motivo,confermata_da,confermata_il,decisione,metodo)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,(
                    b["id"],item["candidatura_id"],
                    "Acquisita" if accepted else "Da verificare",
                    item["versione_proposta"] if accepted else 0,
                    1 if accepted else 0,
                    item["sha256"],item["nome_originale"],str(filepath.relative_to(BASE)),
                    item["modello"],
                    json.dumps(item["punteggi"],ensure_ascii=False) if accepted else "{}",
                    item["punteggio"] if accepted else None,
                    item["totale_pdf"] if accepted else "",item["esito"] if accepted else "",
                    item["note"] if accepted else "",item["motivo"],request_actor(),now,
                    item["decisione"] if accepted else "da_verificare","automatico"
                ))
                counts["rettificate" if status=="rettifica" else
                       "acquisite" if status=="regolare" else "da_verificare"]+=1
            db.commit()
        return {"ok":True,**counts,
                "avviso":"Conservati gli originali PDF. Le anomalie non modificano i punteggi acquisiti. Le rettifiche mantengono lo storico."}
    except Exception:
        # In caso di errore nel DB non restano documenti orfani dell'acquisizione.
        for path in saved:
            try:path.unlink(missing_ok=True)
            except OSError:pass
        raise
    finally:
        shutil.rmtree(processing,ignore_errors=True)


class ValutazioneManuale(BaseModel):
    """Trascrizione di una valutazione esplicitamente documentata nel PDF."""
    candidatura_id: int = Field(gt=0)
    esito: str = Field(min_length=5,max_length=160)
    punteggio: Decimal | None = None
    nota_commissione: str = Field(default="",max_length=4000)
    motivazione_tecnica: str = Field(min_length=10,max_length=600)
    documento_verificato: bool
    conferma_rettifica: bool = False


@app.post("/api/bandi/{code}/valutazioni/{pdf_id}/manuale")
def acquisisci_valutazione_manuale(code: str,pdf_id: int, body: ValutazioneManuale,
                                 request: Request):
    """Promuove una scheda 'Da verificare' solo per un amministratore.

    L'autorizzazione è verificata nel server sulla sessione autenticata:
    nascondere il comando nell'interfaccia non è una misura sufficiente.
    L'originale PDF resta immutabile e le modifiche sono tracciate.
    """
    if AUTH.role(request.cookies.get(COOKIE_NAME)) != "admin":
        fail("Acquisizione manuale riservata all'amministratore", 403)
    code=valid_code(code)
    if pdf_id<=0:fail("Documento PDF non valido")
    if not body.documento_verificato:
        fail("Verifica e attestazione del contenuto della scheda obbligatorie")
    motivo=body.motivazione_tecnica.strip()
    if len(motivo)<10:fail("Indicare il motivo tecnico dell'acquisizione manuale")
    try:
        decisione=classify_esito(body.esito)
    except ValueError:
        fail("Selezionare un esito previsto dalle schede RankUp")
    nota=body.nota_commissione.strip()
    if decisione in ("non_idoneo","escluso"):
        if not nota:fail("Riportare la motivazione effettiva della commissione")
        if body.punteggio is not None:
            fail("Per non idoneo o escluso il punteggio deve rimanere non attribuito")
        total=None
    else:
        score=body.punteggio
        if score is None or not score.is_finite() or score<0 or score>60 or score.as_tuple().exponent< -2:
            fail("Punteggio idoneo obbligatorio, compreso tra 0 e 60 con al massimo due decimali")
        total=float(score)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        document=db.execute("""
            SELECT * FROM valutazioni_pdf
            WHERE bando_id=? AND id=?
        """,(b["id"],pdf_id)).fetchone()
        if not document:fail("Documento non trovato",404)
        if document["stato"]!="Da verificare":
            fail("Si possono acquisire manualmente solo le schede Da verificare. Per una nuova versione caricare un altro PDF.",409)
        candidate=db.execute("""
            SELECT c.id,c.stato,c.id_pica,i.cds,i.sede,i.key
            FROM candidature c JOIN incarichi i ON i.id=c.incarico_id
            WHERE c.id=? AND c.bando_id=? AND i.bando_id=?
        """,(body.candidatura_id,b["id"],b["id"])).fetchone()
        if not candidate or candidate["stato"]!="Attiva":
            fail("Candidatura PICA non presente o non attiva nel bando",409)
        _assert_graduatoria_open(db,b["id"],candidate["cds"],candidate["sede"])
        if document["candidatura_id"] is not None and document["candidatura_id"]!=candidate["id"]:
            fail("La scheda era già associata a un'altra candidatura: associazione manuale vietata",409)
        # Verifica l'originale archiviato, senza fidarsi di percorsi scritti dal client.
        base=(SOURCES/code/"schede_restituite").resolve()
        original=(BASE/document["percorso"]).resolve()
        if not original.is_relative_to(base) or not original.is_file():
            fail("Originale della commissione non disponibile",409)
        pdf_bytes=original.read_bytes()
        if hashlib.sha256(pdf_bytes).hexdigest()!=document["sha256"]:
            fail("Originale PDF non integro: acquisizione manuale bloccata",409)
        # Se la scheda AcroForm è leggibile ma NON contiene alcun
        # pronunciamento (né esito, né nota), la procedura manuale non può
        # trasformare un punteggio in idoneità. Occorre una decisione
        # documentata dalla commissione, non una supposizione dell'operatore.
        try:
            pdf_fields=PdfReader(BytesIO(pdf_bytes),strict=False).get_fields() or {}
        except Exception:
            pdf_fields={}  # PDF appiattito: rimane la verifica documentale umana.
        if pdf_fields:
            raw_status=str((pdf_fields.get("STATUS") or {}).get("/V") or "").strip()
            raw_note=str((pdf_fields.get("NOTE-COMM") or {}).get("/V") or "").strip()
            if raw_status.casefold() in (
                "", "-", "-- seleziona --", "seleziona", "--seleziona--"
            ) and not raw_note:
                fail("La scheda originale non contiene né esito né motivazione. "
                     "Non è possibile convalidare un punteggio senza la decisione "
                     "della commissione: richiedere il documento completato.",409)
            if raw_status in ESITI_IDONEI+ESITI_NON_IDONEI+ESITI_ESCLUSI and raw_status!=body.esito:
                fail("Esito manuale discordante dall'esito registrato nel PDF originale",409)
        previous=db.execute("""
            SELECT id,versione,decisione,esito,note,totale_calcolato
            FROM valutazioni_pdf
            WHERE candidatura_id=? AND bando_id=?
              AND stato='Acquisita' AND attiva=1
        """,(candidate["id"],b["id"])).fetchone()
        if previous and not body.conferma_rettifica:
            fail("Esiste già una valutazione: per cambiarla confermare espressamente la rettifica",409)
        now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        old=dict(document)
        if previous:
            db.execute("UPDATE valutazioni_pdf SET attiva=0 WHERE id=?",(previous["id"],))
        version=(previous["versione"]+1) if previous else 1
        db.execute("""
            UPDATE valutazioni_pdf
            SET candidatura_id=?,stato='Acquisita',attiva=1,versione=?,
                modello=COALESCE(NULLIF(modello,''),'manuale'),
                punteggi_json='{}',totale_calcolato=?,totale_pdf='',
                esito=?,note=?,motivo=?,decisione=?,metodo='manuale',
                motivazione_tecnica=?,confermata_da=?,confermata_il=?
            WHERE id=? AND bando_id=?
        """,(
            candidate["id"],version,total,body.esito,nota,
            "Acquisizione manuale da scheda verificata",decisione,
            motivo,request_actor(),now,pdf_id,b["id"]
        ))
        current=dict(db.execute("SELECT * FROM valutazioni_pdf WHERE id=?",(pdf_id,)).fetchone())
        db.execute("""
            INSERT INTO valutazioni_pdf_eventi
                (bando_id,valutazione_pdf_id,candidatura_id,azione,
                 precedente_json,successivo_json,autore,quando)
            VALUES(?,?,?,?,?,?,?,?)
        """,(
            b["id"],pdf_id,candidate["id"],
            "Rettifica manuale" if previous else "Acquisizione manuale",
            json.dumps({"documento_da_verificare":old,
                        "versione_precedente":dict(previous) if previous else None},
                       ensure_ascii=False),
            json.dumps(current,ensure_ascii=False),request_actor(),now
        ))
        db.commit()
    return {
        "ok":True,"valutazione_id":pdf_id,"versione":version,
        "decisione":decisione,"metodo":"manuale",
        "punteggio":total,
        "avviso":"PDF originale conservato, esito acquisito e operazione registrata nello storico.",
    }


@app.get("/api/bandi/{code}/valutazioni/{pdf_id}/documento")
def download_valutazione_pdf(code: str, pdf_id: int):
    """Scarica l'originale ricevuto, anche quando è «Da verificare»."""
    code=valid_code(code)
    if pdf_id<=0:
        fail("Identificativo documento non valido")
    with connect() as db:
        row=db.execute("""
            SELECT v.percorso,v.nome_originale FROM valutazioni_pdf v
            JOIN bandi b ON b.id=v.bando_id
            WHERE b.codice=? AND v.id=?
        """,(code,pdf_id)).fetchone()
    if not row:
        fail("Documento non trovato",404)
    base=(SOURCES/code/"schede_restituite").resolve()
    file=(BASE/row["percorso"]).resolve()
    if not file.is_relative_to(base) or not file.is_file():
        fail("Originale PDF non disponibile",404)
    filename=_safe_filename(Path(row["nome_originale"]).stem,80)+".pdf"
    return FileResponse(file,media_type="application/pdf",
                        filename=filename,headers={"Cache-Control":"no-store"})


@app.get("/api/bandi/{code}/valutazioni")
def stato_valutazioni(code: str,cds: str="",sede: str=""):
    """Riepilogo per CdS e sede. Gli esiti sono espliciti, non dedotti dal voto."""
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        conditions=["i.bando_id=?"]
        args=[b["id"]]
        if cds:
            conditions.append("i.cds=?");args.append(cds)
        if sede:
            conditions.append("i.sede=?");args.append(sede)
        where=" AND ".join(conditions)
        rows=db.execute("""
          SELECT i.cds,i.sede,
            SUM(CASE WHEN c.stato='Attiva' THEN 1 ELSE 0 END) AS attese,
            SUM(CASE WHEN c.stato='Attiva' AND v.id IS NOT NULL THEN 1 ELSE 0 END) AS acquisite,
            SUM(CASE WHEN c.stato='Attiva' AND v.decisione='idoneo' THEN 1 ELSE 0 END) AS idonee,
            SUM(CASE WHEN c.stato='Attiva' AND v.decisione='non_idoneo' THEN 1 ELSE 0 END) AS non_idonee,
            SUM(CASE WHEN c.stato='Attiva' AND v.decisione='escluso' THEN 1 ELSE 0 END) AS escluse,
            SUM(CASE WHEN c.stato='Attiva' AND v.id IS NULL
              AND EXISTS (SELECT 1 FROM valutazioni_pdf a
                  WHERE a.candidatura_id=c.id AND a.stato='Da verificare') THEN 1 ELSE 0 END)
              AS da_verificare
          FROM candidature c JOIN incarichi i ON i.id=c.incarico_id
          LEFT JOIN valutazioni_pdf v ON v.candidatura_id=c.id
             AND v.stato='Acquisita' AND v.attiva=1
             AND v.decisione IN ('idoneo','non_idoneo','escluso')
          WHERE """+where+"""
          GROUP BY i.cds,i.sede ORDER BY i.cds COLLATE NOCASE,i.sede COLLATE NOCASE
        """,args).fetchall()
        archive_conditions=["v.bando_id=?"]
        archive_args=[b["id"]]
        if cds:
            archive_conditions.append("i.cds=?")
            archive_args.append(cds)
        if sede:
            archive_conditions.append("i.sede=?")
            archive_args.append(sede)
        archives=db.execute("""
          SELECT v.id,v.nome_originale,v.stato,v.motivo,v.totale_calcolato,v.versione,
                 v.percorso,v.sha256,
                 v.confermata_il,v.candidatura_id,v.decisione,v.metodo,v.attiva,
                 v.esito,v.note,v.motivazione_tecnica,
                 c.id_pica,c.cognome,c.nome,i.cds,i.sede,i.insegnamento,i.modulo
          FROM valutazioni_pdf v LEFT JOIN candidature c ON c.id=v.candidatura_id
          LEFT JOIN incarichi i ON i.id=c.incarico_id
          WHERE """+" AND ".join(archive_conditions)+"""
          ORDER BY v.id DESC LIMIT 1000
        """,archive_args).fetchall()
        unmatched=db.execute("""
          SELECT COUNT(*) FROM valutazioni_pdf
          WHERE bando_id=? AND candidatura_id IS NULL AND stato='Da verificare'
        """,(b["id"],)).fetchone()[0]
        candidates,_=_val_context(db,b["id"])
    # Calcolo in sola lettura: recupera punteggi provvisori delle schede
    # archiviate da versioni precedenti, senza modificarle o convalidarle.
    # Nessun percorso del database viene esposto al client.
    archivio=[]
    safe_dir=(SOURCES/code/"schede_restituite").resolve()
    for source in archives:
        item=dict(source)
        original_path=item.pop("percorso","")
        stored_sha=item.pop("sha256","")
        item["punteggio_rilevato"]=None
        item["manuale_possibile"]=False
        if item["stato"]=="Da verificare":
            filename=(BASE/original_path).resolve()
            if filename.is_relative_to(safe_dir) and filename.is_file() and filename.stat().st_size<=MAX_PDF_BYTES:
                try:
                    data=filename.read_bytes()
                    if hashlib.sha256(data).hexdigest()==stored_sha:
                        fields=PdfReader(BytesIO(data),strict=False).get_fields() or {}
                        if fields:
                            selected=str((fields.get("STATUS") or {}).get("/V") or "").strip()
                            note=str((fields.get("NOTE-COMM") or {}).get("/V") or "").strip()
                            blank_status=selected.casefold() in ("","-","-- seleziona --","seleziona","--seleziona--")
                            # Un PDF con punteggio ma senza decisione né note
                            # non può essere convalidato manualmente.
                            item["manuale_possibile"]=not(blank_status and not note)
                        else:
                            # PDF appiattito: la commissione potrebbe avere
                            # scritto esito e punteggio come contenuto visivo.
                            item["manuale_possibile"]=True
                        if item["totale_calcolato"] is None and fields:
                            detail=evaluate_pdf(data,code,candidates,{"hashes":set(),"active":{}})
                            points=detail.get("punteggio")
                            if (detail.get("candidatura_id")==item["candidatura_id"]
                                    and points is not None and points>0):
                                item["punteggio_rilevato"]=points
                except (ValueError, OSError, TypeError, KeyError):
                    pass
        archivio.append(item)
    detail=[dict(row) for row in rows]
    return {
        "cds_sedi":[dict(row,complete=(row["attese"]==row["acquisite"]))
                    for row in detail],
        "totale_attese":sum(row["attese"] for row in detail),
        "totale_acquisite":sum(row["acquisite"] for row in detail),
        "totale_idonee":sum(row["idonee"] for row in detail),
        "totale_non_idonee":sum(row["non_idonee"] for row in detail),
        "totale_escluse":sum(row["escluse"] for row in detail),
        "pdf_non_associati":unmatched,
        "archivio":archivio,
    }



def _graduatoria_status(db, bando_id: int, cds: str, sede: str) -> dict:
    ranked = build_graduatoria(db, bando_id, cds, sede)
    commissions = [dict(x) for x in db.execute("""
        SELECT cds,sede,presidente,componenti FROM commissioni WHERE bando_id=?
    """,(bando_id,))]
    match, commission = resolve_commission(commission_index(commissions),cds,sede)
    ranked["commissione"] = commission if match=="associata" else None
    if match!="associata" or not commission or not commission.get("presidente") or not commission.get("componenti"):
        ranked["criticita"].append("Commissione non associata univocamente a CdS e sede")
        ranked["pronta"]=False
    saved = db.execute("""
        SELECT id,stato,hash_dati,confermata_da,confermata_il,approvazione_id
        FROM graduatorie_cds WHERE bando_id=? AND cds=? AND sede=?
    """,(bando_id,cds,sede)).fetchone()
    ranked["conferma"] = dict(saved) if saved else None
    ranked["stato"] = saved["stato"] if saved else ("pronta" if ranked["pronta"] else "in_lavorazione")
    return ranked


def _assert_graduatoria_open(db, bando_id: int, cds: str, sede: str):
    closed=db.execute("""
        SELECT stato FROM graduatorie_cds
        WHERE bando_id=? AND cds=? AND sede=?
    """,(bando_id,cds,sede)).fetchone()
    if closed:
        fail("Graduatoria gia definitiva o approvata per "+cds+" / "+sede+
             ": blocco delle modifiche; serve una procedura di rettifica formale",409)


@app.get("/api/bandi/{code}/verbali/graduatorie")
def verbali_graduatorie(code: str, cds: str="", sede: str=""):
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        where=["bando_id=?"]
        args=[b["id"]]
        if cds:
            where.append("cds=?");args.append(cds)
        if sede:
            where.append("sede=?");args.append(sede)
        locations=db.execute("""
            SELECT DISTINCT cds,sede FROM incarichi WHERE """+" AND ".join(where)+"""
            ORDER BY cds COLLATE NOCASE,sede COLLATE NOCASE
        """,args).fetchall()
        output=[_graduatoria_status(db,b["id"],r["cds"],r["sede"]) for r in locations]
    return {"graduatorie":output}


class FinalizzaGraduatoria(BaseModel):
    cds: str = Field(min_length=1,max_length=250)
    sede: str = Field(min_length=1,max_length=100)


@app.post("/api/bandi/{code}/verbali/graduatorie/definitiva")
def finalizza_graduatoria_cds(code: str, body: FinalizzaGraduatoria, request: Request):
    require_admin(request)
    code=valid_code(code)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        _assert_graduatoria_open(db,b["id"],body.cds,body.sede)
        result=_graduatoria_status(db,b["id"],body.cds,body.sede)
        if not result["pronta"]:
            fail("Impossibile rendere definitiva la graduatoria: "+
                 "; ".join(result["criticita"][:8]),409)
        stamp=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        snapshot=json.dumps(result,ensure_ascii=False,sort_keys=True)
        digest=hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        cursor=db.execute("""
            INSERT INTO graduatorie_cds
            (bando_id,cds,sede,stato,dati_json,hash_dati,confermata_da,confermata_il)
            VALUES(?,?,?,'definitiva',?,?,?,?)
        """,(b["id"],body.cds,body.sede,snapshot,digest,request_actor(),stamp))
        db.execute("""INSERT INTO graduatorie_eventi
            (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
            VALUES(?,?,?,?,?,?,?)
        """,(b["id"],"cds",cursor.lastrowid,"Graduatoria definitiva",
              json.dumps({"cds":body.cds,"sede":body.sede,"hash":digest}),
              request_actor(),stamp))
        db.commit()
    return {"ok":True,"stato":"definitiva","hash":digest}


@app.get("/api/bandi/{code}/verbali/graduatorie/pdf")
def pdf_graduatoria_cds(code: str, cds: str, sede: str, request: Request):
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        saved=db.execute("""SELECT stato,dati_json FROM graduatorie_cds
            WHERE bando_id=? AND cds=? AND sede=?""",(b["id"],cds,sede)).fetchone()
        if saved:
            snapshot=json.loads(saved["dati_json"])
            snapshot["stato"]=saved["stato"]
        else:
            snapshot=_graduatoria_status(db,b["id"],cds,sede)
            snapshot["stato"]="BOZZA NON DEFINITIVA"
    if not snapshot["incarichi"]:
        fail("Nessun incarico trovato",404)
    data=render_graduatoria_pdf(code,snapshot)
    filename="RankUP_"+_safe_filename(code,40)+"_graduatoria_"+_safe_filename(cds,50)+".pdf"
    return Response(content=data,media_type="application/pdf",
        headers={"Content-Disposition":'attachment; filename="'+filename+'"',"Cache-Control":"no-store"})


class PreparaApprovazione(BaseModel):
    graduatorie_ids: list[int] = Field(min_length=1,max_length=100)


@app.get("/api/bandi/{code}/graduatorie/approvazioni")
def elenco_approvazioni(code: str):
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        rows=db.execute("""SELECT id,stato,estremi_atto,note,
           creata_da,creata_il,approvata_da,approvata_il,dati_json
           FROM graduatorie_approvazioni WHERE bando_id=? ORDER BY id DESC
        """,(b["id"],)).fetchall()
    return {"approvazioni":[{
        **{k:row[k] for k in ("id","stato","estremi_atto","note","creata_da","creata_il","approvata_da","approvata_il")},
        "ambiti":[{"cds":x["cds"],"sede":x["sede"]} for x in json.loads(row["dati_json"])["selezioni"]]
    } for row in rows]}


@app.post("/api/bandi/{code}/graduatorie/approvazioni")
def prepara_approvazione(code: str, body: PreparaApprovazione, request: Request):
    require_admin(request)
    code=valid_code(code)
    ids=list(dict.fromkeys(body.graduatorie_ids))
    if len(ids)!=len(body.graduatorie_ids):
        fail("Graduatorie duplicate non consentite")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        result=[]
        for gid in ids:
            r=db.execute("""SELECT id,cds,sede,dati_json,stato
                FROM graduatorie_cds WHERE bando_id=? AND id=?
            """,(b["id"],gid)).fetchone()
            if not r or r["stato"]!="definitiva":
                fail("Selezionare soltanto graduatorie definitive, non approvate",409)
            # Una graduatoria non puo gia essere inclusa in un'altra bozza.
            existing=db.execute("""
                SELECT dati_json FROM graduatorie_approvazioni
                WHERE bando_id=? AND stato='bozza'
            """,(b["id"],)).fetchall()
            if any(gid in json.loads(x["dati_json"]).get("ids",[]) for x in existing):
                fail("Graduatoria gia presente in una bozza di approvazione",409)
            result.append(json.loads(r["dati_json"]))
        now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        snapshot={"ids":ids,"selezioni":result}
        cursor=db.execute("""INSERT INTO graduatorie_approvazioni
            (bando_id,stato,dati_json,creata_da,creata_il)
            VALUES(?,'bozza',?,?,?)
        """,(b["id"],json.dumps(snapshot,ensure_ascii=False),request_actor(),now))
        db.execute("""INSERT INTO graduatorie_eventi
            (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
            VALUES(?,?,?,?,?,?,?)
        """,(b["id"],"approvazione",cursor.lastrowid,"Bozza approvazione creata",
              json.dumps({"ids":ids}),request_actor(),now))
        db.commit()
    return {"id":cursor.lastrowid,"stato":"bozza"}


class ConfermaApprovazione(BaseModel):
    estremi_atto: str = Field(min_length=5,max_length=500)
    nota: str = Field(default="",max_length=1000)


@app.post("/api/bandi/{code}/graduatorie/approvazioni/{approval_id}/conferma")
def registra_approvazione(code: str, approval_id: int, body: ConfermaApprovazione, request: Request):
    require_admin(request)
    code=valid_code(code)
    if not body.estremi_atto.strip() or "[DA VERIFICARE]" in body.estremi_atto.upper():
        fail("Indicare gli estremi verificati del provvedimento di approvazione")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        approval=db.execute("""SELECT id,stato,dati_json FROM graduatorie_approvazioni
            WHERE id=? AND bando_id=?""",(approval_id,b["id"])).fetchone()
        if not approval or approval["stato"]!="bozza":
            fail("Approvazione inesistente o gia registrata",409)
        snapshot=json.loads(approval["dati_json"])
        now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        for gid in snapshot["ids"]:
            r=db.execute("""SELECT stato FROM graduatorie_cds WHERE id=? AND bando_id=?""",
                         (gid,b["id"])).fetchone()
            if not r or r["stato"]!="definitiva":
                fail("Una graduatoria non e piu disponibile: annullare la bozza",409)
        for gid in snapshot["ids"]:
            db.execute("""UPDATE graduatorie_cds SET stato='approvata',approvazione_id=?
                WHERE id=? AND bando_id=?""",(approval_id,gid,b["id"]))
        db.execute("""UPDATE graduatorie_approvazioni SET stato='approvata',
            estremi_atto=?,note=?,approvata_da=?,approvata_il=?
            WHERE id=? AND bando_id=?""",(body.estremi_atto.strip(),body.nota.strip(),
                                           request_actor(),now,approval_id,b["id"]))
        db.execute("""INSERT INTO graduatorie_eventi
            (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
            VALUES(?,?,?,?,?,?,?)
        """,(b["id"],"approvazione",approval_id,"Atti approvati - incarichi chiusi",
              json.dumps({"graduatorie_ids":snapshot["ids"],
                          "estremi_atto":body.estremi_atto.strip()},ensure_ascii=False),
              request_actor(),now))
        db.commit()
    return {"ok":True,"stato":"approvata","graduatorie_chiuse":len(snapshot["ids"])}


@app.delete("/api/bandi/{code}/graduatorie/approvazioni/{approval_id}")
def annulla_bozza_approvazione(code: str, approval_id: int, request: Request):
    require_admin(request)
    code=valid_code(code)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        row=db.execute("""SELECT stato,dati_json FROM graduatorie_approvazioni
            WHERE bando_id=? AND id=?""",(b["id"],approval_id)).fetchone()
        if not row or row["stato"]!="bozza":
            fail("Solo le bozze non approvate possono essere annullate",409)
        stamp=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        db.execute("""INSERT INTO graduatorie_eventi
           (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
           VALUES(?,?,?,?,?,?,?)
        """,(b["id"],"approvazione",approval_id,"Bozza annullata",row["dati_json"],
             request_actor(),stamp))
        db.execute("DELETE FROM graduatorie_approvazioni WHERE id=? AND bando_id=?",
                  (approval_id,b["id"]))
        db.commit()
    return {"ok":True}


@app.get("/api/bandi/{code}/graduatorie/approvazioni/{approval_id}/pdf")
def pdf_approvazione(code: str, approval_id: int, request: Request):
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        row=db.execute("""SELECT stato,estremi_atto,dati_json FROM graduatorie_approvazioni
            WHERE id=? AND bando_id=?""",(approval_id,b["id"])).fetchone()
    if not row:fail("Approvazione non trovata",404)
    payload=json.loads(row["dati_json"])
    pdf=render_graduatoria_pdf(code,payload,general=True,
             approved=row["stato"]=="approvata",reference=row["estremi_atto"])
    return Response(content=pdf,media_type="application/pdf",
        headers={"Content-Disposition":'attachment; filename="RankUP_'+_safe_filename(code,40)+'_graduatoria_generale.pdf"',
                 "Cache-Control":"no-store"})


@app.get("/api/bandi/{code}/valutazioni/candidature")
def candidature_per_valutazione_manuale(code: str, cds: str="",sede: str=""):
    """Solo candidati PICA del bando; nessuna identità dedotta dal PDF."""
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        where=["c.bando_id=?","c.stato='Attiva'"]
        args=[b["id"]]
        if cds:where.append("i.cds=?");args.append(cds)
        if sede:where.append("i.sede=?");args.append(sede)
        rows=db.execute("""
            SELECT c.id,c.id_pica,c.cognome,c.nome,i.cds,i.sede,
                   i.insegnamento,i.modulo
            FROM candidature c JOIN incarichi i ON i.id=c.incarico_id
            WHERE """+" AND ".join(where)+"""
            ORDER BY i.cds COLLATE NOCASE,i.sede COLLATE NOCASE,
                     c.cognome COLLATE NOCASE,c.nome COLLATE NOCASE,i.modulo COLLATE NOCASE
            LIMIT 5000
        """,args).fetchall()
    return {"candidature":[dict(row) for row in rows]}

