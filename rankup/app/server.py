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
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import threading
from db_lifecycle import AutoClosingConnection, SCHEMA_VERSION, prepare_schema_upgrade, record_schema_version

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter

from pica import MAX_FILE_BYTES, parse_pica
from ente_check import evaluate_ente
from ai_groq import (DEFAULT_GUIDANCE, REVISED_GUIDANCE, GroqSecrets, GroqLimit, role_category, prepare_minimal_payload, source_hash as groq_source_hash,
                     analyze as groq_analyze, models as groq_models, report_pdf as groq_report_pdf)
from ai_ente_report import render_ente_report_pdf
from ai_ente_rules import (classifica_ente, NON_APPLICABILE, ENTE_COERENTE,
                           DA_APPROFONDIRE, RULES_VERSION)
from ai_ente_queue import (ensure_schema as ai_queue_schema, create_job as ai_create_job,
                           last_job as ai_last_job, progress as ai_job_progress,
                           job_action as ai_job_action, cases_with_ai as ai_cases,
                           process_one as ai_process_one, quota_info as ai_quota_info)
from title_date_check import check_three_years
from pdf_import import (
    evaluate_pdf, MAX_PDF_BYTES, MAX_BATCH_BYTES, MAX_BATCH_FILES,
    classify_esito, ESITI_IDONEI, ESITI_NON_IDONEI, ESITI_ESCLUSI,
)
from commissioni import MAX_COMMISSION_FILE, parse_commissioni, make_commission_template
from commission_match import commission_index, resolve_commission, course_key, seat_key
from auth import Authentication, COOKIE_NAME, SESSION_SECONDS, cookie_path, secure_cookie
from schede import make_scheda
from graduatorie import build_graduatoria, render_graduatoria_pdf, enrich_print_metadata
from pdf_definitivo import verbale_definitivo
from verbale_seduta import genera_pdf as genera_verbale_seduta, acquisisci_pdf as acquisisci_verbale_seduta, source_digest as impronta_verbale_seduta, nome_verbale
from archive_bundle import crea_pacchetto, controlla_chiusura, ArchivioNonPronto
from incarichi_fields import EXTRA_DB_FIELDS, incarico_extra_db
from reset_data import RESET_CONFIRMATION, preview as reset_preview, reset_with_backup

BASE = Path(os.getenv("RANKUP_DATA_DIR", "/data"))
PENDING = BASE / "pending"
SOURCES = BASE / "sources"
ARCHIVI = BASE / "archivi_bandi"
DB = BASE / "rankup.sqlite3"
GROQ = GroqSecrets(BASE)
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
    db = sqlite3.connect(DB, timeout=20, factory=AutoClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=20000")
    return db



def _conserva_commissione_catalogo(db, values: dict, fonte_bando: str):
    """Archivia composizioni realmente inserite, senza dedurre validita' per altri bandi."""
    fields=("cds","sede","presidente","componenti","requisiti","anno")
    data={k:str(values.get(k) or "").strip() for k in fields}
    # La chiave normalizzata elimina duplicati testuali dello stesso contenuto,
    # non unisce composizioni diverse e conserva le versioni precedenti.
    canon=dict(data,cds_key=course_key(data["cds"]),sede_key=seat_key(data["sede"]))
    fingerprint=hashlib.sha256(json.dumps(canon,ensure_ascii=False,
                       sort_keys=True).encode("utf-8")).hexdigest()
    db.execute("""
        INSERT OR IGNORE INTO commissioni_catalogo
        (impronta,cds,sede,presidente,componenti,requisiti,anno,fonte_bando,conservata_il)
        VALUES(?,?,?,?,?,?,?,?,?)
    """,(fingerprint,*(data[k] for k in fields),fonte_bando,
         datetime.now(timezone.utc).isoformat(timespec="microseconds")))


def _refresh_application_data(db: sqlite3.Connection) -> None:
    """Ripristina regole PICA e ricontrolla esiti storici in modo idempotente.

    Queste verifiche erano eseguite ad ogni initialize anche nella versione
    precedente: devono continuare a funzionare dopo un reset o un restore.
    Non modificano la versione strutturale dello schema.
    """
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
    # Acquisizione non distruttiva delle commissioni gia' registrate nei
    # bandi, incluse quelle antecedenti alla separazione dell'anagrafica.
    for row in db.execute("""
        SELECT cm.cds,cm.sede,cm.presidente,cm.componenti,cm.requisiti,
               cm.anno,b.codice AS fonte_bando
        FROM commissioni cm JOIN bandi b ON b.id=cm.bando_id
    """).fetchall():
        _conserva_commissione_catalogo(db,dict(row),row["fonte_bando"])
    # Esiti storici già esplicitamente registrati: classificazione esatta.
    # Il numero di esiti può cambiare: segnaposto SQL derivati dagli
    # elenchi attuali + storici, senza modificare valutazioni archiviate.
    flags = lambda values: ",".join("?" for _ in values)
    db.execute(f"""UPDATE valutazioni_pdf SET decisione=
        CASE
          WHEN esito IN ({flags(ESITI_IDONEI)}) THEN 'idoneo'
          WHEN esito IN ({flags(ESITI_NON_IDONEI)}) THEN 'non_idoneo'
          WHEN esito IN ({flags(ESITI_ESCLUSI)}) THEN 'escluso'
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


def initialize() -> None:
    with connect() as db:
        if not prepare_schema_upgrade(db, DB):
            _refresh_application_data(db)
            return
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
        CREATE TABLE IF NOT EXISTS commissioni_catalogo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            impronta TEXT NOT NULL UNIQUE,
            cds TEXT NOT NULL,
            sede TEXT NOT NULL,
            presidente TEXT NOT NULL,
            componenti TEXT NOT NULL,
            requisiti TEXT NOT NULL,
            anno TEXT NOT NULL,
            fonte_bando TEXT NOT NULL,
            conservata_il TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_comm_catalogo_cds_sede
          ON commissioni_catalogo(cds,sede,anno);
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
            data_scadenza TEXT NOT NULL DEFAULT '',
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
        -- Ricerca storica per identità fiscale; non modifica la chiave di
        -- unicità PICA e non obbliga i candidati senza CF a inserire dati fittizi.
        CREATE INDEX IF NOT EXISTS ix_cand_cf_storico
          ON candidature(UPPER(REPLACE(REPLACE(COALESCE(codice_fiscale,''),' ',''),'-','')));
        CREATE INDEX IF NOT EXISTS ix_cand_nominativo_storico
          ON candidature(cognome COLLATE NOCASE,nome COLLATE NOCASE);
        """)
        # Migrazione conservativa del vincolo SSD: terzo modello Inglese.
        # I dati e i riferimenti delle classificazioni preesistenti sono preservati.
        classification_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ssd_classificazioni'"
        ).fetchone()["sql"]
        if "'inglese'" not in classification_sql:
            db.execute("""CREATE TABLE ssd_classificazioni_v3 (
                ssd TEXT PRIMARY KEY,
                tipo TEXT NOT NULL CHECK(tipo IN ('professionalizzante','ordinario','inglese')),
                riferimento TEXT NOT NULL,
                aggiornato_il TEXT NOT NULL
            )""")
            db.execute("""INSERT INTO ssd_classificazioni_v3
                (ssd,tipo,riferimento,aggiornato_il)
                SELECT ssd,tipo,riferimento,aggiornato_il FROM ssd_classificazioni
            """)
            db.execute("DROP TABLE ssd_classificazioni")
            db.execute("ALTER TABLE ssd_classificazioni_v3 RENAME TO ssd_classificazioni")
        ref_columns={r["name"] for r in db.execute("PRAGMA table_info(bandi_riferimenti)")}
        if "data_scadenza" not in ref_columns:
            db.execute("ALTER TABLE bandi_riferimenti ADD COLUMN data_scadenza TEXT NOT NULL DEFAULT ''")
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
        db.execute("""
            CREATE TABLE IF NOT EXISTS graduatorie_vincitori_proposti (
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                incarico_id INTEGER NOT NULL REFERENCES incarichi(id) ON DELETE CASCADE,
                candidatura_id INTEGER NOT NULL REFERENCES candidature(id),
                motivazione TEXT NOT NULL,
                riferimento TEXT NOT NULL,
                autore TEXT NOT NULL,
                quando TEXT NOT NULL,
                PRIMARY KEY(bando_id,incarico_id)
            )
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
        # Storico append-only e marcatori di regressione; nessuna riscrittura
        # delle tabelle delle graduatorie e degli atti esistenti.
        db.executescript("""
            CREATE TABLE IF NOT EXISTS graduatorie_cds_revisioni (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                graduatoria_id INTEGER NOT NULL,
                cds TEXT NOT NULL,
                sede TEXT NOT NULL,
                dati_json TEXT NOT NULL,
                hash_dati TEXT NOT NULL,
                pdf_storico BLOB NOT NULL,
                hash_pdf TEXT NOT NULL,
                motivazione TEXT NOT NULL,
                riferimento TEXT NOT NULL DEFAULT '',
                autore TEXT NOT NULL,
                quando TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_grad_cds_revisioni
              ON graduatorie_cds_revisioni(bando_id,cds,sede,id);
            CREATE TABLE IF NOT EXISTS graduatorie_cds_documenti (
                graduatoria_id INTEGER PRIMARY KEY REFERENCES graduatorie_cds(id) ON DELETE CASCADE,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                pdf_definitivo BLOB NOT NULL,
                sha256 TEXT NOT NULL,
                creato_il TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graduatorie_cds_riaperte (
                graduatoria_id INTEGER PRIMARY KEY REFERENCES graduatorie_cds(id) ON DELETE CASCADE,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                revisione_id INTEGER NOT NULL REFERENCES graduatorie_cds_revisioni(id)
            );
            CREATE TABLE IF NOT EXISTS graduatorie_approvazioni_regressioni (
                approvazione_id INTEGER PRIMARY KEY REFERENCES graduatorie_approvazioni(id) ON DELETE CASCADE,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                motivazione TEXT NOT NULL,
                riferimento TEXT NOT NULL,
                autore TEXT NOT NULL,
                quando TEXT NOT NULL
            );
        """)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS bandi_archivi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codice TEXT NOT NULL,
                nome_file TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                file_count INTEGER NOT NULL,
                creato_da TEXT NOT NULL,
                creato_il TEXT NOT NULL,
                stato_precedente TEXT NOT NULL,
                riepilogo_json TEXT NOT NULL,
                riaperto_il TEXT,
                riaperto_da TEXT,
                motivo_riapertura TEXT,
                riferimento_riapertura TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_bandi_archivi_codice
              ON bandi_archivi(codice,id DESC);
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
        columns = {row["name"] for row in db.execute("PRAGMA table_info(incarichi)")}
        if "ssd" not in columns:
            db.execute("ALTER TABLE incarichi ADD COLUMN ssd TEXT NOT NULL DEFAULT ''")
        for field in EXTRA_DB_FIELDS:
            if field not in columns:
                db.execute("ALTER TABLE incarichi ADD COLUMN " + field +
                           " TEXT NOT NULL DEFAULT ''")
        # Registrazioni amministrative append-only e risoluzioni di parità.
        # Le tabelle si creano solo durante una migrazione di schema, non a ogni avvio.
        db.executescript("""
            CREATE TABLE IF NOT EXISTS candidature_eventi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                candidatura_id INTEGER NOT NULL REFERENCES candidature(id),
                precedente TEXT NOT NULL,
                successivo TEXT NOT NULL,
                motivazione TEXT NOT NULL,
                riferimento TEXT NOT NULL,
                autore TEXT NOT NULL,
                quando TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_candidature_eventi
              ON candidature_eventi(bando_id,candidatura_id,id DESC);
            CREATE TABLE IF NOT EXISTS graduatorie_parita_risoluzioni (
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                incarico_id INTEGER NOT NULL REFERENCES incarichi(id) ON DELETE CASCADE,
                punteggio TEXT NOT NULL,
                ordine_json TEXT NOT NULL,
                valutazioni_json TEXT NOT NULL,
                motivazione TEXT NOT NULL,
                riferimento TEXT NOT NULL,
                autore TEXT NOT NULL,
                quando TEXT NOT NULL,
                PRIMARY KEY(bando_id,incarico_id,punteggio)
            );
            CREATE TABLE IF NOT EXISTS graduatorie_cds_documenti_recuperi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                graduatoria_id INTEGER NOT NULL UNIQUE REFERENCES graduatorie_cds(id),
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                nome_file TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                motivazione TEXT NOT NULL,
                riferimento TEXT NOT NULL,
                autore TEXT NOT NULL,
                quando TEXT NOT NULL
            );
        """)
        # Modello scheda per singolo incarico NN (non globale).
        db.executescript("""
            CREATE TABLE IF NOT EXISTS incarichi_modelli_scheda (
                incarico_id INTEGER PRIMARY KEY REFERENCES incarichi(id) ON DELETE CASCADE,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                modello TEXT NOT NULL CHECK(modello IN
                    ('ordinario','professionalizzante','inglese')),
                riferimento TEXT NOT NULL,
                autore TEXT NOT NULL,
                aggiornato_il TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS incarichi_modelli_scheda_eventi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                incarico_id INTEGER NOT NULL REFERENCES incarichi(id) ON DELETE CASCADE,
                precedente_json TEXT NOT NULL,
                successivo_json TEXT NOT NULL,
                autore TEXT NOT NULL,
                quando TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_incarichi_modelli_eventi
                ON incarichi_modelli_scheda_eventi(bando_id,incarico_id,id);
        """)
        # v5: fonte testuale ufficiale e versioni PDF della Commissione.
        columns={r["name"] for r in db.execute("PRAGMA table_info(bandi_riferimenti)")}
        if "testo_verbale_bando" not in columns:
            db.execute("ALTER TABLE bandi_riferimenti ADD COLUMN testo_verbale_bando TEXT NOT NULL DEFAULT ''")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS verbali_descrittivi (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                cds TEXT NOT NULL,
                sede TEXT NOT NULL,
                versione INTEGER NOT NULL,
                attivo INTEGER NOT NULL CHECK(attivo IN (0,1)),
                nome_originale TEXT NOT NULL,
                pdf_originale BLOB NOT NULL,
                sha256_originale TEXT NOT NULL,
                pdf_statico BLOB NOT NULL,
                sha256_statico TEXT NOT NULL,
                impronta_sorgente TEXT NOT NULL,
                data_seduta TEXT NOT NULL,
                ora_seduta TEXT NOT NULL,
                discussione TEXT NOT NULL,
                autore TEXT NOT NULL,
                creato_il TEXT NOT NULL,
                UNIQUE(bando_id,cds,sede,versione)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_verbale_descrittivo_attivo
              ON verbali_descrittivi(bando_id,cds,sede) WHERE attivo=1;
        """)
        # v6: risultati istruttori GroqCloud (mai punteggi o decisioni).
        db.executescript("""
            CREATE TABLE IF NOT EXISTS ai_ente_verifiche (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bando_id INTEGER NOT NULL REFERENCES bandi(id) ON DELETE CASCADE,
                candidatura_id INTEGER NOT NULL REFERENCES candidature(id) ON DELETE CASCADE,
                incarico_id INTEGER NOT NULL REFERENCES incarichi(id) ON DELETE CASCADE,
                sorgente_sha256 TEXT NOT NULL,
                modello TEXT NOT NULL,
                requisito TEXT NOT NULL,
                categoria_ente TEXT NOT NULL,
                categoria_rapporto TEXT NOT NULL,
                indizio TEXT NOT NULL CHECK(indizio IN
                    ('INDIZI COERENTI','POSSIBILE DISALLINEAMENTO','DATI INSUFFICIENTI')),
                motivo TEXT NOT NULL,
                verifiche TEXT NOT NULL,
                autore TEXT NOT NULL,
                creato_il TEXT NOT NULL,
                UNIQUE(bando_id,candidatura_id,incarico_id,sorgente_sha256)
            );
            CREATE INDEX IF NOT EXISTS ix_ai_ente_bando
              ON ai_ente_verifiche(bando_id,candidatura_id,incarico_id,creato_il);
        """)
        # v7: coda AI persistente; conserva gli esiti storici del vecchio flusso.
        ai_queue_schema(db)
        # Registrare la versione soltanto DOPO l'intero upgrade dello schema.
        record_schema_version(db, SCHEMA_VERSION)
        _refresh_application_data(db)


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
app = FastAPI(title="RankUp", version="1.0.16", docs_url=None, redoc_url=None)
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
    # Un bando archiviato e' bloccato: solo un Admin puo' riaprirlo
    # formalmente. La barriera vale anche per gli operatori e le API.
    if request.method not in ("GET","HEAD","OPTIONS") and path.startswith("/api/bandi/"):
        parts=path.split("/")
        if len(parts)>=4:
            # Anche gli handler convertono il codice in maiuscolo: applicare
            # identica normalizzazione PRIMA di controllare lo stato. In caso
            # contrario il percorso /abc-2026/ salterebbe il blocco di ABC-2026.
            code=parts[3].upper()
            is_formal_reopening=(len(parts)==6 and
                                 parts[4:]==["archivio","riapri"])
            if CODE_RE.fullmatch(code) and not is_formal_reopening:
                with connect() as db:
                    existing=db.execute("SELECT stato FROM bandi WHERE codice=?",
                                        (code,)).fetchone()
                if existing and existing["stato"]=="Archiviato":
                    return JSONResponse({"detail":"Bando archiviato e bloccato: "
                        "la riapertura formale e' riservata all'Admin."},status_code=409)
    token = CURRENT_ACTOR.set(principal["username"] if principal else None)
    try:
        response = await call_next(request)
    finally:
        CURRENT_ACTOR.reset(token)
    response.headers["X-RankUp-Response-Origin"] = "rankup"
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
    # L'indirizzo del peer è fornito dal server ASGI; non fidarsi di
    # CF-Connecting-IP/X-Forwarded-For forniti arbitrariamente dal client.
    source=request.client.host if request.client else "unknown"
    token = AUTH.authenticate(credentials.username, credentials.password,source=source)
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


# L'azzeramento non e' un'azione pianificata: richiede sempre l'Admin,
# la password attuale e conferma testuale. Non elimina account o opzioni.
class RichiestaAzzeramento(BaseModel):
    conferma: str = Field(min_length=1,max_length=100)
    password_admin: str = Field(min_length=1,max_length=128)


@app.get("/api/admin/azzeramento")
def anteprima_azzeramento(request: Request):
    require_admin(request)
    with connect() as db:
        return reset_preview(db,SOURCES,PENDING)


@app.post("/api/admin/azzeramento")
def esegui_azzeramento(request: Request, body: RichiestaAzzeramento):
    require_admin(request)
    if body.conferma != RESET_CONFIRMATION:
        fail("Digitare esattamente la frase di conferma",400)
    if not AUTH.verify_admin_password(body.password_admin):
        fail("Password amministratore non corretta",403)
    try:
        archive=reset_with_backup(DB,SOURCES,PENDING,BASE/"backups_rankup")
    except (ValueError,sqlite3.Error,OSError,zipfile.BadZipFile) as exc:
        fail("Azzeramento non eseguito o incompleto: "+str(exc),409)
    # Ricrea le corrispondenze PICA di sistema; account, opzioni e SSD restano.
    initialize()
    return {"ok":True,"backup":archive,
            "message":"Azzeramento dati completato. Backup scaricabile solo da Admin.",
            "preservati":"Account RankUP, password, associazioni globali SSD/modello e configurazione add-on"}


@app.get("/api/admin/azzeramento/backup/{filename}")
def scarica_backup_azzeramento(filename: str, request: Request):
    require_admin(request)
    if not re.fullmatch(r"rankup_prima_azzeramento_\d{8}T\d{6}Z_[0-9a-f]{10}\.zip",filename):
        fail("Nome backup non valido",400)
    folder=(BASE/"backups_rankup").resolve()
    file=(folder/filename).resolve()
    if not file.is_relative_to(folder) or not file.is_file():
        fail("Backup non trovato",404)
    return FileResponse(file,filename=filename,media_type="application/zip",
                        headers={"Cache-Control":"no-store"})


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
        deleted=db.execute("""
            SELECT id,username,creato_il,aggiornato_il
            FROM rankup_utenti WHERE eliminato=1 ORDER BY username COLLATE NOCASE
        """).fetchall()
    return {"utenti":[dict(r) for r in rows],
            "utenti_eliminati":[dict(r) for r in deleted],
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
        existing=db.execute("SELECT eliminato FROM rankup_utenti WHERE username=? COLLATE NOCASE",
                            (username,)).fetchone()
        if existing:
            if existing["eliminato"]:
                fail("Nome utente già presente nello storico: ripristinalo nella sezione Utenti eliminati",409)
            fail("Nome utente già attivo o disattivato: usa la gestione dell'utente esistente",409)
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


@app.put("/api/utenti/{user_id}/ripristina")
def ripristina_utente_eliminato(user_id: int,body: ReimpostaPasswordUtente,request: Request):
    """Ripristina l'account originale, conservandone ID e storico.

    Richiede password nuova: non ripristina mai la credenziale disattivata.
    Il cambio versione invalida le eventuali sessioni anteriori.
    """
    who=require_admin(request)
    if user_id<=0:fail("Identificativo utente non valido")
    try:
        salt,digest=AUTH.password_record(body.password)
    except ValueError as exc:
        fail(str(exc))
    now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("""
            SELECT id,username,eliminato FROM rankup_utenti WHERE id=?
        """,(user_id,)).fetchone()
        if not row:fail("Utente non trovato",404)
        if not row["eliminato"]:
            fail("L'utente non risulta eliminato: utilizzare la gestione ordinaria",409)
        db.execute("""
            UPDATE rankup_utenti
            SET eliminato=0,attivo=1,password_salt=?,password_hash=?,
                credenziali_versione=credenziali_versione+1,aggiornato_il=?
            WHERE id=? AND eliminato=1
        """,(salt,digest,now,user_id))
        db.execute("""
            INSERT INTO rankup_utenti_eventi(utente_id,azione,autore,quando)
            VALUES(?,?,?,?)
        """,(user_id,"Ripristinato account eliminato con nuova password",
              who["username"],now))
        db.commit()
    AUTH.revoke_user(user_id)
    return {"ok":True,"utente":{"id":user_id,"username":row["username"],"attivo":1},
            "avviso":"Account ripristinato con nuova password. Storico precedente conservato."}


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


def _require_bando_aperto(db: sqlite3.Connection, code: str) -> None:
    """Controllo transazionale, da eseguire dopo BEGIN IMMEDIATE.

    Evita che una richiesta ammessa dal middleware modifichi un bando
    archiviato nel frattempo da un'altra connessione.
    """
    current=db.execute("SELECT stato FROM bandi WHERE codice=?",
                       (code,)).fetchone()
    if current and current["stato"]=="Archiviato":
        fail("Bando archiviato e bloccato: richiedere la riapertura formale",409)


def valid_anno_accademico(value: str) -> str:
    """A.A. dichiarato dall'operatore, mai presunto dall'anno corrente."""
    anno=str(value or "").strip()
    if not anno:
        return ""
    if not re.fullmatch(r"\d{4}-\d{4}",anno):
        fail("Anno accademico: utilizzare AAAA-AAAA senza dati presunti")
    first,second=map(int,anno.split("-"))
    if second!=first+1:
        fail("Anno accademico incoerente: il secondo anno deve essere consecutivo")
    return anno


def _associa_anno_esplicito(db, bando_id: int, anno: str, autore: str, quando: str) -> None:
    """Registra l'A.A. del bando senza sostituire riferimenti già verificati.

    Utilizzata solo con anno fornito esplicitamente dall'operatore.
    Conserva lo storico delle modifiche al pari del form Riferimenti.
    """
    anno=valid_anno_accademico(anno)
    if not anno:
        return
    row=db.execute("""
        SELECT repertorio,protocollo,data_avviso,anno_accademico
        FROM bandi_riferimenti WHERE bando_id=?
    """,(bando_id,)).fetchone()
    prior=dict(row) if row else {
        "repertorio":"","protocollo":"","data_avviso":"","anno_accademico":""}
    if prior["anno_accademico"] and prior["anno_accademico"]!=anno:
        fail("Anno accademico diverso da quello già registrato per il bando: "
             "verificare i Riferimenti del bando",409)
    if prior["anno_accademico"]==anno:
        return
    if row:
        db.execute("""UPDATE bandi_riferimenti
            SET anno_accademico=?,aggiornato_il=? WHERE bando_id=?""",
            (anno,quando,bando_id))
    else:
        db.execute("""INSERT INTO bandi_riferimenti
            (bando_id,anno_accademico,aggiornato_il) VALUES(?,?,?)""",
            (bando_id,anno,quando))
    after=dict(prior,anno_accademico=anno)
    db.execute("""INSERT INTO bandi_riferimenti_eventi
        (bando_id,prima_json,dopo_json,autore,quando)
        VALUES(?,?,?,?,?)""",(bando_id,
        json.dumps(prior,ensure_ascii=False),json.dumps(after,ensure_ascii=False),
        autore,quando))


@app.get("/")
def index():
    return FileResponse(ASSETS / "index.html", media_type="text/html")


@app.get("/api/health")
def health():
    """Il watchdog richiede anche una lettura effettiva del database."""
    try:
        with connect() as db:
            available = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bandi'"
            ).fetchone()
        if not available:
            raise sqlite3.DatabaseError("Schema RankUP non inizializzato")
    except (sqlite3.Error, OSError):
        raise HTTPException(status_code=503, detail="Database RankUP non disponibile")
    return {"status": "ok", "version": "1.0.16", "schema_version": SCHEMA_VERSION}


class NuovoBandoInput(BaseModel):
    codice: str = Field(min_length=3,max_length=50)
    anno_accademico: str = Field(default="",max_length=20)


@app.post("/api/bandi")
def prepara_bando(body: NuovoBandoInput,request: Request):
    """Crea contenitore vuoto per PDF e requisiti PRIMA delle candidature PICA."""
    require_admin(request)
    code=valid_code(body.codice.strip().upper())
    anno=valid_anno_accademico(body.anno_accademico)
    now=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            cur=db.execute("""INSERT INTO bandi
                (codice,stato,created_at,source_filename,source_hash,import_at)
                VALUES(?,'In preparazione',?,'','',?)""",(code,now,now))
            _associa_anno_esplicito(db,cur.lastrowid,anno,request_actor(),now)
            db.commit()
        except sqlite3.IntegrityError:
            fail("Bando già presente",409)
    return {"ok":True,"codice":code,"stato":"In preparazione",
            "anno_accademico":anno}


@app.get("/api/bandi")
def list_bandi():
    with connect() as db:
        rows = db.execute("""
          SELECT b.codice, b.stato, b.import_at, b.source_filename,
                 COALESCE(ref.anno_accademico,'') AS anno_accademico,
                 (SELECT COUNT(*) FROM candidature c WHERE c.bando_id=b.id) AS candidature,
                 (SELECT COUNT(*) FROM incarichi i WHERE i.bando_id=b.id) AS incarichi,
                 (SELECT COUNT(DISTINCT i.cds) FROM incarichi i WHERE i.bando_id=b.id) AS cds,
                 (SELECT COUNT(DISTINCT v.candidatura_id) FROM valutazioni_pdf v
                   JOIN candidature c ON c.id=v.candidatura_id
                   WHERE v.bando_id=b.id AND c.stato='Attiva'
                     AND v.stato='Acquisita' AND v.attiva=1
                     AND v.decisione IN ('idoneo','non_idoneo','escluso'))
                   AS valutazioni_acquisite,
                 (SELECT COUNT(*) FROM (
                   SELECT DISTINCT i.cds,i.sede FROM incarichi i
                   WHERE i.bando_id=b.id AND NOT EXISTS (
                     SELECT 1 FROM graduatorie_cds g
                     LEFT JOIN graduatorie_cds_riaperte ri ON ri.graduatoria_id=g.id
                     WHERE g.bando_id=b.id AND g.cds=i.cds AND g.sede=i.sede
                       AND g.stato IN ('definitiva','approvata')
                       AND ri.graduatoria_id IS NULL
                   )
                 )) AS cds_in_lavorazione
          FROM bandi b LEFT JOIN bandi_riferimenti ref ON ref.bando_id=b.id
          ORDER BY b.import_at DESC, b.id DESC
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
    data_scadenza: str = Field(default="", max_length=16)
    anno_accademico: str = Field(default="", max_length=20)
    testo_verbale_bando: str | None = Field(default=None, max_length=40000)
    expected_updated_at: str | None = None


@app.get("/api/bandi/{code}/riferimenti")
def get_riferimenti_bando(code: str):
    code = valid_code(code)
    with connect() as db:
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not b:
            fail("Bando non trovato", 404)
        row = db.execute("SELECT repertorio,protocollo,data_avviso,data_scadenza,anno_accademico,testo_verbale_bando,aggiornato_il "
                         "FROM bandi_riferimenti WHERE bando_id=?", (b["id"],)).fetchone()
    return {"riferimenti": dict(row) if row else {
        "repertorio": "", "protocollo": "", "data_avviso": "", "data_scadenza": "",
        "anno_accademico": "", "testo_verbale_bando": "", "aggiornato_il": None,
    }}


@app.put("/api/bandi/{code}/riferimenti")
def save_riferimenti_bando(code: str, body: RiferimentiBando):
    """Solo dati comunicati dall'operatore, non estratti dagli esempi PDF."""
    code = valid_code(code)
    values = {k: str(v or "").strip() for k, v in body.model_dump().items()
              if k != "expected_updated_at" and v is not None}
    data = values["data_avviso"]
    if data:
        try:
            if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", data):
                raise ValueError()
            datetime.strptime(data, "%d/%m/%Y")
        except ValueError:
            fail("Data avviso: utilizzare GG/MM/AAAA, senza valori presunti")
    deadline = values["data_scadenza"]
    if deadline:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", deadline):
                raise ValueError()
            datetime.strptime(deadline, "%Y-%m-%dT%H:%M")
        except ValueError:
            fail("Scadenza candidature: indicare data e ora valide")
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
        _require_bando_aperto(db,code)
        b = db.execute("SELECT id FROM bandi WHERE codice=?", (code,)).fetchone()
        if not b:
            fail("Bando non trovato", 404)
        current = db.execute("""
          SELECT repertorio,protocollo,data_avviso,data_scadenza,anno_accademico,testo_verbale_bando
          FROM bandi_riferimenti WHERE bando_id=?
        """, (b["id"],)).fetchone()
        previous = dict(current) if current else {key: "" for key in ("repertorio","protocollo","data_avviso","data_scadenza","anno_accademico","testo_verbale_bando")}
        values["testo_verbale_bando"] = values.get("testo_verbale_bando", previous["testo_verbale_bando"])
        version = db.execute("SELECT aggiornato_il FROM bandi_riferimenti WHERE bando_id=?",
                             (b["id"],)).fetchone()
        current_time = version["aggiornato_il"] if version else None
        if current_time != body.expected_updated_at:
            fail("I riferimenti sono stati modificati: ricarica la pagina", 409)
        if previous != values:
            db.execute("""
              INSERT INTO bandi_riferimenti(bando_id,repertorio,protocollo,data_avviso,
                  data_scadenza,anno_accademico,testo_verbale_bando,aggiornato_il)
              VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(bando_id) DO UPDATE SET
                repertorio=excluded.repertorio,
                protocollo=excluded.protocollo,
                data_avviso=excluded.data_avviso,
                data_scadenza=excluded.data_scadenza,
                anno_accademico=excluded.anno_accademico,
                testo_verbale_bando=excluded.testo_verbale_bando,
                aggiornato_il=excluded.aggiornato_il
            """, (b["id"], values["repertorio"], values["protocollo"],
                  values["data_avviso"], values["data_scadenza"], values["anno_accademico"],
                 values["testo_verbale_bando"], now))
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
                 nn.modello AS modello_scheda_nn, nn.riferimento AS modello_scheda_riferimento,
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
          LEFT JOIN incarichi_modelli_scheda nn ON nn.incarico_id=i.id
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
        _require_bando_aperto(db,code)
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



class ModelloNNInput(BaseModel):
    tipo: str = Field(min_length=4,max_length=40)
    riferimento: str = Field(min_length=5,max_length=500)


@app.get("/api/bandi/{code}/incarichi/modelli-nn")
def modelli_incarichi_nn(code: str):
    """SSD NN: classificazione PDF specifica per ogni incarico del bando."""
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        rows=db.execute("""
            SELECT i.id,i.key,i.cds,i.sede,i.insegnamento,i.modulo,
                   i.ul,i.partizione_studenti,i.codice_partizione,
                   nn.modello AS modello_esplicito,nn.riferimento,
                   nn.autore,nn.aggiornato_il,
                   (SELECT COUNT(*) FROM valutazioni_pdf v
                    JOIN candidature c ON c.id=v.candidatura_id
                    WHERE c.incarico_id=i.id) AS schede_acquisite,
                   g.stato AS stato_graduatoria,
                   ri.graduatoria_id AS graduatoria_riaperta
            FROM incarichi i
            LEFT JOIN incarichi_modelli_scheda nn ON nn.incarico_id=i.id
            LEFT JOIN graduatorie_cds g
              ON g.bando_id=i.bando_id AND g.cds=i.cds AND g.sede=i.sede
            LEFT JOIN graduatorie_cds_riaperte ri ON ri.graduatoria_id=g.id
            WHERE i.bando_id=? AND UPPER(TRIM(i.ssd))='NN'
            ORDER BY i.cds COLLATE NOCASE,i.sede COLLATE NOCASE,
                     i.insegnamento COLLATE NOCASE,i.modulo COLLATE NOCASE,i.id
        """,(b["id"],)).fetchall()
    result=[]
    for row in rows:
        item=dict(row)
        item["modello_effettivo"]=item["modello_esplicito"] or "ordinario"
        item["bloccato"]=bool(item["schede_acquisite"] or
            (item["stato_graduatoria"] and not item["graduatoria_riaperta"]))
        result.append(item)
    return {"incarichi":result}


@app.put("/api/bandi/{code}/incarichi/{key}/modello-scheda")
def assegna_modello_incarico_nn(code: str,key: str,body: ModelloNNInput,
                                request: Request):
    require_admin(request)
    code=valid_code(code)
    if not re.fullmatch(r"[0-9a-f]{24}",key):
        fail("Chiave dell'incarico non valida")
    if body.tipo not in ("ordinario","professionalizzante","inglese"):
        fail("Scegliere un modello esistente: ordinario, professionalizzante o Inglese")
    ref=body.riferimento.strip()
    if len(ref)<5 or "[DA VERIFICARE]" in ref.upper():
        fail("Indicare un riferimento documentale verificato per la scelta della scheda")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
        row=db.execute("""
            SELECT i.id,i.bando_id,i.cds,i.sede,i.ssd FROM incarichi i
            JOIN bandi b ON b.id=i.bando_id
            WHERE b.codice=? AND i.key=?
        """,(code,key)).fetchone()
        if not row:fail("Incarico non trovato",404)
        if str(row["ssd"] or "").strip().upper()!="NN":
            fail("La scelta per incarico è consentita solo quando SSD è NN",409)
        _assert_graduatoria_open(db,row["bando_id"],row["cds"],row["sede"])
        if db.execute("""
            SELECT 1 FROM valutazioni_pdf v
            JOIN candidature c ON c.id=v.candidatura_id
            WHERE c.incarico_id=? LIMIT 1
        """,(row["id"],)).fetchone():
            fail("Modello bloccato: esistono schede PDF acquisite o da verificare "
                 "per questo incarico; non è consentito reinterpretarle.",409)
        before=db.execute("""
            SELECT modello,riferimento,autore,aggiornato_il
            FROM incarichi_modelli_scheda WHERE incarico_id=?
        """,(row["id"],)).fetchone()
        if before and before["modello"]==body.tipo and before["riferimento"]==ref:
            db.commit()
            return {"ok":True,"modificato":False,"modello":body.tipo}
        stamp=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        after={"modello":body.tipo,"riferimento":ref,
               "autore":request_actor(),"aggiornato_il":stamp}
        db.execute("""
            INSERT INTO incarichi_modelli_scheda
                (incarico_id,bando_id,modello,riferimento,autore,aggiornato_il)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(incarico_id) DO UPDATE SET
              modello=excluded.modello,riferimento=excluded.riferimento,
              autore=excluded.autore,aggiornato_il=excluded.aggiornato_il
        """,(row["id"],row["bando_id"],body.tipo,ref,request_actor(),stamp))
        db.execute("""
            INSERT INTO incarichi_modelli_scheda_eventi
                (bando_id,incarico_id,precedente_json,successivo_json,autore,quando)
            VALUES(?,?,?,?,?,?)
        """,(row["bando_id"],row["id"],
             json.dumps(dict(before) if before else {},ensure_ascii=False),
             json.dumps(after,ensure_ascii=False),request_actor(),stamp))
        db.commit()
    return {"ok":True,"modificato":True,"modello":body.tipo,
            "avviso":"Rigenerare e ridistribuire le schede PDF dell'incarico; "
                     "le schede precedenti potrebbero non corrispondere al modello scelto."}


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
        _require_bando_aperto(db,code)
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


class CambiaStatoCandidatura(BaseModel):
    stato: str = Field(min_length=5,max_length=40)
    motivazione: str = Field(min_length=10,max_length=1000)
    riferimento: str = Field(min_length=5,max_length=500)


@app.post("/api/bandi/{code}/candidature/{candidatura_id}/stato")
def aggiorna_stato_candidatura(code: str, candidatura_id: int,
                              body: CambiaStatoCandidatura, request: Request):
    """L'Admin esclude la candidatura dal lavoro della commissione senza cancellare i PDF."""
    require_admin(request)
    code=valid_code(code)
    allowed={"Rinunciata","Ritirata","Esclusa d'ufficio","Attiva"}
    if body.stato not in allowed:
        fail("Stato della candidatura non previsto",400)
    motivo=body.motivazione.strip()
    riferimento=body.riferimento.strip()
    if (len(motivo)<10 or len(riferimento)<5
            or "[DA VERIFICARE]" in motivo.upper()
            or "[DA VERIFICARE]" in riferimento.upper()):
        fail("Servono motivazione e riferimento documentale effettivi",400)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
        row=db.execute("""SELECT c.id,c.stato,c.incarico_id,c.bando_id,
                          i.cds,i.sede,g.id AS graduatoria_id
                 FROM candidature c JOIN incarichi i ON i.id=c.incarico_id
                 JOIN bandi b ON b.id=c.bando_id
                 LEFT JOIN graduatorie_cds g
                   ON g.bando_id=c.bando_id AND g.cds=i.cds AND g.sede=i.sede
                 WHERE b.codice=? AND c.id=?""",(code,candidatura_id)).fetchone()
        if not row:
            fail("Candidatura non trovata nel bando",404)
        _assert_graduatoria_open(db,row["bando_id"],row["cds"],row["sede"])
        # Un'approvazione già intervenuta non viene alterata da un evento post-atto:
        # gli scorrimenti sono esterni all'applicazione.
        if row["graduatoria_id"]:
            approvals=db.execute("""SELECT dati_json FROM graduatorie_approvazioni
                WHERE bando_id=? AND stato='approvata'""",(row["bando_id"],)).fetchall()
            if any(row["graduatoria_id"] in json.loads(a["dati_json"]).get("ids",[])
                   for a in approvals):
                fail("Atti già approvati: registrare lo scorrimento fuori da RankUP",409)
        before=row["stato"]
        if before==body.stato:
            fail("La candidatura è già nello stato indicato",409)
        if before!="Attiva" and body.stato!="Attiva":
            fail("Per correggere uno stato pregresso occorre prima il ripristino documentato",409)
        now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        db.execute("UPDATE candidature SET stato=? WHERE id=? AND bando_id=?",
                   (body.stato,candidatura_id,row["bando_id"]))
        db.execute("""INSERT INTO candidature_eventi
            (bando_id,candidatura_id,precedente,successivo,motivazione,riferimento,autore,quando)
            VALUES(?,?,?,?,?,?,?,?)""",
            (row["bando_id"],candidatura_id,before,body.stato,motivo,riferimento,
             request_actor(),now))
        db.commit()
    return {"ok":True,"candidatura_id":candidatura_id,"stato":body.stato,
            "scheda_obbligatoria":body.stato=="Attiva"}


@app.get("/api/bandi/{code}/candidature/{candidatura_id}/eventi")
def eventi_stato_candidatura(code: str, candidatura_id: int, request: Request):
    require_admin(request)
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        if not db.execute("SELECT 1 FROM candidature WHERE id=? AND bando_id=?",
                          (candidatura_id,b["id"])).fetchone():
            fail("Candidatura non trovata",404)
        events=db.execute("""SELECT precedente,successivo,motivazione,
                         riferimento,autore,quando
              FROM candidature_eventi WHERE bando_id=? AND candidatura_id=?
              ORDER BY id DESC""",(b["id"],candidatura_id)).fetchall()
    return {"eventi":[dict(e) for e in events]}


@app.get("/api/bandi/{code}/candidature")
def list_candidature(code: str):
    code = valid_code(code)
    with connect() as db:
        rows = db.execute("""
           SELECT c.id, c.id_pica, c.cognome, c.nome, c.stato, c.source_json,
                  i.key AS incarico_key, i.cds, i.sede, i.insegnamento, i.modulo, i.ssd, i.tipo_cds,
                  COALESCE(ref.data_scadenza, '') AS data_scadenza,
                  er.ente_richiesto
           FROM candidature c
           JOIN incarichi i ON i.id=c.incarico_id
           JOIN bandi b ON b.id=c.bando_id
           LEFT JOIN incarichi_ente_requisiti er ON er.bando_id=b.id AND er.key_incarico=i.key
           LEFT JOIN bandi_riferimenti ref ON ref.bando_id=b.id
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
                   ("occupazione","azienda_ente","profilo_professionale",
                    "ateneo","qualifica")}
        d.update(candidate)
        d["titolo_studio"] = (record.get("titolo_studio") or original.get("titolo_studio") or "")
        d["data_conseguimento"] = (record.get("data_conseguimento") or original.get("data_conseguimento") or "")
        d["verifica_anzianita"] = check_three_years(d["data_conseguimento"], d["data_scadenza"])
        if not d.get("ente_richiesto"):
            mapping = dictionary.get(tipo_cds_key(d.get("tipo_cds")))
            d["ente_richiesto"] = mapping["requisito"] if mapping else None
        d["verifica_ente"]=evaluate_ente(d.get("ente_richiesto"),**{
            "occupazione":candidate["occupazione"],
            "azienda":candidate["azienda_ente"],
            "profilo":candidate["profilo_professionale"]})
        output.append(d)
    return {"candidature":output}



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
    anno_accademico: str = Field(default="",max_length=20)


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
    anno = valid_anno_accademico(body.anno_accademico)
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
            _associa_anno_esplicito(db,bando_id,anno,who,now)
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
    code=db.execute("SELECT codice FROM bandi WHERE id=?",(bando_id,)).fetchone()["codice"]
    _conserva_commissione_catalogo(db,values,code)
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
    code=db.execute("SELECT codice FROM bandi WHERE id=?",(bando_id,)).fetchone()["codice"]
    _conserva_commissione_catalogo(db,updated,code)
    return True


@app.post("/api/bandi/{code}/commissioni/manuale")
def add_commissione(code: str, body: CommissioneInput):
    code = valid_code(code)
    values = _commission_fields(body.model_dump())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
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
        _require_bando_aperto(db,code)
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



@app.get("/api/admin/commissioni-globali")
def archivio_commissioni_generali(request: Request):
    """Composizioni conservate a livello applicativo, indipendenti dai bandi."""
    require_admin(request)
    with connect() as db:
        rows=db.execute("""
            SELECT id,cds,sede,presidente,componenti,requisiti,anno,
                   fonte_bando,conservata_il
            FROM commissioni_catalogo
            ORDER BY cds COLLATE NOCASE,sede COLLATE NOCASE,
                     anno DESC,id DESC
        """).fetchall()
    return {"commissioni":[dict(row) for row in rows]}


class RecuperaCommissioniGlobali(BaseModel):
    ids: list[int] = Field(min_length=1,max_length=100)
    conferma_verifica: bool


@app.post("/api/bandi/{code}/commissioni/recupera-globali")
def recupera_commissioni_globali(code: str,body: RecuperaCommissioniGlobali,
                                 request: Request):
    """Solo associazione esplicita e verificata; mai ereditare nomine automaticamente."""
    require_admin(request)
    code=valid_code(code)
    if not body.conferma_verifica:
        fail("Confermare la verifica dell'atto e della validita' della composizione",400)
    ids=body.ids
    if len(ids)!=len(set(ids)):
        fail("Commissioni duplicate nella selezione",400)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        refs=db.execute("""SELECT anno_accademico FROM bandi_riferimenti
                           WHERE bando_id=?""",(b["id"],)).fetchone()
        anno_bando=(refs["anno_accademico"] or "").strip() if refs else ""
        existing=db.execute("SELECT * FROM commissioni WHERE bando_id=?",(b["id"],)).fetchall()
        selected=[]
        for archive_id in ids:
            row=db.execute("""SELECT cds,sede,presidente,componenti,requisiti,anno
                              FROM commissioni_catalogo WHERE id=?""",(archive_id,)).fetchone()
            if not row:fail("Commissione non trovata nell'anagrafica",404)
            values=dict(row)
            _validate_commission_course(db,b["id"],values)
            if (anno_bando and values["anno"] and
                    anno_bando.casefold()!=values["anno"].casefold()):
                fail("Anno accademico della commissione non corrispondente al bando: "+
                     values["cds"]+" / "+values["sede"],409)
            if _match_commission_row(existing+selected,values["cds"],values["sede"]) is not None:
                fail("Commissione gia presente o duplicata per "+values["cds"]+" / "+values["sede"],409)
            selected.append(values)
        for values in selected:
            _insert_commission(db,b["id"],values,"recupero_da_catalogo_verificato")
        db.commit()
    return {"ok":True,"aggiunte":len(selected)}


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
        _require_bando_aperto(db,code)
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
    riferimento: str | None = None


@app.get("/api/admin/ssd/classificazioni")
def classificazioni_globali_ssd(request: Request):
    """Configurazione globale, consultabile anche senza bandi importati."""
    require_admin(request)
    with connect() as db:
        rows=db.execute("""
            SELECT sectors.ssd,cl.tipo,cl.riferimento,cl.aggiornato_il,
                   COALESCE(jobs.totale,0) AS incarichi
            FROM (
                SELECT ssd FROM ssd_classificazioni
                UNION
                SELECT DISTINCT UPPER(TRIM(ssd)) FROM incarichi
                WHERE TRIM(COALESCE(ssd,''))<>''
                  AND UPPER(TRIM(ssd)) NOT IN ('NN','ND','N-D','NON-INDICATO')
            ) sectors
            LEFT JOIN ssd_classificazioni cl ON cl.ssd=sectors.ssd
            LEFT JOIN (
                SELECT UPPER(TRIM(ssd)) AS ssd,COUNT(*) AS totale FROM incarichi
                GROUP BY UPPER(TRIM(ssd))
            ) jobs ON jobs.ssd=sectors.ssd
            ORDER BY sectors.ssd
        """).fetchall()
    return {"ssd":[dict(row) for row in rows]}


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
def assegna_classificazione(ssd: str, body: ClassificazioneSSD, request: Request):
    require_admin(request)
    sector = ssd.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+(?:[-/][A-Z0-9]+)*", sector) or len(sector)>32:
        fail("Codice SSD non valido")
    if sector in ("NN", "ND", "N-D", "NON-INDICATO"):
        fail("PICA non attribuisce un SSD specifico a questo incarico: verificare la copertura prima di associare un modello PDF")
    if body.tipo not in ("professionalizzante", "ordinario", "inglese"):
        fail("Selezionare professionalizzante, ordinario oppure Inglese")
    if body.riferimento is not None and len(body.riferimento.strip())>400:
        fail("Il riferimento facoltativo non può superare 400 caratteri")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        before=db.execute("SELECT riferimento FROM ssd_classificazioni WHERE ssd=?",
                          (sector,)).fetchone()
        reference=(body.riferimento.strip() if body.riferimento is not None
                   else (before["riferimento"] if before else ""))
        # La classificazione è una configurazione generale. Non richiede bandi
        # importati e si applica automaticamente a ogni futuro incarico SSD.
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
                      WHEN UPPER(TRIM(i.ssd))='NN' THEN COALESCE(nn.modello,'ordinario')
                      ELSE COALESCE(cl.tipo, 'ordinario') END AS tipo_ssd
          FROM candidature c
          JOIN incarichi i ON i.id=c.incarico_id
          JOIN bandi b ON b.id=c.bando_id
          LEFT JOIN ssd_classificazioni cl ON cl.ssd=UPPER(TRIM(i.ssd))
          LEFT JOIN incarichi_modelli_scheda nn ON nn.incarico_id=i.id
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
    # La verifica istruttoria dell’ente è indipendente dal controllo date.
    # Le schede archiviate restano invariate.
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
                "ATTENZIONE - Verificare occupazione e appartenenza all'ente (" +
                requested + ") secondo il requisito specifico previsto dal bando."
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
    gruppi=sorted({(r["cds"],r["sede"]) for r in associated})
    with zipfile.ZipFile(buffer,"w",compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        # Un modello descrittivo per Commissione, insieme alle schede.
        filenames=set()
        for course,location in gruppi:
            with connect() as db:
                context=_contesto_verbale_seduta(db,bando["id"],code,course,location)
            nome=nome_verbale(code,course,location)
            if nome.casefold() in filenames:
                fail("Collisione dei nomi dei verbali: verificare i CdS",409)
            filenames.add(nome.casefold())
            archive.writestr(nome,genera_verbale_seduta(context,editable=True))
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



def _contesto_verbale_seduta(db, bando_id: int, code: str,
                            cds: str, sede: str) -> dict:
    """Associa la Commissione alla sede esatta, senza inventare dati d'atto."""
    commissions=db.execute("""
        SELECT cds,sede,presidente,componenti,anno,requisiti
        FROM commissioni WHERE bando_id=?
    """,(bando_id,)).fetchall()
    status,commission=resolve_commission(commission_index(commissions),cds,sede)
    if status!="associata" or not commission:
        fail("Commissione non associata univocamente a CdS e sede",409)
    if not commission.get("presidente") or not commission.get("componenti"):
        fail("Composizione della Commissione incompleta",409)
    ref=db.execute("""
        SELECT repertorio,protocollo,data_avviso,anno_accademico,testo_verbale_bando
        FROM bandi_riferimenti WHERE bando_id=?
    """,(bando_id,)).fetchone()
    values=dict(ref) if ref else {}
    return {
        "codice":code,"cds":cds,"sede":sede,
        "anno_accademico":values.get("anno_accademico") or commission.get("anno") or "",
        "repertorio":values.get("repertorio") or "",
        "protocollo":values.get("protocollo") or "",
        "data_avviso":values.get("data_avviso") or "",
        "presidente":commission["presidente"],
        "componenti":commission["componenti"],
        "testo_bando":values.get("testo_verbale_bando") or "",
    }


@app.get("/api/bandi/{code}/verbali/descrittivi")
def elenco_verbali_descrittivi(code: str):
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b: fail("Bando non trovato",404)
        rows=db.execute("""
            SELECT id,cds,sede,versione,nome_originale,sha256_originale,
                   data_seduta,ora_seduta,autore,creato_il,impronta_sorgente
            FROM verbali_descrittivi WHERE bando_id=? AND attivo=1
            ORDER BY cds COLLATE NOCASE,sede COLLATE NOCASE
        """,(b["id"],)).fetchall()
        result=[]
        for row in rows:
            entry=dict(row)
            try:
                context=_contesto_verbale_seduta(db,b["id"],code,row["cds"],row["sede"])
                entry["sorgente_attuale"]=(
                    impronta_verbale_seduta(context)==row["impronta_sorgente"])
            except HTTPException:
                entry["sorgente_attuale"]=False
            result.append(entry)
    return {"verbali":result}


@app.post("/api/bandi/{code}/verbali/descrittivi")
async def importa_verbale_descrittivo(code: str, request: Request,
                                     cds: str = Form(...), sede: str = Form(...),
                                     file: UploadFile = File(...)):
    """Caricamento nella sezione Verbali, non nelle valutazioni candidate."""
    code=valid_code(code)
    raw=await file.read(8*1024*1024+1)
    if not raw or len(raw)>8*1024*1024:
        fail("Il verbale PDF deve avere dimensione massima di 8 MB",400)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        context=_contesto_verbale_seduta(db,b["id"],code,cds,sede)
    if not context["testo_bando"].strip() or "[DA VERIFICARE]" in context["testo_bando"].upper():
        fail("Inserire e verificare prima il testo ufficiale in Riferimenti del bando",409)
    try:
        data,pdf_statico=acquisisci_verbale_seduta(raw,context)
    except ValueError as exc:
        fail(str(exc),422)
    digest=hashlib.sha256(raw).hexdigest()
    static_digest=hashlib.sha256(pdf_statico).hexdigest()
    source_sha=impronta_verbale_seduta(context)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
        _assert_graduatoria_open(db,b["id"],cds,sede)
        current=_contesto_verbale_seduta(db,b["id"],code,cds,sede)
        if impronta_verbale_seduta(current)!=source_sha:
            fail("Dati del bando modificati durante l'acquisizione; riprovare",409)
        previous=db.execute("""
            SELECT id,versione,sha256_originale FROM verbali_descrittivi
            WHERE bando_id=? AND cds=? AND sede=? AND attivo=1
        """,(b["id"],cds,sede)).fetchone()
        if previous and previous["sha256_originale"]==digest:
            return {"ok":True,"duplicato":True,"versione":previous["versione"]}
        version=int(previous["versione"])+1 if previous else 1
        if previous:
            db.execute("UPDATE verbali_descrittivi SET attivo=0 WHERE id=?",
                       (previous["id"],))
        db.execute("""
            INSERT INTO verbali_descrittivi (
                bando_id,cds,sede,versione,attivo,nome_originale,
                pdf_originale,sha256_originale,pdf_statico,sha256_statico,
                impronta_sorgente,data_seduta,ora_seduta,discussione,autore,creato_il
            ) VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)
        """,(b["id"],cds,sede,version,(file.filename or "verbale.pdf")[:180],
             raw,digest,pdf_statico,static_digest,source_sha,
             data["data_seduta"],data["ora_seduta"],data["riassunto_discussione"],
             request_actor(),datetime.now(timezone.utc).isoformat(timespec="microseconds")))
        db.commit()
    return {"ok":True,"versione":version,"sha256":digest,
            "pagine":len(PdfReader(BytesIO(pdf_statico)).pages)}


@app.get("/api/bandi/{code}/verbali/descrittivi/{document_id}/pdf")
def scarica_verbale_descrittivo(code: str, document_id: int,
                               tipo: str = "statico"):
    code=valid_code(code)
    if tipo not in ("statico","originale"):
        fail("Tipo di documento non valido",400)
    with connect() as db:
        row=db.execute("""
            SELECT v.cds,v.sede,v.pdf_originale,v.sha256_originale,
                   v.pdf_statico,v.sha256_statico
            FROM verbali_descrittivi v JOIN bandi b ON b.id=v.bando_id
            WHERE b.codice=? AND v.id=?
        """,(code,document_id)).fetchone()
    if not row:fail("Verbale descrittivo non trovato",404)
    payload=bytes(row["pdf_"+tipo])
    if hashlib.sha256(payload).hexdigest()!=row["sha256_"+tipo]:
        fail("Integrità del documento non verificata",409)
    name=nome_verbale(code,row["cds"],row["sede"])
    return Response(content=payload,media_type="application/pdf",
        headers={"Content-Disposition":'attachment; filename="'+name+'"',
                 "Cache-Control":"no-store"})


def _pdf_descrittivo_per_chiusura(db, bando_id: int, code: str,
                                 cds: str, sede: str) -> bytes:
    row=db.execute("""
        SELECT pdf_statico,sha256_statico,impronta_sorgente
        FROM verbali_descrittivi
        WHERE bando_id=? AND cds=? AND sede=? AND attivo=1
    """,(bando_id,cds,sede)).fetchone()
    if not row:
        fail("Verbale descrittivo della Commissione obbligatorio: acquisirlo in Verbali",409)
    if hashlib.sha256(bytes(row["pdf_statico"])).hexdigest()!=row["sha256_statico"]:
        fail("Integrità del verbale descrittivo non verificata",409)
    context=_contesto_verbale_seduta(db,bando_id,code,cds,sede)
    if impronta_verbale_seduta(context)!=row["impronta_sorgente"]:
        fail("I dati della Commissione o del bando sono cambiati: riacquisire il verbale",409)
    return bytes(row["pdf_statico"])



# === Assistente AI: controllo dell'ente, esclusivamente Admin ===
class ConfigGroqCloud(BaseModel):
    model: str = Field(default="", max_length=125)
    api_key: str | None = Field(default=None, max_length=256)
    elimina_chiave: bool = False


@app.get("/api/admin/ai/config")
def config_ai(request: Request):
    require_admin(request)
    return {"model":GROQ.model(),"chiave_configurata":bool(GROQ.key()),
            "provider":"GroqCloud","solo_categorie_ente":False,
            "campi_pica_minimizzati":["Azienda/ente","Ateneo","Occupazione",
                                     "Profilo professionale","Qualifica"]}


@app.put("/api/admin/ai/config")
def salva_config_ai(body: ConfigGroqCloud,request: Request):
    require_admin(request)
    if body.elimina_chiave and body.api_key:
        fail("Non è possibile aggiungere ed eliminare contemporaneamente la chiave")
    try:
        GROQ.save("" if body.elimina_chiave else body.api_key,body.model.strip())
    except (ValueError,OSError) as exc:
        fail(str(exc),400)
    return config_ai(request)


@app.get("/api/admin/ai/modelli")
def elenco_modelli_groq(request: Request):
    require_admin(request)
    try:
        found=groq_models(GROQ.key())
        return {"modelli":found,"provider":"GroqCloud","connessione":"verificata"}
    except GroqLimit as exc:
        # In precedenza un HTTP 429 non gestito diventava un generico errore 500.
        raise HTTPException(status_code=429,detail=
            "GroqCloud: limite richieste raggiunto. Riprovare dopo "+
            str(exc.retry_after)+" secondi.",
            headers={"Retry-After":str(exc.retry_after)}) from None
    except ValueError as exc:
        # 424: errore di dipendenza esterna, distinto dal 502 del proxy
        # Home Assistant/Cloudflare. La UI può visualizzare il dettaglio JSON.
        fail(str(exc),424)


@app.post("/api/admin/ai/test")
def prova_modello_groq(request: Request):
    """Una sola richiesta sintetica, priva di qualunque dato PICA."""
    require_admin(request)
    model=GROQ.model()
    key=GROQ.key()
    if not model or not key:
        fail("Configurare prima modello e chiave GroqCloud",409)
    synthetic=prepare_minimal_payload({
        "ente_richiesto":"AOUI",
        "azienda_ente":"AOUI Verona",
        "ateneo":"Universita' di esempio",
        "occupazione":"Dirigente sanitario presso AOUI Verona",
        "profilo_professionale":"Dirigente",
        "qualifica":"Medico",
    })
    try:
        result=groq_analyze(key,model,synthetic)
    except GroqLimit as exc:
        raise HTTPException(status_code=429,detail=
            "GroqCloud: limite richieste raggiunto. Riprovare dopo "+
            str(exc.retry_after)+" secondi.",
            headers={"Retry-After":str(exc.retry_after)}) from None
    except ValueError as exc:
        # 424: errore di dipendenza esterna, distinto dal 502 del proxy
        # Home Assistant/Cloudflare. La UI può visualizzare il dettaglio JSON.
        fail(str(exc),424)
    if result.get("origine")=="locale":
        fail("Il test non ha raggiunto GroqCloud",502)
    return {"ok":True,"model":model,
            "chiamata_groq":True,"token_usati":result.get("token_usati"),
            "avviso":"Test effettuato con ente e categorie sintetici, senza dati PICA."}


class ParametriAnalisiEnte(BaseModel):
    code: str
    cds: str = Field(default="", max_length=250)
    sede: str = Field(default="", max_length=100)


def _ai_scope(db,code: str,cds: str,sede: str) -> tuple[int,list[dict]]:
    """Controllo preliminare sugli incarichi PICA importati, non sulle schede PDF.

    Un requisito dell'ente può essere controllato prima della valutazione
    della Commissione. Nessun dato identificativo va a GroqCloud.
    """
    code=valid_code(code)
    b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
    if not b:
        fail("Bando non trovato",404)
    candidates=[r for r in list_candidature(code)["candidature"]
                if r["stato"]=="Attiva"
                and (not cds or r["cds"]==cds)
                and (not sede or r["sede"]==sede)]
    if len(candidates)>2000:
        fail("Ambito troppo grande: filtrare per CdS e sede",409)
    return b["id"],candidates


def _ai_result_rows(db,bando_id: int,candidates: list[dict], model: str) -> tuple[list[dict],list[dict]]:
    candidates_by_key={(int(c["id"]),str(c["incarico_key"])):c for c in candidates}
    records=db.execute("""
        SELECT a.*,i.key AS incarico_key
        FROM ai_ente_verifiche a JOIN incarichi i ON i.id=a.incarico_id
        WHERE a.bando_id=? ORDER BY a.id DESC
    """,(bando_id,)).fetchall()
    valid={}
    for r in records:
        key=(int(r["candidatura_id"]),str(r["incarico_key"]))
        source=candidates_by_key.get(key)
        if source and key not in valid:
            data=prepare_minimal_payload(source)
            if r["sorgente_sha256"]==groq_source_hash(data,model) and r["modello"]==model:
                valid[key]=dict(r)
    results=[]
    pending=[]
    for c in candidates:
        key=(int(c["id"]),str(c["incarico_key"]))
        payload=prepare_minimal_payload(c)
        info=valid.get(key)
        if info:
            results.append({
                "id_pica":str(c["id_pica"]),"cds":c["cds"],"sede":c["sede"],
                "insegnamento":c["insegnamento"],"requisito":info["requisito"],
                "categoria_ente":info["categoria_ente"],
                "categoria_rapporto":info["categoria_rapporto"],
                "indizio":info["indizio"],"motivo":info["motivo"],
                "verifiche":info["verifiche"],"creato_il":info["creato_il"],
                "autore":info["autore"],"modello":info["modello"],
            })
        else:
            pending.append({"documento":c,"payload":payload})
    return results,pending


@app.get("/api/admin/ai/risultati")
def risultati_ai(request: Request,code: str,cds: str="",sede: str=""):
    require_admin(request)
    model=GROQ.model()
    with connect() as db:
        bid,cands=_ai_scope(db,code,cds,sede)
        results,pending=_ai_result_rows(db,bid,cands,model)
    return {"risultati":results,"totale":len(cands),"completati":len(results),
            "da_analizzare":len(pending),"model":model}


@app.post("/api/admin/ai/analizza-passo")
def esegui_passo_ai(body:ParametriAnalisiEnte,request:Request):
    who=require_admin(request)
    code=valid_code(body.code)
    model=GROQ.model()
    key=GROQ.key()
    if not model or not key:
        fail("Configurare prima modello e chiave GroqCloud",409)
    with connect() as db:
        bid,cands=_ai_scope(db,code,body.cds,body.sede)
        results,pending=_ai_result_rows(db,bid,cands,model)
    if not pending:
        return {"completati":len(results),"da_analizzare":0,"nuovi":0,
                "chiamate_groq":0,"token_groq":0,"solo_locale":0}
    count=0
    requests_sent=0
    tokens_used=0
    local_only=0
    for task in pending[:3]:
        original=task["documento"]
        minimal=task["payload"]
        try:
            outcome=groq_analyze(key,model,minimal)
        except GroqLimit as exc:
            return {"completati":len(results)+count,
                    "da_analizzare":len(pending)-count,
                    "nuovi":count,"limite_raggiunto":True,
                    "retry_after":exc.retry_after,
                    "chiamate_groq":requests_sent,"token_groq":tokens_used,
                    "solo_locale":local_only}
        except ValueError as exc:
            # 424 differenzia l'errore del provider dal gateway dell'add-on.
            fail(str(exc),424)
        if outcome.get("origine")=="groqcloud":
            requests_sent+=1
            tokens_used+=int(outcome.get("token_usati") or 0)
        elif outcome.get("origine")=="locale":
            local_only+=1
        source=groq_source_hash(minimal,model)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            fresh=db.execute("""
                SELECT i.id AS incarico_id,c.bando_id,c.stato
                FROM candidature c JOIN incarichi i ON i.id=c.incarico_id
                WHERE c.id=? AND i.key=? AND c.bando_id=?
            """,(original["id"],original["incarico_key"],bid)).fetchone()
            if not fresh or fresh["stato"]!="Attiva":
                fail("La candidatura è cambiata: ripetere il controllo",409)
            # Una graduatoria definitiva non viene modificata neppure dal processo AI.
            db.execute("""
                INSERT OR IGNORE INTO ai_ente_verifiche
                (bando_id,candidatura_id,incarico_id,sorgente_sha256,modello,
                 requisito,categoria_ente,categoria_rapporto,indizio,motivo,
                 verifiche,autore,creato_il)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,(bid,int(original["id"]),int(fresh["incarico_id"]),source,model,
                 minimal["requisito_ente"],minimal["denominazione_ente_dichiarato"],
                 role_category(str(original.get("occupazione") or ""),
                               str(original.get("profilo_professionale") or "")),outcome["indizio"],
                 outcome["motivo"],outcome["verifiche"],who["username"],
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            db.commit()
        count+=1
    return {"completati":len(results)+count,
            "da_analizzare":len(pending)-count,"nuovi":count,
            "limite_raggiunto":False,
            "chiamate_groq":requests_sent,"token_groq":tokens_used,
            "solo_locale":local_only}





# Istruzioni generali del modello, visibili e modificabili SOLO dall'Admin.
# Il prompt di sicurezza/struttura JSON resta fisso e non modificabile.
class AIIndicazioniInput(BaseModel):
    testo: str = Field(min_length=10,max_length=5000)


@app.get("/api/admin/ai/istruzioni")
def leggi_istruzioni_ai(request:Request):
    require_admin(request)
    return {"testo":GROQ.guidance(),"predefinito":REVISED_GUIDANCE,
            "avviso":"Le correzioni v1.0.14 sono gia' attive nel prompt fisso. " 
                     "Sostituire le istruzioni generali e' facoltativo e invalida "
                     "il riutilizzo della cache di tutti i candidati. "
                     "Inserire soltanto indicazioni istituzionali generali, "
                     "mai dati di candidati. Le modifiche valgono per le "
                     "nuove analisi e invalidano il riutilizzo dei vecchi esiti."}


@app.put("/api/admin/ai/istruzioni")
def salva_istruzioni_ai(body:AIIndicazioniInput,request:Request):
    require_admin(request)
    try:
        GROQ.save_guidance(body.testo)
    except (ValueError,OSError) as exc:
        fail(str(exc),400)
    return leggi_istruzioni_ai(request)


# === Verifica requisito ente per bando: coda persistente v7 ===
class AIQuotaConfig(BaseModel):
    rpm: int = Field(default=30,ge=1,le=1000)
    tpm: int = Field(default=8000,ge=100,le=1000000)
    rpd: int = Field(default=1000,ge=1,le=1000000)
    tpd: int = Field(default=200000,ge=100,le=100000000)
    margine: int = Field(default=80,ge=50,le=95)
    stima_token: int = Field(default=950,ge=50,le=50000)


@app.get("/api/admin/ai/limiti")
def leggi_limiti_ai(request:Request):
    require_admin(request)
    return {"limiti":GROQ.limits(),
            "avviso":"Valori configurati per la coda RankUP; le quote reali "
                     "dipendono dall'organizzazione e dal progetto GroqCloud."}


@app.put("/api/admin/ai/limiti")
def salva_limiti_ai(body:AIQuotaConfig,request:Request):
    require_admin(request)
    try:
        GROQ.save_limits(body.model_dump())
    except (ValueError,OSError) as exc:
        fail(str(exc),400)
    return leggi_limiti_ai(request)


@app.get("/api/admin/ai/verifiche")
def verifiche_ai_bando(request:Request,code:str,cds:str="",sede:str="",esito:str="",tipo:str=""):
    """Tutte le candidature PICA, anche quelle mai inviate all'AI."""
    require_admin(request)
    if esito not in ("",NON_APPLICABILE,ENTE_COERENTE,DA_APPROFONDIRE):
        fail("Filtro esito non valido",400)
    if len(tipo)>90:
        fail("Filtro tipo troppo lungo",400)
    with connect() as db:
        bid,candidates=_ai_scope(db,code,cds,sede)
        rows=ai_cases(db,bid,candidates,GROQ.model(),GROQ.guidance())
    counts={NON_APPLICABILE:0,ENTE_COERENTE:0,DA_APPROFONDIRE:0}
    tipi={}
    for entry in rows:
        counts[entry["esito"]]+=1
        label=entry.get("tipo_approfondimento") or "NON DEFINITO"
        tipi[label]=tipi.get(label,0)+1
    if esito:
        rows=[r for r in rows if r["esito"]==esito]
    if tipo:
        rows=[r for r in rows if r.get("tipo_approfondimento")==tipo]
    return {"casi":rows,"totale":len(candidates),"visualizzati":len(rows),
            "conteggi":counts,"conteggi_tipo":tipi,"versione_regole":RULES_VERSION,
            "avvertenza":"La coerenza istituzionale dell'ente non prova il "
                         "rapporto di dipendenza e non determina idoneita'."}


class AccodaAnalisiEnte(ParametriAnalisiEnte):
    conferma_servizio: bool = False
    forza: bool = False
    ids_candidature: list[int] = Field(default_factory=list,max_length=2000)


@app.post("/api/admin/ai/lavori")
def avvia_analisi_ente(body:AccodaAnalisiEnte,request:Request):
    who=require_admin(request)
    if not body.conferma_servizio:
        fail("Confermare l'uso del servizio esterno",400)
    model=GROQ.model()
    if not model or not GROQ.key():
        fail("Configurare prima modello e chiave GroqCloud",409)
    with connect() as db:
        bid,candidates=_ai_scope(db,body.code,body.cds,body.sede)
        b=db.execute("SELECT stato FROM bandi WHERE id=?",(bid,)).fetchone()
        if not b or b["stato"]=="Archiviato":
            fail("Bando archiviato: non si possono avviare nuove analisi",409)
        selected=body.ids_candidature
        if selected:
            ids=set(selected)
            if len(ids)!=len(selected) or any(i<=0 for i in ids):
                fail("Selezione candidature non valida o duplicata",400)
            available={int(c["id"]):c for c in candidates}
            if not ids.issubset(available):
                fail("La selezione contiene candidature non presenti nell'ambito attivo",400)
            candidates=[available[i] for i in selected]
            if not all(classifica_ente(c)["richiede_ai"] for c in candidates):
                fail("Selezione non inviabile: ente assente, requisito non valido o dati protetti",400)
        job=ai_create_job(db,bid,valid_code(body.code),body.cds,body.sede,
                          model,candidates,who["username"],GROQ.guidance(),
                          bool(body.forza or selected),targeted=bool(selected))
        if selected and job.get("gia_esistente"):
            fail("Una coda e' gia' attiva per questo CdS/sede: attenderne il termine",409)
    AI_WORKER_STOP_WAKE.set()
    return {"lavoro":job,"totale_candidature":len(candidates),
            "selezione_mirata":bool(body.ids_candidature),
            "avviso":"Gli esiti locali sono conservati e le chiamate GroqCloud "
                     "usano solo denominazioni di ente minimizzate."}


@app.get("/api/admin/ai/lavori")
def stato_analisi_ente(request:Request,code:str,cds:str="",sede:str=""):
    require_admin(request)
    with connect() as db:
        bid,_=_ai_scope(db,code,cds,sede)
        result=ai_last_job(db,bid,cds,sede)
        quota=ai_quota_info(db,GROQ.model(),GROQ.limits())
    return {"lavoro":result,"quota_locale":quota,
            "avviso_quota":"Il conteggio locale comprende soltanto le chiamate "
                           "della coda RankUP, non altri client o progetti GroqCloud."}


@app.post("/api/admin/ai/lavori/{job_id}/{azione}")
def aggiorna_analisi_ente(job_id:int,azione:str,request:Request):
    require_admin(request)
    if azione not in ("sospendi","annulla","riprendi","riprova_errori"):
        fail("Azione non valida",400)
    with connect() as db:
        try:
            job=ai_job_action(db,job_id,azione)
        except ValueError as exc:
            fail(str(exc),409)
    AI_WORKER_STOP_WAKE.set()
    return {"lavoro":job}


AI_WORKER_STOP = threading.Event()
AI_WORKER_STOP_WAKE = threading.Event()
AI_WORKER_THREAD = None
AI_WORKER_LOCK = threading.Lock()


def _ai_worker_candidates(code:str,cds:str,sede:str) -> list[dict]:
    with connect() as db:
        _,candidates=_ai_scope(db,code,cds,sede)
    return candidates


def _ai_worker_main():
    while not AI_WORKER_STOP.is_set():
        try:
            # Una richiesta per iterazione; denominazioni controllate, non dati candidati.
            did_work=ai_process_one(connect,_ai_worker_candidates,groq_analyze,
                                    GROQ.key,GROQ.limits())
        except Exception as exc:
            # Nessuna dichiarazione PICA, chiave o testo remoto nei log.
            import logging
            logging.getLogger("rankup.ai").error(
                "Worker AI: errore interno di classe %s",type(exc).__name__)
            did_work=False
        AI_WORKER_STOP_WAKE.wait(0.8 if did_work else 2.0)
        AI_WORKER_STOP_WAKE.clear()


@app.on_event("startup")
def avvia_worker_ai():
    global AI_WORKER_THREAD
    if os.getenv("RANKUP_DISABLE_AI_WORKER")=="1":
        return
    with AI_WORKER_LOCK:
        if AI_WORKER_THREAD is not None and AI_WORKER_THREAD.is_alive():
            return
        # Stato in_corso interrotto da riavvio: torna in coda; una richiesta
        # sospesa prima del salvataggio e' ritentata in modo idempotente.
        with connect() as db:
            db.execute("""UPDATE ai_ente_lavori SET stato='in_coda'
                          WHERE stato='in_corso'""")
            db.commit()
        AI_WORKER_STOP.clear()
        AI_WORKER_STOP_WAKE.clear()
        AI_WORKER_THREAD=threading.Thread(target=_ai_worker_main,
                        name="rankup-ai-ente-worker",daemon=True)
        AI_WORKER_THREAD.start()


@app.on_event("shutdown")
def arresta_worker_ai():
    global AI_WORKER_THREAD
    AI_WORKER_STOP.set()
    AI_WORKER_STOP_WAKE.set()
    if AI_WORKER_THREAD is not None:
        AI_WORKER_THREAD.join(timeout=2.5)
        AI_WORKER_THREAD=None


@app.get("/api/admin/ai/report.pdf")
def report_ai_pdf(request: Request,code: str,cds: str="",sede: str="",
                  esito: str="",tipo: str=""):
    """Report per CdS/sede: solo Admin, nessuna chiamata GroqCloud."""
    require_admin(request)
    if esito not in ("",NON_APPLICABILE,ENTE_COERENTE,DA_APPROFONDIRE):
        fail("Filtro esito non valido",400)
    code=valid_code(code)
    with connect() as db:
        bid,candidates=_ai_scope(db,code,cds,sede)
        results=ai_cases(db,bid,candidates,GROQ.model(),GROQ.guidance())
    counts={NON_APPLICABILE:0,ENTE_COERENTE:0,DA_APPROFONDIRE:0}
    for item in results:
        counts[item["esito"]]+=1
    if len(tipo)>90:
        fail("Filtro tipo troppo lungo",400)
    included=[item for item in results
              if (not esito or item["esito"]==esito)
              and (not tipo or item.get("tipo_approfondimento")==tipo)]
    payload=render_ente_report_pdf(
        code,cds or "Tutti i CdS",sede or "Tutte le sedi",
        included,counts,filter_text=" / ".join(x for x in (esito,tipo) if x))
    # Nome leggibile anche quando si scaricano report di CdS differenti:
    # sostituzione conservativa per sistemi Windows.
    def part(value):
        normal=re.sub(r"[^A-Za-z0-9]+","_",
                      unicodedata.normalize("NFKD",str(value)).encode(
                      "ascii","ignore").decode("ascii").upper()).strip("_")
        return (normal or "TUTTI")[:24].rstrip("_")
    label="_".join([part(code),part(cds),part(sede),
                    part(esito or "TUTTI"),part(tipo or "TUTTI")])
    # Digest del filtro originale per evitare collisioni fra CdS molto lunghi.
    unique=hashlib.sha256(
        json.dumps([code,cds,sede,esito,tipo],ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:6].upper()
    filename="RANKUP_ENTE_"+label[:55].rstrip("_")+"_"+unique+".pdf"
    return Response(content=payload,media_type="application/pdf",headers={
        "Content-Disposition":'attachment; filename="'+filename+'"',
        "Cache-Control":"no-store"})

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
                     WHEN UPPER(TRIM(i.ssd))='NN' THEN COALESCE(nn.modello,'ordinario')
                     ELSE COALESCE(cl.tipo,'ordinario') END AS modello
            FROM candidature c JOIN incarichi i ON i.id=c.incarico_id
            LEFT JOIN ssd_classificazioni cl ON cl.ssd=UPPER(TRIM(i.ssd))
            LEFT JOIN incarichi_modelli_scheda nn ON nn.incarico_id=i.id
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
        _require_bando_aperto(db,code)
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
            _require_bando_aperto(db,code)
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
        _require_bando_aperto(db,code)
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
        if total is not None and total > 50:
            tipo = db.execute("""SELECT CASE WHEN UPPER(TRIM(i.ssd))='NN' THEN COALESCE(nn.modello,'ordinario')
                            ELSE cl.tipo END AS tipo
                FROM candidature c
                JOIN incarichi i ON i.id=c.incarico_id
                LEFT JOIN ssd_classificazioni cl ON cl.ssd=UPPER(TRIM(i.ssd))
                LEFT JOIN incarichi_modelli_scheda nn ON nn.incarico_id=i.id
                WHERE c.id=?""",(candidate["id"],)).fetchone()
            if tipo and tipo["tipo"]=="inglese":
                fail("Per il modello Inglese il punteggio massimo e' 50",409)
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
        jobs_conditions=["i.bando_id=?"]
        jobs_args=[b["id"]]
        if cds:
            jobs_conditions.append("i.cds=?")
            jobs_args.append(cds)
        if sede:
            jobs_conditions.append("i.sede=?")
            jobs_args.append(sede)
        # I gruppi sono gli incarichi PICA realmente importati, compresi
        # quelli senza alcuna scheda: nessuna inferenza da nomi di file.
        jobs=db.execute("""
          SELECT i.id,i.key,i.cds,i.sede,i.insegnamento,i.modulo,
                 i.ssd,i.id_copertura,COUNT(c.id) AS candidature
          FROM incarichi i
          LEFT JOIN candidature c ON c.incarico_id=i.id AND c.stato='Attiva'
          WHERE """+" AND ".join(jobs_conditions)+"""
          GROUP BY i.id
          ORDER BY i.cds COLLATE NOCASE,i.sede COLLATE NOCASE,
                   i.insegnamento COLLATE NOCASE,i.modulo COLLATE NOCASE,i.id
        """,jobs_args).fetchall()
        archives=db.execute("""
          SELECT v.id,v.nome_originale,v.stato,v.motivo,v.totale_calcolato,v.versione,
                 v.percorso,v.sha256,
                 v.confermata_il,v.candidatura_id,v.decisione,v.metodo,v.attiva,
                 v.esito,v.note,v.motivazione_tecnica,
                 c.id_pica,c.cognome,c.nome,c.stato AS candidatura_stato,
                 i.id AS incarico_id,i.key AS incarico_key,
                 i.cds,i.sede,i.insegnamento,i.modulo,i.ssd
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
        tie_pending_ids=set()
        for place in {(job["cds"],job["sede"]) for job in jobs}:
            ranking=build_graduatoria(db,b["id"],*place)
            for assignment in ranking["incarichi"]:
                for tie in assignment.get("parita",[]):
                    tie_pending_ids.update(c["candidatura_id"] for c in tie["candidati"])
    # Calcolo in sola lettura: recupera punteggi provvisori delle schede
    # archiviate da versioni precedenti, senza modificarle o convalidarle.
    # Nessun percorso del database viene esposto al client.
    archivio=[]
    safe_dir=(SOURCES/code/"schede_restituite").resolve()
    for source in archives:
        item=dict(source)
        original_path=item.pop("percorso","")
        stored_sha=item.pop("sha256","")
        item["verifica_parita"]=bool(
            item["candidatura_id"] in tie_pending_ids
            and item["stato"]=="Acquisita" and item["attiva"]==1)
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
        "incarichi":[dict(job) for job in jobs],
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
        SELECT g.id,g.stato,g.hash_dati,g.confermata_da,g.confermata_il,
               g.approvazione_id,d.sha256 AS hash_pdf_definitivo,
               CASE WHEN d.pdf_definitivo IS NULL THEN 0 ELSE 1 END AS pdf_definitivo_presente
        FROM graduatorie_cds g
        LEFT JOIN graduatorie_cds_documenti d ON d.graduatoria_id=g.id
        WHERE g.bando_id=? AND g.cds=? AND g.sede=?
    """,(bando_id,cds,sede)).fetchone()
    reopened = (db.execute("""
        SELECT revisione_id FROM graduatorie_cds_riaperte
        WHERE bando_id=? AND graduatoria_id=?
    """,(bando_id,saved["id"])).fetchone() if saved else None)
    ranked["revisioni"] = [dict(x) for x in db.execute("""
        SELECT id,quando FROM graduatorie_cds_revisioni
        WHERE bando_id=? AND cds=? AND sede=? ORDER BY id DESC
    """,(bando_id,cds,sede)).fetchall()]
    ranked["conferma"] = dict(saved) if saved and not reopened else None
    ranked["pdf_definitivo_presente"]=bool(saved and saved["pdf_definitivo_presente"])
    # "Passed" significa acquisita e validata, inclusi esclusi/non idonei motivati.
    ranked["schede_validate"] = sum(
        1 for job in ranked["incarichi"] for c in job["candidature"]
        if c["valutazione_id"] is not None
        and not c.get("verifica_parita")
        and c["decisione"] in ("idoneo", "non_idoneo", "escluso")
        and (c["decisione"] != "idoneo" or c["punteggio"] is not None)
        and (c["decisione"] == "idoneo" or bool(str(c.get("note") or "").strip()))
    )
    ranked["schede_da_risolvere"] = ranked["totale_candidature"] - ranked["schede_validate"]
    ranked["verbale_generabile"] = (
        ranked["pronta"] and ranked["schede_da_risolvere"] == 0
        and (ranked["totale_candidature"] > 0 or ranked["totale_annullate"] > 0)
    )
    ranked["stato"] = (saved["stato"] if saved and not reopened else
                       ("pronta" if ranked["verbale_generabile"] else "in_lavorazione"))
    return ranked


def _assert_graduatoria_open(db, bando_id: int, cds: str, sede: str):
    closed=db.execute("""
        SELECT g.stato,r.graduatoria_id AS riaperta
        FROM graduatorie_cds g
        LEFT JOIN graduatorie_cds_riaperte r ON r.graduatoria_id=g.id
        WHERE g.bando_id=? AND g.cds=? AND g.sede=?
    """,(bando_id,cds,sede)).fetchone()
    if closed and not closed["riaperta"]:
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
        _require_bando_aperto(db,code)
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        _assert_graduatoria_open(db,b["id"],body.cds,body.sede)
        result=_graduatoria_status(db,b["id"],body.cds,body.sede)
        if not result["verbale_generabile"]:
            fail("Impossibile rendere definitiva la graduatoria: "+
                 "; ".join(result["criticita"][:8]),409)
        stamp=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        snapshot=json.dumps(result,ensure_ascii=False,sort_keys=True)
        digest=hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        # La generazione (incluso l'appiattimento delle schede) precede la
        # registrazione di "definitiva". Fallimento = nessuna chiusura.
        pdf_finale=_costruisci_verbale_completo(
            db,b["id"],code,dict(result,stato="definitiva"))
        hash_pdf=hashlib.sha256(pdf_finale).hexdigest()
        reopened=db.execute("""
            SELECT g.id FROM graduatorie_cds g
            JOIN graduatorie_cds_riaperte r ON r.graduatoria_id=g.id
            WHERE g.bando_id=? AND g.cds=? AND g.sede=?
        """,(b["id"],body.cds,body.sede)).fetchone()
        if reopened:
            graduatoria_id=reopened["id"]
            db.execute("""
                UPDATE graduatorie_cds
                SET stato='definitiva',dati_json=?,hash_dati=?,
                    confermata_da=?,confermata_il=?,approvazione_id=NULL
                WHERE id=? AND bando_id=?
            """,(snapshot,digest,request_actor(),stamp,graduatoria_id,b["id"]))
            db.execute("DELETE FROM graduatorie_cds_riaperte WHERE graduatoria_id=?",
                       (graduatoria_id,))
        else:
            cursor=db.execute("""
                INSERT INTO graduatorie_cds
                (bando_id,cds,sede,stato,dati_json,hash_dati,confermata_da,confermata_il)
                VALUES(?,?,?,'definitiva',?,?,?,?)
            """,(b["id"],body.cds,body.sede,snapshot,digest,request_actor(),stamp))
            graduatoria_id=cursor.lastrowid
        db.execute("""
            INSERT INTO graduatorie_cds_documenti
            (graduatoria_id,bando_id,pdf_definitivo,sha256,creato_il)
            VALUES(?,?,?,?,?)
            ON CONFLICT(graduatoria_id) DO UPDATE SET
                pdf_definitivo=excluded.pdf_definitivo,
                sha256=excluded.sha256,creato_il=excluded.creato_il
        """,(graduatoria_id,b["id"],pdf_finale,hash_pdf,stamp))
        db.execute("""INSERT INTO graduatorie_eventi
            (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
            VALUES(?,?,?,?,?,?,?)
        """,(b["id"],"cds",graduatoria_id,"Graduatoria definitiva",
              json.dumps({"cds":body.cds,"sede":body.sede,
                          "hash":digest,"hash_pdf":hash_pdf}),
              request_actor(),stamp))
        db.commit()
    return {"ok":True,"stato":"definitiva","hash":digest,"hash_pdf":hash_pdf}


@app.post("/api/bandi/{code}/verbali/graduatorie/genera-definitivo")
def genera_verbale_con_chiusura(code: str, body: FinalizzaGraduatoria, request: Request):
    """Genera un PDF statico e registra definitiva nella stessa transazione."""
    return finalizza_graduatoria_cds(code,body,request)


class RegredisciStato(BaseModel):
    motivazione: str = Field(min_length=10,max_length=1000)
    riferimento: str = Field(default="",max_length=500)


def _motivo_regressione(body: RegredisciStato, riferimento_obbligatorio: bool = False):
    motivo=body.motivazione.strip()
    riferimento=body.riferimento.strip()
    if len(motivo)<10 or "[DA VERIFICARE]" in motivo.upper():
        fail("Indicare una motivazione concreta di almeno 10 caratteri",400)
    if riferimento_obbligatorio and (len(riferimento)<5 or "[DA VERIFICARE]" in riferimento.upper()):
        fail("Indicare gli estremi verificati del provvedimento che dispone la regressione",400)
    return motivo,riferimento


class RegredisciGraduatoriaCdS(RegredisciStato):
    cds: str = Field(min_length=1,max_length=250)
    sede: str = Field(min_length=1,max_length=100)


@app.post("/api/bandi/{code}/verbali/graduatorie/regredisci-stato")
def regredisci_stato_cds(code: str, body: RegredisciGraduatoriaCdS, request: Request):
    require_admin(request)
    code=valid_code(code)
    motivo,riferimento=_motivo_regressione(body)
    # Genera prima la copia archivistica completa (frontespizio e schede).
    # Nessun atto è sbloccato se gli originali non sono verificabili.
    with connect() as check:
        b=check.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        previous=check.execute("""
            SELECT id,stato,dati_json,hash_dati FROM graduatorie_cds
            WHERE bando_id=? AND cds=? AND sede=?
        """,(b["id"],body.cds,body.sede)).fetchone()
        if not previous or previous["stato"]!="definitiva":
            fail("Solo un verbale CdS definitivo, non approvato, può essere riaperto",409)
        if check.execute("""
            SELECT 1 FROM graduatorie_cds_riaperte WHERE graduatoria_id=?
        """,(previous["id"],)).fetchone():
            fail("Il verbale CdS è già stato riaperto",409)
        old_hash=previous["hash_dati"]
    archival_pdf=pdf_verbale_cds_con_schede(code,body.cds,body.sede,request).body
    stamp=datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        row=db.execute("""
            SELECT id,stato,dati_json,hash_dati FROM graduatorie_cds
            WHERE bando_id=? AND cds=? AND sede=?
        """,(b["id"],body.cds,body.sede)).fetchone()
        if (not row or row["stato"]!="definitiva" or row["hash_dati"]!=old_hash
                or db.execute("SELECT 1 FROM graduatorie_cds_riaperte WHERE graduatoria_id=?",
                              (row["id"],)).fetchone()):
            fail("Stato o contenuto modificati durante la richiesta: riprovare",409)
        drafts=db.execute("""
            SELECT dati_json FROM graduatorie_approvazioni
            WHERE bando_id=? AND stato='bozza'
        """,(b["id"],)).fetchall()
        if any(row["id"] in json.loads(d["dati_json"]).get("ids",[]) for d in drafts):
            fail("Annullare prima la bozza di approvazione generale che include questo CdS",409)
        revision=db.execute("""
            INSERT INTO graduatorie_cds_revisioni
            (bando_id,graduatoria_id,cds,sede,dati_json,hash_dati,
             pdf_storico,hash_pdf,motivazione,riferimento,autore,quando)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,(b["id"],row["id"],body.cds,body.sede,row["dati_json"],old_hash,
             archival_pdf,hashlib.sha256(archival_pdf).hexdigest(),
             motivo,riferimento,request_actor(),stamp))
        db.execute("""
            INSERT INTO graduatorie_cds_riaperte(bando_id,graduatoria_id,revisione_id)
            VALUES(?,?,?)
        """,(b["id"],row["id"],revision.lastrowid))
        db.execute("""INSERT INTO graduatorie_eventi
            (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
            VALUES(?,?,?,?,?,?,?)
        """,(b["id"],"cds",row["id"],"Regressione da definitiva a lavorabile",
             json.dumps({"revisione_id":revision.lastrowid,"hash_precedente":old_hash,
                         "motivazione":motivo,"riferimento":riferimento},ensure_ascii=False),
             request_actor(),stamp))
        db.commit()
    return {"ok":True,"stato":"lavorabile","revisione_id":revision.lastrowid}


@app.get("/api/bandi/{code}/verbali/graduatorie/revisioni/{revision_id}/pdf")
def pdf_verbale_cds_storico(code: str, revision_id: int, inline: bool = False):
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        row=db.execute("""
            SELECT cds,sede,pdf_storico,hash_pdf FROM graduatorie_cds_revisioni
            WHERE bando_id=? AND id=?
        """,(b["id"],revision_id)).fetchone()
    if not row:fail("Versione storica del verbale non trovata",404)
    pdf=row["pdf_storico"]
    if hashlib.sha256(pdf).hexdigest()!=row["hash_pdf"]:
        fail("Integrità del verbale archiviato non verificata",409)
    filename=("RankUP_"+_safe_filename(code,40)+"_verbale_storico_"+
              _safe_filename(row["cds"],40)+"_"+str(revision_id)+".pdf")
    return Response(content=pdf,media_type="application/pdf",
        headers={"Content-Disposition":("inline" if inline else "attachment")+'; filename="'+filename+'"',
                 "Cache-Control":"no-store"})


class RisolviParita(BaseModel):
    punteggio: str = Field(min_length=1,max_length=30)
    ordine_candidature: list[int] = Field(min_length=2,max_length=100)
    motivazione: str = Field(min_length=10,max_length=1000)
    riferimento: str = Field(min_length=5,max_length=500)


@app.post("/api/bandi/{code}/verbali/graduatorie/{incarico_id}/parita")
def registra_ordine_parita(code: str, incarico_id: int, body: RisolviParita,
                          request: Request):
    """Trascrizione dell'ordine comunicato dalla Commissione, punteggi immutati."""
    require_admin(request)
    code=valid_code(code)
    motivo=body.motivazione.strip()
    ref=body.riferimento.strip()
    if (len(motivo)<10 or len(ref)<5 or "[DA VERIFICARE]" in ref.upper()
            or "[DA VERIFICARE]" in motivo.upper()):
        fail("Servono motivazione e riferimento documentale verificato della Commissione",400)
    if len(set(body.ordine_candidature))!=len(body.ordine_candidature):
        fail("La precedenza non può ripetere un candidato",400)
    try:
        score=str(Decimal(body.punteggio).quantize(Decimal("0.01")))
    except Exception:
        fail("Punteggio di parità non valido",400)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        job=db.execute("""SELECT cds,sede FROM incarichi
            WHERE id=? AND bando_id=?""",(incarico_id,b["id"])).fetchone()
        if not job:fail("Incarico non trovato",404)
        _assert_graduatoria_open(db,b["id"],job["cds"],job["sede"])
        rank=build_graduatoria(db,b["id"],job["cds"],job["sede"])
        assignment=next((x for x in rank["incarichi"] if x["id"]==incarico_id),None)
        tied=next((x for x in (assignment or {}).get("parita",[])
                   if x["punteggio"]==score),None)
        if not tied:
            fail("Nessuna parità da verificare per l'incarico e il punteggio indicati",409)
        expected={x["candidatura_id"] for x in tied["candidati"]}
        if set(body.ordine_candidature)!=expected or len(body.ordine_candidature)!=len(expected):
            fail("L'ordine deve contenere esattamente tutti i candidati in parità",409)
        details={str(c["candidatura_id"]):c["valutazione_id"] for c in assignment["candidature"]
                 if c["candidatura_id"] in expected}
        if any(not value for value in details.values()) or len(details)!=len(expected):
            fail("Schede in parità non più acquisite o da verificare",409)
        now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        db.execute("""INSERT INTO graduatorie_parita_risoluzioni
            (bando_id,incarico_id,punteggio,ordine_json,valutazioni_json,
             motivazione,riferimento,autore,quando)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(bando_id,incarico_id,punteggio) DO UPDATE SET
             ordine_json=excluded.ordine_json,
             valutazioni_json=excluded.valutazioni_json,
             motivazione=excluded.motivazione,riferimento=excluded.riferimento,
             autore=excluded.autore,quando=excluded.quando""",
            (b["id"],incarico_id,score,json.dumps(body.ordine_candidature),
             json.dumps(details,sort_keys=True),motivo,ref,request_actor(),now))
        db.execute("""INSERT INTO graduatorie_eventi
            (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
            VALUES(?,?,?,?,?,?,?)""",
            (b["id"],"incarico",incarico_id,"Ordine di precedenza parità documentato",
             json.dumps({"punteggio":score,"ordine_candidature":body.ordine_candidature,
                         "valutazioni":details,"motivazione":motivo,
                         "riferimento":ref},ensure_ascii=False),request_actor(),now))
        db.commit()
    return {"ok":True,"ordine_candidature":body.ordine_candidature,"punteggio":score}


class CambiaVincitoreProposto(BaseModel):
    candidatura_id: int = Field(gt=0)
    motivazione: str = Field(min_length=10,max_length=1000)
    riferimento: str = Field(min_length=5,max_length=500)


@app.put("/api/bandi/{code}/verbali/graduatorie/{incarico_id}/vincitore")
def vincitore_proposto_istruttorio(code: str,incarico_id: int,
                                 body: CambiaVincitoreProposto,request: Request):
    require_admin(request)
    code=valid_code(code)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        job=db.execute("""SELECT cds,sede FROM incarichi WHERE id=? AND bando_id=?""",
                       (incarico_id,b["id"])).fetchone()
        if not job:fail("Incarico non trovato",404)
        _assert_graduatoria_open(db,b["id"],job["cds"],job["sede"])
        choice=db.execute("""
            SELECT c.id FROM candidature c
            JOIN valutazioni_pdf v ON v.candidatura_id=c.id
                 AND v.attiva=1 AND v.stato='Acquisita' AND v.decisione='idoneo'
            WHERE c.id=? AND c.incarico_id=? AND c.stato='Attiva'
                AND v.totale_calcolato IS NOT NULL
        """,(body.candidatura_id,incarico_id)).fetchone()
        if not choice:
            fail("Il nominativo della Commissione deve avere una scheda idonea acquisita",409)
        # L'Admin trascrive soltanto una decisione della Commissione su parita'.
        # Non puo' proporre un vincitore con punteggio inferiore.
        ranking=build_graduatoria(db,b["id"],job["cds"],job["sede"])
        assignment=next((x for x in ranking["incarichi"] if x["id"]==incarico_id),None)
        if not assignment:
            fail("Incarico non trovato",404)
        top=[c for c in assignment["candidature"]
             if c.get("decisione")=="idoneo" and c.get("posizione")==1]
        if len(top)<2:
            fail("Punteggio massimo univoco: il nominativo deriva dalle schede della "
                 "Commissione e non e' modificabile dall'Admin",409)
        if body.candidatura_id not in {c["candidatura_id"] for c in top}:
            fail("La decisione documentata puo' risolvere solo la parita' al primo posto",409)
        if (not body.motivazione.strip() or not body.riferimento.strip()
                or "[DA VERIFICARE]" in body.riferimento.upper()):
            fail("Necessari motivazione e riferimento documentale verificato della Commissione",400)
        before=db.execute("""SELECT * FROM graduatorie_vincitori_proposti
            WHERE bando_id=? AND incarico_id=?""",(b["id"],incarico_id)).fetchone()
        now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        db.execute("""INSERT INTO graduatorie_vincitori_proposti
            (bando_id,incarico_id,candidatura_id,motivazione,riferimento,autore,quando)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(bando_id,incarico_id) DO UPDATE SET
            candidatura_id=excluded.candidatura_id,motivazione=excluded.motivazione,
            riferimento=excluded.riferimento,autore=excluded.autore,quando=excluded.quando
        """,(b["id"],incarico_id,body.candidatura_id,
             body.motivazione.strip(),body.riferimento.strip(),request_actor(),now))
        db.execute("""INSERT INTO graduatorie_eventi
            (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
            VALUES(?,?,?,?,?,?,?)
        """,(b["id"],"incarico",incarico_id,"Decisione della Commissione su parita' registrata dall'Admin",
             json.dumps({"precedente":dict(before) if before else None,
                         "candidatura_id":body.candidatura_id,
                         "motivazione":body.motivazione.strip(),
                         "riferimento":body.riferimento.strip()},ensure_ascii=False),
             request_actor(),now))
        db.commit()
    return {"ok":True}


@app.get("/api/bandi/{code}/verbali/graduatorie/pdf")
def pdf_graduatoria_cds(code: str, cds: str, sede: str, request: Request):
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        saved=db.execute("""SELECT g.stato,g.dati_json FROM graduatorie_cds g
            LEFT JOIN graduatorie_cds_riaperte r ON r.graduatoria_id=g.id
            WHERE g.bando_id=? AND g.cds=? AND g.sede=?
              AND r.graduatoria_id IS NULL""",(b["id"],cds,sede)).fetchone()
        if saved:
            snapshot=json.loads(saved["dati_json"])
            snapshot["stato"]=saved["stato"]
        else:
            snapshot=_graduatoria_status(db,b["id"],cds,sede)
            snapshot["stato"]="BOZZA NON DEFINITIVA"
        # Integra in sola lettura eventuali UL/partizioni da incarichi PICA
        # anche per snapshot CdS anteriori. Non riscrive i PDF definitivi.
        snapshot=enrich_print_metadata(db,b["id"],snapshot)
    if not snapshot["incarichi"]:
        fail("Nessun incarico trovato",404)
    if not snapshot.get("pronta") or snapshot.get("criticita"):
        fail("Verbale non generabile: schede mancanti, incomplete o criticita' non risolte",409)
    data=render_graduatoria_pdf(code,snapshot)
    filename="RankUP_"+_safe_filename(code,40)+"_graduatoria_"+_safe_filename(cds,50)+".pdf"
    return Response(content=data,media_type="application/pdf",
        headers={"Content-Disposition":'attachment; filename="'+filename+'"',"Cache-Control":"no-store"})


def _costruisci_verbale_completo(db, bando_id: int, code: str, snapshot: dict) -> bytes:
    """Congela i valori delle schede: non altera gli originali restituiti."""
    cds,sede=snapshot["cds"],snapshot["sede"]
    if (not snapshot.get("incarichi") or not snapshot.get("pronta") or
            snapshot.get("criticita") or (
                snapshot.get("totale_candidature",0)<=0 and
                snapshot.get("totale_annullate",0)<=0)):
        fail("Verbale non generabile: tutte le schede devono essere acquisite e "
             "validate e le criticita' risolte",409)
    ids=[]
    for job in snapshot["incarichi"]:
        ordered=sorted(job["candidature"],key=lambda c:(
            0 if c.get("decisione")=="idoneo" else 1,
            c["posizione"] if c.get("posizione") is not None else 999999,
            str(c.get("id_pica") or ""),c["candidatura_id"]))
        for candidate in ordered:
            docid=candidate.get("valutazione_id")
            if docid is None:
                fail("Scheda mancante: verbale unico non generabile",409)
            ids.append(int(docid))
    if len(ids)!=len(set(ids)):
        fail("Scheda duplicata negli allegati: verificare le associazioni",409)
    attachments=[]
    root=(SOURCES/code/"schede_restituite").resolve()
    for pdf_id in ids:
        row=db.execute("""SELECT v.percorso,v.sha256,v.candidatura_id,
                    v.stato,v.attiva,c.id_pica,i.cds,i.sede
            FROM valutazioni_pdf v
            JOIN candidature c ON c.id=v.candidatura_id
            JOIN incarichi i ON i.id=c.incarico_id
            WHERE v.id=? AND v.bando_id=?""",(pdf_id,bando_id)).fetchone()
        if (not row or row["stato"]!="Acquisita" or row["attiva"]!=1 or
                row["cds"]!=cds or row["sede"]!=sede):
            fail("Scheda non acquisita/associata alla graduatoria selezionata",409)
        location=(BASE/row["percorso"]).resolve()
        if not location.is_relative_to(root) or not location.is_file():
            fail("Originale scheda non disponibile per ID PICA "+str(row["id_pica"]),409)
        if location.stat().st_size>MAX_PDF_BYTES:
            fail("Originale scheda oltre il limite consentito",409)
        payload=location.read_bytes()
        if hashlib.sha256(payload).hexdigest()!=row["sha256"]:
            fail("Integrita originale PDF non verificata: ID PICA "+str(row["id_pica"]),409)
        attachments.append(payload)
    if not attachments and snapshot.get("totale_candidature",0)>0:
        fail("Nessuna scheda compilata e acquisita da allegare per questo CdS",409)
    # Prima pagina: verbale descrittivo compilato dalla Commissione.
    # La copia statica è reimpaginata integralmente e non presenta
    # i riquadri data/ora della versione PDF compilabile.
    descrittivo=_pdf_descrittivo_per_chiusura(db,bando_id,code,cds,sede)
    graduatoria=render_graduatoria_pdf(code,snapshot)
    merged=PdfWriter()
    merged.append(BytesIO(descrittivo))
    merged.append(BytesIO(graduatoria))
    out=BytesIO()
    merged.write(out)
    front=out.getvalue()
    if not attachments:
        return front
    try:
        return verbale_definitivo(front,attachments)
    except Exception as error:
        # Non confermare la finalizzazione se non possiamo garantire
        # presenza dei valori e assenza di campi interattivi.
        fail("Impossibile creare un PDF definitivo non compilabile: "+
             str(error)[:300],409)


@app.get("/api/bandi/{code}/verbali/graduatorie/pdf-completo")
def pdf_verbale_cds_con_schede(code: str, cds: str, sede: str, request: Request,
                                inline: bool = False):
    """Solo documenti definitivi. I nuovi sono conservati come byte immutabili.

    Gli stati definitivi precedenti all'aggiornamento, privi di copia salvata,
    sono ricostruiti da snapshot e originali verificati, con campi appiattiti.
    Le versioni precedenti alle riaperture restano nello storico.
    """
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        saved=db.execute("""SELECT g.id,g.stato,g.dati_json,
                    d.pdf_definitivo,d.sha256 AS hash_documento
            FROM graduatorie_cds g
            LEFT JOIN graduatorie_cds_riaperte r ON r.graduatoria_id=g.id
            LEFT JOIN graduatorie_cds_documenti d ON d.graduatoria_id=g.id
            WHERE g.bando_id=? AND g.cds=? AND g.sede=?
              AND r.graduatoria_id IS NULL""",(b["id"],cds,sede)).fetchone()
        if not saved:
            fail("Il verbale non e ancora definitivo: usare Genera verbale definitivo",409)
        if saved["pdf_definitivo"] is not None:
            pdf=bytes(saved["pdf_definitivo"])
            if hashlib.sha256(pdf).hexdigest()!=saved["hash_documento"]:
                fail("Integrita del verbale definitivo non verificata",409)
        else:
            fail("PDF definitivo storico non conservato: usare il recupero documentato Admin",409)
    filename=("RankUP_"+_safe_filename(code,40)+"_verbale_schede_"+
              _safe_filename(cds,50)+"_"+_safe_filename(sede,30)+".pdf")
    return Response(content=pdf,media_type="application/pdf",
        headers={"Content-Disposition":("inline" if inline else "attachment")+
                 '; filename="'+filename+'"',"Cache-Control":"no-store"})


@app.post("/api/bandi/{code}/verbali/graduatorie/recupera-pdf")
async def recupera_pdf_definitivo_storico(
    code: str, request: Request, cds: str = Form(...), sede: str = Form(...),
    motivazione: str = Form(...), riferimento: str = Form(...),
    conferma_corrispondenza: bool = Form(False),
    file: UploadFile = File(...),
):
    """Conserva SOLO l'originale PDF adottato verificato dall'Admin; non ricostruisce atti."""
    require_admin(request)
    code=valid_code(code)
    motivo=motivazione.strip()
    ref=riferimento.strip()
    if (len(motivo)<10 or len(ref)<5 or "[DA VERIFICARE]" in ref.upper()
            or "[DA VERIFICARE]" in motivo.upper() or not conferma_corrispondenza):
        fail("Richiesti riferimento, motivazione e conferma di corrispondenza con l'atto",400)
    limit=64*1024*1024
    raw=await file.read(limit+1)
    if not raw or len(raw)>limit or not raw.startswith(b"%PDF-"):
        fail("Documento PDF assente, non valido o troppo grande",400)
    try:
        reader=PdfReader(BytesIO(raw),strict=False)
        if not reader.pages:
            raise ValueError("Nessuna pagina")
    except Exception:
        fail("Il PDF storico non è leggibile",400)
    digest=hashlib.sha256(raw).hexdigest()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        g=db.execute("""SELECT g.id,g.stato,g.dati_json,
                   d.graduatoria_id AS pdf_salvato,
                   r.graduatoria_id AS riaperta
                   FROM graduatorie_cds g
                   LEFT JOIN graduatorie_cds_documenti d
                     ON d.graduatoria_id=g.id
                   LEFT JOIN graduatorie_cds_riaperte r
                     ON r.graduatoria_id=g.id
                   WHERE g.bando_id=? AND g.cds=? AND g.sede=?""",
                     (b["id"],cds,sede)).fetchone()
        if not g or g["riaperta"] or g["stato"] not in ("definitiva","approvata"):
            fail("Verbale CdS non definitivo o non associato a questa procedura",409)
        if g["pdf_salvato"]:
            fail("PDF definitivo già conservato: non è consentito sovrascriverlo",409)
        snapshot=json.loads(g["dati_json"])
        if snapshot.get("cds")!=cds or snapshot.get("sede")!=sede:
            fail("I dati della graduatoria non corrispondono al CdS/sede",409)
        now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        db.execute("""INSERT INTO graduatorie_cds_documenti
            (graduatoria_id,bando_id,pdf_definitivo,sha256,creato_il)
            VALUES(?,?,?,?,?)""",(g["id"],b["id"],raw,digest,now))
        db.execute("""INSERT INTO graduatorie_cds_documenti_recuperi
            (graduatoria_id,bando_id,nome_file,sha256,motivazione,
             riferimento,autore,quando) VALUES(?,?,?,?,?,?,?,?)""",
            (g["id"],b["id"],(file.filename or "documento_storico.pdf")[:250],
             digest,motivo,ref,request_actor(),now))
        db.execute("""INSERT INTO graduatorie_eventi
            (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
            VALUES(?,?,?,?,?,?,?)""",
            (b["id"],"cds",g["id"],"Recupero PDF storico effettivamente adottato",
             json.dumps({"sha256":digest,"riferimento":ref,
                         "motivazione":motivo},ensure_ascii=False),
             request_actor(),now))
        db.commit()
    return {"ok":True,"sha256":digest,"cds":cds,"sede":sede}


# Chiusura formale del bando con ZIP unico e consultazione perpetua.
# Gli archivi si trovano in /data/archivi_bandi, fuori dalle directory
# interessate dall'azzeramento operativo.
@app.get("/api/archivio")
def elenco_archivio_generale(request: Request):
    require_admin(request)
    with connect() as db:
        rows=db.execute("""
            SELECT id,codice,nome_file,sha256,bytes,file_count,creato_da,
                   creato_il,riaperto_il,riaperto_da,stato_precedente,
                   motivo_riapertura,riferimento_riapertura
            FROM bandi_archivi ORDER BY id DESC
        """).fetchall()
        active={row["codice"]:row["stato"] for row in
                db.execute("SELECT codice,stato FROM bandi")}
    return {"archivi":[dict(row,stato_bando=active.get(row["codice"])) for row in rows]}


@app.get("/api/bandi/{code}/archivio/verifica")
def verifica_ultima_fase_bando(code: str, request: Request):
    require_admin(request)
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT * FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        result=controlla_chiusura(db,b["id"])
        if b["stato"]=="Archiviato":
            result["pronto"]=False
            result["criticita"].append("Bando già archiviato.")
        latest=db.execute("""SELECT id,nome_file,sha256,creato_il,riaperto_il
                             FROM bandi_archivi WHERE codice=? ORDER BY id DESC LIMIT 1""",
                          (code,)).fetchone()
    return {**result,"stato_bando":b["stato"],
            "ultimo_archivio":dict(latest) if latest else None}


def _chiudi_bando_con_snapshot(code: str, actor: str) -> dict:
    """Prepara uno ZIP da una copia coerente di SQLite, senza tenere
    BEGIN IMMEDIATE durante la compressione o la verifica dei file.

    Se il DB cambia anche per un bando diverso durante la preparazione,
    si rifiuta prudenzialmente la chiusura: basta riprovare.
    """
    code=valid_code(code)
    ARCHIVI.mkdir(mode=0o700,parents=True,exist_ok=True)
    ARCHIVI.chmod(0o700)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename="rankup_bando_"+code+"_"+stamp+"_"+secrets.token_hex(5)+".zip"
    temp=ARCHIVI/(filename+".tmp")
    snap_path=ARCHIVI/(filename+".sqlite.tmp")
    output=ARCHIVI/filename
    created=False
    try:
        snap_path.touch(mode=0o600,exist_ok=False)
        snap_path.chmod(0o600)
        # Le connessioni rimangono aperte, senza transazione di lettura,
        # fino al commit finale per controllare PRAGMA data_version.
        with closing(connect()) as live, closing(sqlite3.connect(snap_path)) as snapshot:
            snapshot.row_factory=sqlite3.Row
            version_before=live.execute("PRAGMA data_version").fetchone()[0]
            live.backup(snapshot,pages=256,sleep=0.05)
            version_after=live.execute("PRAGMA data_version").fetchone()[0]
            if version_before!=version_after:
                raise ArchivioNonPronto(
                    "Database aggiornato durante la fotografia: ripetere la chiusura.")
            snapshot.execute("PRAGMA query_only=ON")
            b=snapshot.execute("SELECT * FROM bandi WHERE codice=?",(code,)).fetchone()
            if not b:fail("Bando non trovato",404)
            if b["stato"]=="Archiviato":
                fail("Bando già archiviato: scarica lo ZIP esistente.",409)
            status=controlla_chiusura(snapshot,b["id"])
            if not status["pronto"]:
                fail("Chiusura non possibile: "+"; ".join(status["criticita"]),409)

            def cds_pdf(cds,sede):
                row=snapshot.execute("""
                    SELECT g.stato,g.dati_json,d.pdf_definitivo,d.sha256
                    FROM graduatorie_cds g
                    LEFT JOIN graduatorie_cds_documenti d ON d.graduatoria_id=g.id
                    WHERE g.bando_id=? AND g.cds=? AND g.sede=?
                """,(b["id"],cds,sede)).fetchone()
                if not row or row["stato"]!="approvata":
                    raise ArchivioNonPronto("CdS non approvato.")
                if row["pdf_definitivo"] is not None:
                    payload=bytes(row["pdf_definitivo"])
                    if hashlib.sha256(payload).hexdigest()!=row["sha256"]:
                        raise ArchivioNonPronto("Documento definitivo alterato: "+cds)
                    return payload
                raise ArchivioNonPronto("PDF definitivo storico mancante: "+cds)

            def approval_pdf(row):
                printable=enrich_print_metadata(
                    snapshot,b["id"],json.loads(row["dati_json"]))
                return render_graduatoria_pdf(
                    code,printable,general=True,
                    approved=True,reference=row["estremi_atto"])

            metadata=crea_pacchetto(snapshot,dict(b),SOURCES,temp,
                                   cds_pdf,approval_pdf)
            temp.chmod(0o600)
            # Il solo blocco di scrittura resta qui: nessuna compressione ZIP
            # né generazione PDF mentre altri operatori attendono SQLite.
            live.execute("BEGIN IMMEDIATE")
            current_version=live.execute("PRAGMA data_version").fetchone()[0]
            if current_version!=version_after:
                raise ArchivioNonPronto(
                    "Sono stati modificati dati durante la preparazione: "
                    "verificare e ripetere la chiusura.")
            current=live.execute("SELECT stato FROM bandi WHERE id=?",
                                 (b["id"],)).fetchone()
            if not current or current["stato"]!=b["stato"]:
                raise ArchivioNonPronto(
                    "Stato del bando cambiato: verificare e ripetere la chiusura.")
            # Prima di registrare l'archivio è già stato verificato l'hash
            # di ogni elemento ZIP, contenuti originali e dati dello snapshot.
            os.replace(temp,output)
            created=True
            now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
            cur=live.execute("""
                INSERT INTO bandi_archivi
                (codice,nome_file,sha256,bytes,file_count,creato_da,
                 creato_il,stato_precedente,riepilogo_json)
                VALUES(?,?,?,?,?,?,?,?,?)
            """,(code,filename,metadata["sha256"],metadata["bytes"],
                 metadata["file_count"],actor,now,b["stato"],
                 json.dumps(metadata["riepilogo"],ensure_ascii=False)))
            live.execute("UPDATE bandi SET stato='Archiviato' WHERE id=?",
                         (b["id"],))
            live.commit()
            return {"ok":True,"archivio_id":cur.lastrowid,"nome_file":filename,
                    "sha256":metadata["sha256"],"dimensione":metadata["bytes"],
                    "file_count":metadata["file_count"],
                    "cds_sedi":status["cds_sedi_totali"],
                    "approvazioni":status["approvazioni_completate"]}
    except (ArchivioNonPronto,sqlite3.Error,OSError,ValueError,
            zipfile.BadZipFile) as error:
        if created:
            output.unlink(missing_ok=True)
        fail("Archivio non creato: "+str(error),409)
    finally:
        temp.unlink(missing_ok=True)
        for path in (snap_path,snap_path.with_name(snap_path.name+"-wal"),
                     snap_path.with_name(snap_path.name+"-shm")):
            path.unlink(missing_ok=True)


@app.post("/api/bandi/{code}/archivio/chiudi")
def chiudi_bando_con_zip(code: str, request: Request):
    """Compatibilità API per client esistenti. L'UI usa l'avvio asincrono."""
    who=require_admin(request)
    return _chiudi_bando_con_snapshot(valid_code(code),who["username"])


# Esecuzione in un thread limitato. Il browser interroga soltanto lo stato,
# evitando di tenere aperta una richiesta Cloudflare per tutta la compressione.
_ARCHIVE_POOL=ThreadPoolExecutor(max_workers=1,thread_name_prefix="rankup-zip")
_ARCHIVE_LOCK=threading.Lock()
_ARCHIVE_JOBS={}


def _background_archive(job_id: str, code: str, actor: str) -> None:
    with _ARCHIVE_LOCK:
        _ARCHIVE_JOBS[job_id]["stato"]="in_elaborazione"
    try:
        outcome=_chiudi_bando_con_snapshot(code,actor)
    except Exception as error:
        detail=str(getattr(error,"detail",str(error)))[:700]
        with _ARCHIVE_LOCK:
            _ARCHIVE_JOBS[job_id].update(stato="errore",errore=detail)
    else:
        with _ARCHIVE_LOCK:
            _ARCHIVE_JOBS[job_id].update(stato="completato",risultato=outcome)


@app.post("/api/bandi/{code}/archivio/chiudi/avvia",status_code=202)
def avvia_archiviazione_bando(code: str, request: Request):
    who=require_admin(request)
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id,stato FROM bandi WHERE codice=?",
                     (code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        if b["stato"]=="Archiviato":
            fail("Bando già archiviato",409)
        ready=controlla_chiusura(db,b["id"])
        if not ready["pronto"]:
            fail("Chiusura non possibile: "+"; ".join(ready["criticita"]),409)
    with _ARCHIVE_LOCK:
        for key,job in _ARCHIVE_JOBS.items():
            if job["codice"]==code and job["stato"] in ("in_coda","in_elaborazione"):
                return {"lavoro_id":key,"stato":job["stato"]}
        # Limite per evitare accumulo illimitato in memoria.
        completed=[key for key,job in _ARCHIVE_JOBS.items()
                   if job["stato"] in ("completato","errore")]
        for key in completed[:-30]:
            del _ARCHIVE_JOBS[key]
        job_id=secrets.token_urlsafe(18)
        _ARCHIVE_JOBS[job_id]={"codice":code,"stato":"in_coda",
                               "errore":None,"risultato":None}
        _ARCHIVE_POOL.submit(_background_archive,job_id,code,who["username"])
    return {"lavoro_id":job_id,"stato":"in_coda"}


@app.get("/api/bandi/{code}/archivio/chiudi/lavoro-attivo")
def ultimo_lavoro_archiviazione(code: str,request: Request):
    require_admin(request)
    code=valid_code(code)
    with _ARCHIVE_LOCK:
        available=[(key,job) for key,job in _ARCHIVE_JOBS.items()
                   if job["codice"]==code]
        if not available:
            return {"lavoro_id":None,"stato":None}
        key,job=available[-1]
        return {"lavoro_id":key,"stato":job["stato"],
                "errore":job["errore"],"risultato":job["risultato"]}


@app.get("/api/bandi/{code}/archivio/chiudi/lavoro/{job_id}")
def stato_archiviazione_bando(code: str,job_id: str,request: Request):
    require_admin(request)
    code=valid_code(code)
    with _ARCHIVE_LOCK:
        entry=_ARCHIVE_JOBS.get(job_id)
        if not entry or entry["codice"]!=code:
            fail("Lavoro non disponibile; verificare se il bando è già archiviato",404)
        return {k:v for k,v in entry.items() if k!="codice"}


class RicercaCandidatoStorico(BaseModel):
    testo: str = Field(min_length=2,max_length=120)
    criterio: str = Field(default="tutti",max_length=20)
    pagina: int = Field(default=1,ge=1,le=10000)


@app.post("/api/archivio/ricerca-candidati")
def ricerca_candidati_in_tutti_i_bandi(body: RicercaCandidatoStorico,request: Request):
    """Interroga SOLO il database, inclusi i bandi archiviati.
    
    Ricerca CF esatta, nomi e ID PICA, senza leggere gli ZIP e senza
    modificare valutazioni. POST impedisce il CF nei parametri URL del proxy.
    I valori del CF sono mascherati nelle risposte.
    """
    require_admin(request)
    value=body.testo.strip()
    kind=body.criterio.strip().lower()
    if kind not in ("tutti","cf","cognome","nome","id_pica"):
        fail("Criterio di ricerca non valido",400)
    if kind=="cf":
        cleaned=re.sub(r"[\s-]","",value).upper()
        if not re.fullmatch(r"[A-Z0-9]{11,16}",cleaned):
            fail("Inserire un codice fiscale completo (11-16 caratteri alfanumerici)",400)
    elif len(value)<3:
        fail("Per le ricerche testuali occorrono almeno tre caratteri",400)
    cleaned=re.sub(r"[\s-]","",value).upper()
    normalized_cf=("UPPER(REPLACE(REPLACE(COALESCE(c.codice_fiscale,''),' ',''),'-',''))")
    if kind=="cf":
        predicate=normalized_cf+"=?"
        args=[cleaned]
    elif kind=="cognome":
        predicate="INSTR(LOWER(c.cognome),LOWER(?))>0"
        args=[value]
    elif kind=="nome":
        predicate="INSTR(LOWER(c.nome),LOWER(?))>0"
        args=[value]
    elif kind=="id_pica":
        predicate="INSTR(LOWER(c.id_pica),LOWER(?))>0"
        args=[value]
    else:
        predicate=("("+normalized_cf+"=? AND ? BETWEEN 11 AND 16)"
                   " OR INSTR(LOWER(c.cognome),LOWER(?))>0"
                   " OR INSTR(LOWER(c.nome),LOWER(?))>0"
                   " OR INSTR(LOWER(c.id_pica),LOWER(?))>0")
        args=[cleaned,len(cleaned),value,value,value]
    joins=""" FROM candidature c
                JOIN bandi b ON b.id=c.bando_id
                JOIN incarichi i ON i.id=c.incarico_id
                LEFT JOIN valutazioni_pdf v ON v.candidatura_id=c.id """
    where=" WHERE "+predicate
    with connect() as db:
        totals=db.execute("SELECT COUNT(*) AS righe, COUNT(DISTINCT c.id) AS candidature"+
                          joins+where,args).fetchone()
        page_size=100
        offset=(body.pagina-1)*page_size
        rows=db.execute("""
            SELECT c.id AS candidatura_id,c.codice_fiscale,c.id_pica,c.nome,
                   c.cognome,c.stato AS stato_candidatura,
                   b.codice AS codice_bando,b.stato AS stato_bando,
                   b.import_at,i.cds,i.sede,i.insegnamento,i.modulo,i.ssd,
                   i.key AS incarico_key,
                   v.id AS valutazione_id,v.versione,v.stato AS stato_scheda,
                   v.attiva,v.decisione,v.totale_calcolato,v.esito,v.note,
                   v.confermata_il
        """+joins+where+"""
            ORDER BY b.import_at DESC,b.codice DESC,
                     c.cognome COLLATE NOCASE,c.nome COLLATE NOCASE,
                     i.cds COLLATE NOCASE,i.sede COLLATE NOCASE,
                     i.insegnamento COLLATE NOCASE,
                     c.id DESC,v.versione DESC,v.id DESC
            LIMIT ? OFFSET ?
        """,[*args,page_size,offset]).fetchall()
    result=[]
    for row in rows:
        record=dict(row)
        cf=re.sub(r"[\s-]","",record.pop("codice_fiscale") or "").upper()
        record["cf_mascherato"]=("*"*max(0,len(cf)-4)+cf[-4:]) if cf else None
        record["scheda_corrente"]=bool(record["valutazione_id"] and
                                       record["stato_scheda"]=="Acquisita" and
                                       record["attiva"]==1)
        result.append(record)
    return {"risultati":result,"righe_totali":totals["righe"],
            "candidature_totali":totals["candidature"],
            "pagina":body.pagina,"per_pagina":page_size,
            "pagine":(totals["righe"]+page_size-1)//page_size,
            "criterio":kind,
            "avvertenza_identita":"Solo il codice fiscale presente e corrispondente "
              "consente il collegamento certo tra bandi; i nominativi omonimi "
              "non vengono automaticamente unificati."}


@app.get("/api/archivio/{archive_id}/zip")
def scarica_zip_bando(archive_id: int,request: Request):
    require_admin(request)
    with connect() as db:
        entry=db.execute("""SELECT nome_file,sha256 FROM bandi_archivi WHERE id=?""",
                         (archive_id,)).fetchone()
    if not entry:fail("Archivio non trovato",404)
    folder=ARCHIVI.resolve()
    location=(folder/entry["nome_file"]).resolve()
    if (not location.is_relative_to(folder) or not location.is_file()
            or not re.fullmatch(r"rankup_bando_[A-Z0-9-]+_\d{8}T\d{6}Z_[0-9a-f]{10}\.zip",
                                entry["nome_file"])):
        fail("File ZIP archiviato non disponibile",404)
    with location.open("rb") as stream:
        digest=hashlib.file_digest(stream,"sha256").hexdigest()
    if digest!=entry["sha256"]:
        fail("L'integrità dello ZIP archiviato non corrisponde al registro",409)
    return FileResponse(location,filename=entry["nome_file"],
                        media_type="application/zip",
                        headers={"Cache-Control":"no-store"})


class RiapriBandoArchiviato(BaseModel):
    motivazione: str = Field(min_length=10,max_length=1000)
    riferimento: str = Field(min_length=5,max_length=500)


@app.post("/api/bandi/{code}/archivio/riapri")
def riapri_bando_archiviato(code: str,body: RiapriBandoArchiviato,request: Request):
    require_admin(request)
    code=valid_code(code)
    motivazione=body.motivazione.strip()
    riferimento=body.riferimento.strip()
    if (len(motivazione)<10 or len(riferimento)<5
            or "[DA VERIFICARE]" in riferimento.upper()):
        fail("Motivazione e riferimento al provvedimento formale sono obbligatori",400)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b=db.execute("SELECT id,stato FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b or b["stato"]!="Archiviato":
            fail("Bando non archiviato oppure non più presente",409)
        row=db.execute("""SELECT id,stato_precedente,riaperto_il FROM bandi_archivi
            WHERE codice=? ORDER BY id DESC LIMIT 1""",(code,)).fetchone()
        if not row or row["riaperto_il"]:
            fail("Archivio non coerente con lo stato del bando",409)
        now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        db.execute("""UPDATE bandi_archivi SET riaperto_il=?,riaperto_da=?,
            motivo_riapertura=?,riferimento_riapertura=? WHERE id=?""",
            (now,request_actor(),motivazione,riferimento,row["id"]))
        db.execute("UPDATE bandi SET stato=? WHERE id=?",
                   (row["stato_precedente"],b["id"]))
        db.commit()
    return {"ok":True,"stato":"riaperto",
            "messaggio":"Archivio precedente conservato. Le eventuali rettifiche degli atti "
                        "richiedono le specifiche procedure di regressione."}


class PreparaApprovazione(BaseModel):
    graduatorie_ids: list[int] = Field(min_length=1,max_length=100)


@app.get("/api/bandi/{code}/graduatorie/approvazioni")
def elenco_approvazioni(code: str):
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        rows=db.execute("""SELECT a.id,a.estremi_atto,a.note,
           a.creata_da,a.creata_il,a.approvata_da,a.approvata_il,a.dati_json,
           CASE WHEN r.approvazione_id IS NOT NULL THEN 'regredita'
                ELSE a.stato END AS stato,
           COALESCE(r.motivazione,'') AS regressione_motivazione,
           COALESCE(r.riferimento,'') AS regressione_riferimento,
           COALESCE(r.quando,'') AS regressione_quando
           FROM graduatorie_approvazioni a
           LEFT JOIN graduatorie_approvazioni_regressioni r ON r.approvazione_id=a.id
           WHERE a.bando_id=? ORDER BY a.id DESC
        """,(b["id"],)).fetchall()
    return {"approvazioni":[{
        **{k:row[k] for k in ("id","stato","estremi_atto","note","creata_da","creata_il","approvata_da","approvata_il",
                                  "regressione_motivazione","regressione_riferimento","regressione_quando")},
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
        _require_bando_aperto(db,code)
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
        total_ambiti=db.execute("""
            SELECT COUNT(*) FROM (
                SELECT cds,sede FROM incarichi WHERE bando_id=?
                GROUP BY cds,sede
            )
        """,(b["id"],)).fetchone()[0]
        snapshot={"ids":ids,"selezioni":result,"totale_ambiti_bando":total_ambiti}
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
        _require_bando_aperto(db,code)
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



@app.post("/api/bandi/{code}/graduatorie/approvazioni/{approval_id}/regredisci-stato")
def regredisci_approvazione_generale(code: str, approval_id: int,
                                     body: RegredisciStato, request: Request):
    require_admin(request)
    code=valid_code(code)
    motivo,riferimento=_motivo_regressione(body,riferimento_obbligatorio=True)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        row=db.execute("""
            SELECT stato,dati_json,estremi_atto FROM graduatorie_approvazioni
            WHERE id=? AND bando_id=?
        """,(approval_id,b["id"])).fetchone()
        if not row or row["stato"]!="approvata":
            fail("Solo un'approvazione già registrata può essere regredita",409)
        if db.execute("""
            SELECT 1 FROM graduatorie_approvazioni_regressioni
            WHERE approvazione_id=? AND bando_id=?
        """,(approval_id,b["id"])).fetchone():
            fail("Questa approvazione risulta già regredita",409)
        snapshot=json.loads(row["dati_json"])
        ids=snapshot["ids"]
        if not ids:
            fail("Approvazione senza graduatorie: operazione non consentita",409)
        for gid in ids:
            current=db.execute("""
                SELECT stato,approvazione_id FROM graduatorie_cds
                WHERE id=? AND bando_id=?
            """,(gid,b["id"])).fetchone()
            if not current or current["stato"]!="approvata" or current["approvazione_id"]!=approval_id:
                fail("Una graduatoria CdS non corrisponde più all'approvazione: nessuna modifica eseguita",409)
        stamp=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        for gid in ids:
            db.execute("""
                UPDATE graduatorie_cds SET stato='definitiva',approvazione_id=NULL
                WHERE id=? AND bando_id=? AND stato='approvata' AND approvazione_id=?
            """,(gid,b["id"],approval_id))
        db.execute("""
            INSERT INTO graduatorie_approvazioni_regressioni
            (approvazione_id,bando_id,motivazione,riferimento,autore,quando)
            VALUES(?,?,?,?,?,?)
        """,(approval_id,b["id"],motivo,riferimento,request_actor(),stamp))
        db.execute("""INSERT INTO graduatorie_eventi
            (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
            VALUES(?,?,?,?,?,?,?)
        """,(b["id"],"approvazione",approval_id,
             "Regressione approvazione registrata - storico originale conservato",
             json.dumps({"ids":ids,"estremi_atto_origine":row["estremi_atto"],
                         "motivazione":motivo,"riferimento_provvedimento":riferimento},
                        ensure_ascii=False),request_actor(),stamp))
        db.commit()
    return {"ok":True,"stato":"regredita","graduatorie_riaperte":len(ids)}


@app.delete("/api/bandi/{code}/graduatorie/approvazioni/{approval_id}")
def annulla_bozza_approvazione(code: str, approval_id: int, request: Request):
    require_admin(request)
    code=valid_code(code)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bando_aperto(db,code)
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
def pdf_approvazione(code: str, approval_id: int, request: Request, inline: bool = False):
    code=valid_code(code)
    with connect() as db:
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        row=db.execute("""SELECT stato,estremi_atto,dati_json FROM graduatorie_approvazioni
            WHERE id=? AND bando_id=?""",(approval_id,b["id"])).fetchone()
        if not row:fail("Approvazione non trovata",404)
        payload=enrich_print_metadata(db,b["id"],json.loads(row["dati_json"]))
    pdf=render_graduatoria_pdf(code,payload,general=True,
             approved=row["stato"]=="approvata",reference=row["estremi_atto"])
    return Response(content=pdf,media_type="application/pdf",
        headers={"Content-Disposition":("inline" if inline else "attachment")+
                 '; filename="RankUP_'+_safe_filename(code,40)+'_graduatoria_generale.pdf"',
                 "Cache-Control":"no-store"})


@app.get("/api/bandi/{code}/graduatorie/approvazioni/{approval_id}/prospetto-interno")
def prospetto_nominativo_interno(code: str, approval_id: int, request: Request):
    """Identità complete dallo snapshot approvato, non dai dati correnti."""
    require_admin(request)
    code=valid_code(code)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        b=db.execute("SELECT id FROM bandi WHERE codice=?",(code,)).fetchone()
        if not b:fail("Bando non trovato",404)
        approval=db.execute("""SELECT a.stato,a.estremi_atto,a.dati_json,
                   r.approvazione_id AS regredita
                   FROM graduatorie_approvazioni a
                   LEFT JOIN graduatorie_approvazioni_regressioni r
                     ON r.approvazione_id=a.id
                   WHERE a.id=? AND a.bando_id=?""",(approval_id,b["id"])).fetchone()
        if not approval:
            fail("Approvazione non trovata",404)
        if approval["stato"]!="approvata" or approval["regredita"]:
            fail("Il prospetto nominativo è disponibile solo per atti attualmente approvati",409)
        payload=enrich_print_metadata(db,b["id"],json.loads(approval["dati_json"]))
        pdf=render_graduatoria_pdf(code,payload,general=True,approved=True,
                                  reference=approval["estremi_atto"],internal=True)
        now=datetime.now(timezone.utc).isoformat(timespec="microseconds")
        db.execute("""INSERT INTO graduatorie_eventi
            (bando_id,oggetto,oggetto_id,evento,dettagli_json,autore,quando)
            VALUES(?,?,?,?,?,?,?)""",
            (b["id"],"approvazione",approval_id,"Download prospetto nominativo interno",
             json.dumps({"sha256":hashlib.sha256(pdf).hexdigest()}),
             request_actor(),now))
        db.commit()
    return Response(content=pdf,media_type="application/pdf",
        headers={"Content-Disposition":'attachment; filename="RankUP_'+
                 _safe_filename(code,40)+'_prospetto_nominativo_INTERNO.pdf"',
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

