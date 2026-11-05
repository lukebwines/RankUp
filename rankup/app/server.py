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
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pica import MAX_FILE_BYTES, parse_pica
from commissioni import MAX_COMMISSION_FILE, parse_commissioni, make_commission_template
from commission_match import commission_index, resolve_commission, course_key, seat_key
from auth import Authentication, COOKIE_NAME, SESSION_SECONDS, cookie_path, secure_cookie
from schede import make_scheda
from incarichi_fields import EXTRA_DB_FIELDS, incarico_extra_db

BASE = Path(os.getenv("RANKUP_DATA_DIR", "/data"))
PENDING = BASE / "pending"
SOURCES = BASE / "sources"
DB = BASE / "rankup.sqlite3"
ASSETS = Path(__file__).resolve().parent / "static"
CODE_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$")

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
        CREATE INDEX IF NOT EXISTS ix_cand_bando ON candidature(bando_id);
        """)
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
app = FastAPI(title="RankUp", version="0.5.0", docs_url=None, redoc_url=None)
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
    if not public and not AUTH.valid(request.cookies.get(COOKIE_NAME)):
        return JSONResponse({"detail": "Autenticazione richiesta"}, status_code=401)
    response = await call_next(request)
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
    return {
        "configured": AUTH.configured,
        "authenticated": AUTH.valid(request.cookies.get(COOKIE_NAME)),
        "username": AUTH.username if AUTH.valid(request.cookies.get(COOKIE_NAME)) else None,
    }


@app.post("/api/auth/login")
def auth_login(credentials: LoginRequest, request: Request):
    if not AUTH.configured:
        fail("Configurare amministratore e password nella scheda Configurazione dell'add-on", 503)
    token = AUTH.authenticate(credentials.username, credentials.password)
    if token is None:
        fail("Credenziali non valide oppure troppi tentativi: riprovare più tardi", 401)
    response = JSONResponse({"ok": True, "username": AUTH.username})
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
    return {"status": "ok", "version": "0.5.0"}


@app.get("/api/bandi")
def list_bandi():
    with connect() as db:
        rows = db.execute("""
          SELECT b.codice, b.stato, b.import_at, b.source_filename,
                 (SELECT COUNT(*) FROM candidature c WHERE c.bando_id=b.id) AS candidature,
                 (SELECT COUNT(*) FROM incarichi i WHERE i.bando_id=b.id) AS incarichi,
                 (SELECT COUNT(DISTINCT i.cds) FROM incarichi i WHERE i.bando_id=b.id) AS cds
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
                  json.dumps(values,ensure_ascii=False), AUTH.username, now))
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
                 COUNT(c.id) AS candidature
          FROM incarichi i
          JOIN bandi b ON b.id=i.bando_id
          LEFT JOIN candidature c ON c.incarico_id=i.id
          LEFT JOIN ssd_classificazioni cl ON cl.ssd=UPPER(TRIM(i.ssd))
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
        if not db.execute("SELECT 1 FROM bandi WHERE codice=?", (code,)).fetchone():
            fail("Bando non trovato", 404)
    return {
        "incarichi": [dict(row) for row in rows],
        "acquisizione_metadati": dict(migration) if migration else None,
    }


@app.get("/api/bandi/{code}/candidature")
def list_candidature(code: str):
    code = valid_code(code)
    with connect() as db:
        rows = db.execute("""
           SELECT c.id_pica, c.cognome, c.nome, c.stato,
                  i.cds, i.sede, i.insegnamento, i.modulo
           FROM candidature c
           JOIN incarichi i ON i.id=c.incarico_id
           JOIN bandi b ON b.id=c.bando_id
           WHERE b.codice=?
           ORDER BY i.cds COLLATE NOCASE, i.sede COLLATE NOCASE,
                    i.insegnamento COLLATE NOCASE, i.modulo COLLATE NOCASE,
                    c.cognome COLLATE NOCASE, c.nome COLLATE NOCASE
           LIMIT 10000
        """, (code,)).fetchall()
    return {"candidature": [dict(row) for row in rows]}


@app.post("/api/pica/preview")
async def preview_pica(file: UploadFile = File(...)):
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
        exists = db.execute("SELECT 1 FROM bandi WHERE codice=?", (parsed["code"],)).fetchone()
    if exists:
        fail("Il bando " + parsed["code"] + " è già stato importato. Nessuna sovrascrittura automatica.", 409)
    token = secrets.token_urlsafe(24)
    metadata = {"parsed": parsed, "filename": filename, "sha256": hashlib.sha256(content).hexdigest()}
    (PENDING / (token + ".json")).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    (PENDING / (token + ".input")).write_bytes(content)
    return {"token": token, "codice_bando": parsed["code"],
            "titolo_pica": parsed["source_title"], "riepilogo": parsed["summary"],
            "file": filename, "gia_presente": False}


class CommitRequest(BaseModel):
    token: str


@app.post("/api/pica/commit")
def commit_pica(body: CommitRequest):
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
        json.dumps(after, ensure_ascii=False), AUTH.username,
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
                 i.key,i.cds,i.sede,i.insegnamento,i.modulo,i.ul,i.ssd,
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
                  _safe_filename(record["id_pica"])+"_"+record["key"][:10]+".pdf")
            archive.writestr(path,pdf)
    filename = "RankUp_"+code+"_schede"
    if cds:
        filename += "_"+_safe_filename(cds,30)
    return Response(content=buffer.getvalue(),media_type="application/zip",headers={
        "Content-Disposition":'attachment; filename="'+filename+'.zip"',
        "Cache-Control":"no-store",
    })
