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

from fastapi import FastAPI, File, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pica import MAX_FILE_BYTES, parse_pica
from commissioni import MAX_COMMISSION_FILE, parse_commissioni
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
app = FastAPI(title="RankUp", version="0.3.1", docs_url=None, redoc_url=None)
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
    return {"status": "ok", "version": "0.3.1"}


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


@app.get("/api/bandi/{code}/incarichi")
def list_incarichi(code: str):
    code = valid_code(code)
    with connect() as db:
        rows = db.execute("""
          SELECT i.cds, i.sede, i.insegnamento, i.modulo, i.ul,
                 i.id_copertura, i.codice_copertura, i.ssd,
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
    return {"incarichi": [dict(row) for row in rows]}


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
                    cur = db.execute("""
                       INSERT INTO incarichi(bando_id,key,cds,sede,insegnamento,modulo,ul,
                                            codice_copertura,id_copertura,ssd)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                    """, (bando_id, key, item["cds"], item["sede"], item["insegnamento"],
                          item["modulo"], item["ul"], item["codice_copertura"], item["id_copertura"],
                          item.get("ssd","").strip().upper()))
                    assignment_ids[key] = cur.lastrowid
                db.execute("""
                    INSERT INTO candidature(bando_id,incarico_id,id_pica,nome,cognome,
                                            codice_fiscale,stato_pica,source_row,source_json)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (bando_id, assignment_ids[key], record["id_pica"], record["nome"],
                      record["cognome"], record["codice_fiscale"], record["stato_pica"],
                      record["source_row"], json.dumps(record, ensure_ascii=False)))
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


@app.get("/api/bandi/{code}/commissioni")
def list_commissioni(code: str):
    code = valid_code(code)
    with connect() as db:
        rows = db.execute("""
            SELECT cm.cds, cm.sede, cm.presidente, cm.componenti, cm.anno
            FROM commissioni cm JOIN bandi b ON cm.bando_id=b.id
            WHERE b.codice=? ORDER BY cm.cds COLLATE NOCASE, cm.sede COLLATE NOCASE
        """, (code,)).fetchall()
    return {"commissioni": [dict(x) for x in rows]}


@app.post("/api/bandi/{code}/commissioni/import")
async def import_commissioni(code: str, file: UploadFile = File(...)):
    code = valid_code(code)
    filename = (file.filename or "").split("/")[-1].split("\\")[-1]
    content = await file.read(MAX_COMMISSION_FILE + 1)
    try:
        parsed = parse_commissioni(filename, content, code)
    except ValueError as error:
        fail(str(error))
    except Exception:
        fail("Impossibile leggere l'Excel delle commissioni")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if b is None:
            db.rollback()
            fail("Bando non trovato", 404)
        exists = db.execute("SELECT 1 FROM commissioni WHERE bando_id=? LIMIT 1", (b["id"],)).fetchone()
        if exists:
            db.rollback()
            fail("Commissioni già importate: la modifica guidata sarà disponibile in una fase successiva", 409)
        for row in parsed["rows"]:
            db.execute("""
              INSERT INTO commissioni(bando_id,cds,sede,presidente,componenti,requisiti,anno,updated_at)
              VALUES(?,?,?,?,?,?,?,?)
            """, (b["id"],row["cds"],row["sede"],row["presidente"],row["componenti"],
                  row["requisiti"],row["anno"],now))
        db.commit()
    destination=SOURCES / code
    destination.mkdir(parents=True,exist_ok=True)
    (destination / "commissioni_originali.xlsx").write_bytes(content)
    return {"ok":True,"commissioni":parsed["numero_sedi"],"cds":parsed["numero_cds"]}


class ClassificazioneSSD(BaseModel):
    tipo: str
    riferimento: str


@app.get("/api/bandi/{code}/ssd")
def elenco_ssd_bando(code: str):
    code = valid_code(code)
    with connect() as db:
        rows = db.execute("""
           SELECT UPPER(TRIM(i.ssd)) AS ssd, cl.tipo, cl.riferimento,
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


@app.put("/api/ssd/{ssd}/classificazione")
def assegna_classificazione(ssd: str, body: ClassificazioneSSD):
    sector = ssd.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+(?:[-/][A-Z0-9]+)*", sector) or len(sector)>32:
        fail("Codice SSD non valido")
    if sector in ("NN", "ND", "N-D", "NON-INDICATO"):
        fail("PICA non attribuisce un SSD specifico a questo incarico: verificare la copertura prima di associare un modello PDF")
    if body.tipo not in ("professionalizzante", "ordinario"):
        fail("Selezionare professionalizzante oppure ordinario")
    reference = body.riferimento.strip()
    if len(reference)<3 or len(reference)>400:
        fail("Inserire il riferimento documentale verificato che giustifica la classificazione")
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
                 cl.tipo AS tipo_ssd,
                 cm.presidente,cm.componenti,cm.anno
          FROM candidature c
          JOIN incarichi i ON i.id=c.incarico_id
          JOIN bandi b ON b.id=c.bando_id
          LEFT JOIN ssd_classificazioni cl ON cl.ssd=UPPER(TRIM(i.ssd))
          LEFT JOIN commissioni cm ON cm.id=(
              SELECT cm2.id FROM commissioni cm2
              WHERE cm2.bando_id=b.id
                AND LOWER(TRIM(cm2.cds))=LOWER(TRIM(i.cds))
              ORDER BY cm2.id LIMIT 1
          )
          WHERE """+" AND ".join(conditions)+"""
          ORDER BY i.cds COLLATE NOCASE, i.sede COLLATE NOCASE,
                   i.insegnamento COLLATE NOCASE, i.modulo COLLATE NOCASE,
                   c.cognome COLLATE NOCASE,c.nome COLLATE NOCASE
        """,params).fetchall()
    if not rows:
        fail("Nessuna candidatura attiva trovata per la selezione")
    if len(rows)>5000:
        fail("Numero schede troppo elevato: selezionare uno o piu CdS")
    unclassified=sorted({r["ssd"] or "(SSD mancante)" for r in rows if not r["tipo_ssd"]})
    if unclassified:
        fail("Classificare gli SSD prima della generazione: "+", ".join(unclassified[:20]))
    without_commission=sorted({r["cds"]+" / "+r["sede"] for r in rows if not r["presidente"]})
    if without_commission:
        fail("Commissioni non associate per: "+", ".join(without_commission[:12]))
    buffer=BytesIO()
    with zipfile.ZipFile(buffer,"w",compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for row in rows:
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
