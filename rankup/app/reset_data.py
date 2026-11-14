"""Azzeramento controllato dell'ambiente dati RankUP.

L'applicazione e le credenziali restano intatte. Snapshot SQLite coerente,
archivio cifrabile dall'utente e originali PDF/PICA inclusi. Nessuna azione
automatica: solo una richiesta esplicita dell'amministratore autenticato.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from zipfile import ZipFile, ZIP_DEFLATED
from db_lifecycle import AutoClosingConnection

RESET_LOCK = threading.Lock()
BUSINESS_TABLES = (
    # Prima i marcatori e le revisioni, poi le tabelle collegate: rispetta le FK.
    "incarichi_modelli_scheda_eventi",
    "incarichi_modelli_scheda",
    "graduatorie_cds_documenti_recuperi",
    "graduatorie_cds_documenti",
    "graduatorie_parita_risoluzioni",
    "candidature_eventi",
    "graduatorie_cds_riaperte",
    "graduatorie_cds_revisioni",
    "graduatorie_approvazioni_regressioni",
    "ai_occupazione_lavori",
    "valutazioni_occupazione_ai",
    "ai_ente_lavori_anomalie",
    "ai_ente_lavori_elementi",
    "ai_ente_ai_esiti",
    "ai_ente_lavori",
    "ai_ente_quota_eventi",
    "ai_ente_verifiche",
    "verbali_descrittivi",
    "valutazioni_pdf_eventi",
    "graduatorie_vincitori_proposti",
    "graduatorie_cds",
    "graduatorie_approvazioni",
    "graduatorie_eventi",
    "incarichi_ente_requisiti",
    "incarichi_ente_eventi",
    "ssd_import_rettifiche",
    "ssd_import_migrations",
    "incarichi_metadata_audit",
    "incarichi_metadata_migrations",
    "commissioni_eventi",
    "bandi_riferimenti_eventi",
    "pica_tipo_cds_eventi",
    "valutazioni_pdf",
    "candidature",
    "segnalazioni_risposte",
    "segnalazioni",
    "commissioni",
    "bandi_riferimenti",
    "incarichi",
    "bandi",
    "pica_tipo_cds_dizionario",
)
OPTIONAL_LEGACY_TABLES = frozenset({"ai_occupazione_lavori", "valutazioni_occupazione_ai"})
PRESERVED_TABLES = ("rankup_utenti", "rankup_utenti_eventi", "ssd_classificazioni", "commissioni_catalogo", "bandi_archivi")
RESET_CONFIRMATION = "AZZERA TUTTI I DATI RANKUP"


def _tables(db):
    return {x[0] for x in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def current_counts(db):
    known=set(BUSINESS_TABLES)|set(PRESERVED_TABLES)
    unexpected=_tables(db)-known
    if unexpected:
        raise ValueError("Tabelle sconosciute: aggiornare la procedura di backup/reset prima di procedere")
    present=_tables(db)
    missing=set(BUSINESS_TABLES)-present-OPTIONAL_LEGACY_TABLES
    if missing:
        raise ValueError("Database incompleto: reset non consentito")
    return {table:(db.execute('SELECT COUNT(*) FROM "'+table+'"').fetchone()[0]
                   if table in present else 0)
            for table in BUSINESS_TABLES}


def preview(db, sources:Path, pending:Path):
    counts=current_counts(db)
    return {
        "bandi":counts["bandi"],"candidature":counts["candidature"],
        "valutazioni":counts["valutazioni_pdf"],
        "graduatorie_cds":counts["graduatorie_cds"],
        "graduatorie_approvazioni":counts["graduatorie_approvazioni"],
        "tabelle_dati":sum(counts.values()),
        "originali":sum(1 for folder in (sources,pending)
                        if folder.exists() for p in folder.rglob("*") if p.is_file()),
        "conferma_richiesta":RESET_CONFIRMATION,
        "preservati":"Credenziali e account, classificazioni SSD, anagrafica commissioni e archivi ZIP finali, configurazione Home Assistant",
        "classificazioni_ssd_conservate":db.execute("SELECT COUNT(*) FROM ssd_classificazioni").fetchone()[0],
        "commissioni_conservate":db.execute("SELECT COUNT(*) FROM commissioni_catalogo").fetchone()[0],
        "zip_finali_conservati":db.execute("SELECT COUNT(*) FROM bandi_archivi").fetchone()[0],
        "backup_obbligatorio":True,
    }


def _archive_tree(archive:ZipFile, root:Path, prefix:str):
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Collegamento simbolico nei documenti: backup annullato")
        if path.is_file():
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("File fuori dalla cartella gestita: backup annullato")
            archive.write(path,prefix+"/"+path.relative_to(root).as_posix())


def reset_with_backup(db_path:Path,sources:Path,pending:Path,backups:Path):
    """Ritorna il nome del backup dopo reset; genera eccezione senza reset se fallisce."""
    if not RESET_LOCK.acquire(blocking=False):
        raise ValueError("Un azzeramento e' gia' in corso")
    temp_db=None
    temp_zip=None
    try:
        backups.mkdir(parents=True,exist_ok=True)
        backups.chmod(0o700)
        stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename="rankup_prima_azzeramento_"+stamp+"_"+secrets.token_hex(5)+".zip"
        temp_db=backups/("snapshot_"+secrets.token_hex(8)+".sqlite3")
        temp_zip=backups/(filename+".tmp")
        final_zip=backups/filename
        with sqlite3.connect(db_path,timeout=20,factory=AutoClosingConnection) as source:
            counts_before=current_counts(source)
            with sqlite3.connect(temp_db,factory=AutoClosingConnection) as target:
                source.backup(target)
                integrity=target.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity!="ok":
                    raise ValueError("Backup SQLite non integro; azzeramento annullato")
        temp_db.chmod(0o600)
        with ZipFile(temp_zip,"w",ZIP_DEFLATED,compresslevel=6) as archive:
            archive.write(temp_db,"rankup.sqlite3")
            _archive_tree(archive,sources,"sources")
            _archive_tree(archive,pending,"pending")
            archive.writestr("manifest.json",json.dumps({
                "formato":"RankUP-reset-backup-v1",
                "creato_il":datetime.now(timezone.utc).isoformat(),
                "tabelle":counts_before,
                "database_sha256":hashlib.sha256(temp_db.read_bytes()).hexdigest(),
                "include":"SQLite, originali PICA/PDF, file temporanei",
                "preservati_dopo_reset":"opzioni, account, SSD, commissioni e archivi finali ZIP conservati nel percorso dedicato",
            },ensure_ascii=False,indent=2))
        with ZipFile(temp_zip) as archive:
            if archive.testzip() is not None:
                raise ValueError("Archivio ZIP non integro; azzeramento annullato")
        temp_zip.chmod(0o600)
        os.replace(temp_zip,final_zip)

        # Non modificare il DB se il contenuto e' cambiato mentre si preparava lo snapshot.
        with sqlite3.connect(db_path,timeout=20,factory=AutoClosingConnection) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                if current_counts(db)!=counts_before:
                    raise ValueError("Dati modificati durante il backup: ripetere il reset")
                present=_tables(db)
                for table in BUSINESS_TABLES:
                    if table in present:
                        db.execute('DELETE FROM "'+table+'"')
                db.commit()
            except Exception:
                db.rollback()
                raise
        # I documenti sono stati copiati nel backup e non servono piu' al database.
        # Rimuovere solo le directory gestite da RankUP, non /data o options.json.
        for folder in (sources,pending):
            if folder.exists():
                shutil.rmtree(folder)
            folder.mkdir(parents=True,exist_ok=True)
            folder.chmod(0o700)
        return filename
    finally:
        if temp_db is not None:
            temp_db.unlink(missing_ok=True)
        if temp_zip is not None:
            temp_zip.unlink(missing_ok=True)
        RESET_LOCK.release()
