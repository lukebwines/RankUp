"""RankUp - prima milestone operativa (Home Assistant Ingress).

Accesso diretto possibile soltanto dopo autenticazione RankUp.
Home Assistant Ingress rimane disponibile come ulteriore percorso di accesso.
"""
from __future__ import annotations

import hashlib
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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pica import MAX_FILE_BYTES, parse_pica
from commissioni import MAX_COMMISSION_FILE, parse_commissioni
from auth import Authentication, COOKIE_NAME, SESSION_SECONDS, cookie_path, secure_cookie

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
        CREATE INDEX IF NOT EXISTS ix_comm_bando ON commissioni(bando_id);
        CREATE INDEX IF NOT EXISTS ix_inc_bando ON incarichi(bando_id);
        CREATE INDEX IF NOT EXISTS ix_cand_bando ON candidature(bando_id);
        """)


initialize()
app = FastAPI(title="RankUp", version="0.2.0", docs_url=None, redoc_url=None)
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
    username: str
    password: str


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
    return {"status": "ok", "version": "0.2.0"}


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
                 i.id_copertura, i.codice_copertura,
                 COUNT(c.id) AS candidature
          FROM incarichi i
          JOIN bandi b ON b.id=i.bando_id
          LEFT JOIN candidature c ON c.incarico_id=i.id
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
                                            codice_copertura,id_copertura)
                       VALUES(?,?,?,?,?,?,?,?,?)
                    """, (bando_id, key, item["cds"], item["sede"], item["insegnamento"],
                          item["modulo"], item["ul"], item["codice_copertura"], item["id_copertura"]))
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
