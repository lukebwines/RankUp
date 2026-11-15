"""Backup manuali e ripristino protetto dei dati RankUP.

L'archivio include database e documenti, ma NON le opzioni Home Assistant,
i token dei servizi esterni o altri backup. Il ripristino viene applicato
solo all'avvio successivo, prima di creare connessioni applicative.
Le credenziali/account correnti e il registro di sicurezza non retrocedono.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZIP_DEFLATED, BadZipFile

from db_lifecycle import AutoClosingConnection, SCHEMA_VERSION
from reset_data import RESET_LOCK

MANUAL_NAME = re.compile(r"rankup_backup_\d{8}T\d{6}Z_[a-f0-9]{10}\.zip")
RESET_NAME = re.compile(r"rankup_prima_azzeramento_\d{8}T\d{6}Z_[a-f0-9]{10}\.zip")
IMPORT_NAME = re.compile(r"rankup_importato_\d{8}T\d{6}Z_[a-f0-9]{10}\.zip")
NAME = re.compile(r"(?:rankup_backup|rankup_prima_azzeramento|rankup_importato)_\d{8}T\d{6}Z_[a-f0-9]{10}\.zip")
MANIFEST = "manifest.json"
FORMAT = "RankUP-backup-v2"
RESET_FORMAT = "RankUP-reset-backup-v1"
PHRASE = "RIPRISTINA DATI RANKUP"
MAX_UPLOAD = 2 * 1024 * 1024 * 1024
MAX_FILES = 100000
MAX_EXPANDED = 20 * 1024 * 1024 * 1024
SECURITY_TABLES = (
    "rankup_admin_auth", "rankup_politiche_sicurezza",
    "rankup_utenti", "rankup_utenti_eventi",
)


def _folder(folder: Path) -> Path:
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    return folder


def _filename(prefix: str) -> str:
    return prefix + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + secrets.token_hex(5) + ".zip"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_backup(folder: Path, filename: str) -> Path:
    if not NAME.fullmatch(filename):
        raise ValueError("Nome del backup non valido")
    path = folder / filename
    if not path.is_file() or path.is_symlink() or path.resolve().parent != folder.resolve():
        raise ValueError("Backup non trovato")
    return path


def _snapshot(src: Path, dst: Path):
    if not src.is_file():
        raise ValueError("Database di RankUP non disponibile")
    with sqlite3.connect(src, timeout=20, factory=AutoClosingConnection) as source:
        with sqlite3.connect(dst, factory=AutoClosingConnection) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Snapshot SQLite non integro")
    dst.chmod(0o600)


def _schema_version(db_path: Path) -> int:
    with sqlite3.connect(db_path, factory=AutoClosingConnection) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def _add_tree(z: ZipFile, root: Path, prefix: str):
    z.writestr(prefix + "/", b"")
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Collegamento o percorso non ammesso nei documenti")
        if path.is_file():
            z.write(path, prefix + "/" + path.relative_to(root).as_posix())


def create_backup(base: Path, *, prefix: str = "rankup_backup_") -> str:
    """Copia non distruttiva. Condivide il mutex con l'azzeramento."""
    if not RESET_LOCK.acquire(blocking=False):
        raise ValueError("Backup o azzeramento già in corso")
    folder = _folder(base / "backups_rankup")
    filename = _filename(prefix)
    temp_zip = folder / (filename + ".tmp")
    dest = folder / filename
    try:
        with tempfile.TemporaryDirectory(prefix="rankup_snap_", dir=folder) as tmp:
            snapshot = Path(tmp) / "rankup.sqlite3"
            _snapshot(base / "rankup.sqlite3", snapshot)
            audit = Path(tmp) / "rankup_audit.sqlite3"
            if (base / "rankup_audit.sqlite3").is_file():
                _snapshot(base / "rankup_audit.sqlite3", audit)
            with ZipFile(temp_zip, "w", ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
                z.write(snapshot, "rankup.sqlite3")
                if audit.exists():
                    z.write(audit, "rankup_audit.sqlite3")
                for prefix_dir in ("sources", "pending", "archivi_bandi"):
                    _add_tree(z, base / prefix_dir, prefix_dir)
                z.writestr(MANIFEST, json.dumps({
                    "formato": FORMAT,
                    "creato_il": datetime.now(timezone.utc).isoformat(),
                    "database_sha256": _sha(snapshot),
                    "audit_sha256": _sha(audit) if audit.exists() else None,
                    "schema_versione": _schema_version(snapshot),
                    "include": "Database RankUP, audit, PICA/PDF, file temporanei e ZIP archivio finale",
                    "esclusi": "Opzioni Home Assistant, chiavi AI e cartella backup",
                    "ripristino": "I dati amministrativi di accesso e l'audit corrente sono preservati",
                }, ensure_ascii=False, indent=2))
        with ZipFile(temp_zip) as z:
            if z.testzip() is not None:
                raise ValueError("Archivio ZIP non integro")
        temp_zip.chmod(0o600)
        os.replace(temp_zip, dest)
        return filename
    finally:
        temp_zip.unlink(missing_ok=True)
        RESET_LOCK.release()


def _entries(z: ZipFile):
    names = set()
    total = 0
    entries = z.infolist()
    if len(entries) > MAX_FILES:
        raise ValueError("Numero di elementi nello ZIP eccessivo")
    for item in entries:
        key = item.filename
        if key in names:
            raise ValueError("Nomi duplicati nel backup")
        names.add(key)
        name = PurePosixPath(key.rstrip("/"))
        if (not key or key.startswith("/") or "\\" in key
                or any(p in ("", ".", "..") for p in key.rstrip("/").split("/"))):
            raise ValueError("Percorso non consentito nello ZIP")
        root = name.parts[0]
        if root in ("sources", "pending", "archivi_bandi") and len(name.parts) == 1 and not item.is_dir():
            raise ValueError("La cartella dei documenti non può essere un file")
        if not ((root in ("rankup.sqlite3", "rankup_audit.sqlite3", MANIFEST) and len(name.parts) == 1)
                or (root in ("sources", "pending", "archivi_bandi") and len(name.parts) >= 1)):
            raise ValueError("Contenuto estraneo al formato backup")
        if (item.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError("Link simbolico nel backup")
        total += item.file_size
        if total > MAX_EXPANDED:
            raise ValueError("Backup troppo grande per la verifica")
        if item.file_size > 10 * 1024 * 1024 and item.compress_size == 0:
            raise ValueError("Rapporto di compressione anomalo")
        if item.file_size > 10 * 1024 * 1024 and item.file_size > 5000 * max(1, item.compress_size):
            raise ValueError("Compressione anomala nel backup")
    if not {"rankup.sqlite3", MANIFEST}.issubset(names):
        raise ValueError("Manifesto o database mancanti")
    return entries


def verify_backup(path: Path) -> dict:
    """Non modifica il database corrente, rilegge CRC, SHA e SQLite."""
    with ZipFile(path) as z:
        members = _entries(z)
        manifest = json.loads(z.read(MANIFEST))
        if manifest.get("formato") not in (FORMAT, RESET_FORMAT):
            raise ValueError("Formato RankUP non riconosciuto")
        if z.testzip() is not None:
            raise ValueError("Archivio ZIP danneggiato")
        with tempfile.TemporaryDirectory(prefix="rankup_verifica_") as tmp:
            db = Path(tmp) / "test.sqlite3"
            with z.open("rankup.sqlite3") as source, db.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            if _sha(db) != manifest.get("database_sha256"):
                raise ValueError("Impronta del database non corrispondente")
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True,
                                 factory=AutoClosingConnection) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Database SQLite non integro")
                schema = connection.execute("PRAGMA user_version").fetchone()[0]
                if schema > SCHEMA_VERSION:
                    raise ValueError("Backup creato con uno schema più recente del software")
                if not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='bandi' AND type='table'"
                ).fetchone():
                    raise ValueError("Database non riconosciuto come RankUP")
            if "rankup_audit.sqlite3" in z.namelist() and manifest.get("audit_sha256"):
                audit = Path(tmp) / "audit.sqlite3"
                with z.open("rankup_audit.sqlite3") as source, audit.open("wb") as dest:
                    shutil.copyfileobj(source, dest)
                if _sha(audit) != manifest["audit_sha256"]:
                    raise ValueError("Impronta del registro sicurezza non corretta")
                with sqlite3.connect(f"file:{audit}?mode=ro", uri=True,
                                     factory=AutoClosingConnection) as c:
                    if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Registro sicurezza danneggiato")
        return {"valido": True, "formato": manifest["formato"],
                "creato_il": manifest.get("creato_il"),
                "schema_versione": schema,
                "elementi": len(members),
                "dimensione_byte": path.stat().st_size,
                "archivi_finali_inclusi": "archivi_bandi/" in z.namelist()}


def list_backups(folder: Path) -> list[dict]:
    if not folder.is_dir():
        return []
    out = []
    for p in folder.iterdir():
        if not p.is_file() or p.is_symlink() or not NAME.fullmatch(p.name):
            continue
        kind = "Prima dell'azzeramento" if RESET_NAME.fullmatch(p.name) else (
            "Importato" if IMPORT_NAME.fullmatch(p.name) else "Manuale")
        out.append({"nome": p.name, "tipo": kind, "dimensione_byte": p.stat().st_size,
                    "creato_il": datetime.fromtimestamp(p.stat().st_mtime,timezone.utc).isoformat()})
    return sorted(out, key=lambda x: x["creato_il"], reverse=True)


def import_backup(base: Path, source) -> str:
    """Copia un upload già limitato di dimensione, poi lo valida."""
    folder = _folder(base / "backups_rankup")
    name = _filename("rankup_importato_")
    temp = folder / (name + ".tmp")
    try:
        with temp.open("xb") as f:
            size = 0
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise ValueError("ZIP superiore al limite di 2 GB")
                f.write(chunk)
        try:
            verify_backup(temp)
        except BadZipFile as exc:
            raise ValueError("Il file caricato non è un archivio ZIP valido") from exc
        temp.chmod(0o600)
        os.replace(temp, folder / name)
        return name
    finally:
        temp.unlink(missing_ok=True)


def _marker_path(base: Path) -> Path:
    return base / "backups_rankup" / "ripristino_programmato.json"


def pending_restore(base: Path) -> dict | None:
    path = _marker_path(base)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def queue_restore(base: Path, filename: str) -> dict:
    folder = _folder(base / "backups_rankup")
    if _marker_path(base).exists():
        raise ValueError("Esiste già un ripristino in attesa di riavvio")
    selected = resolve_backup(folder, filename)
    details = verify_backup(selected)
    # Salva i dati correnti PRIMA di programmare qualsiasi sostituzione.
    rescue = create_backup(base, prefix="rankup_backup_")
    marker = {"file": filename, "sha256": _sha(selected),
              "backup_pre_ripristino": rescue,
              "backup_pre_sha256": _sha(resolve_backup(folder, rescue)),
              "stato": "pronto",
              "richiesto_il": datetime.now(timezone.utc).isoformat()}
    _write_marker(base, marker)
    return {"programmato": True, "backup_pre_ripristino": rescue,
            "richiede_riavvio": True, "verifica": details}


def _write_marker(base: Path, payload: dict):
    marker = _marker_path(base)
    staging = marker.with_suffix(".tmp")
    try:
        staging.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.chmod(0o600)
        os.replace(staging, marker)
    finally:
        staging.unlink(missing_ok=True)


def _copy_security(current: Path, target: Path):
    """Mantiene gli account e le credenziali presenti prima del ripristino."""
    with sqlite3.connect(target, factory=AutoClosingConnection) as db:
        db.execute("ATTACH DATABASE ? AS old", (str(current),))
        try:
            db.execute("BEGIN IMMEDIATE")
            for table in SECURITY_TABLES:
                ident = '"' + table + '"'
                cols_new = [r[1] for r in db.execute("PRAGMA main.table_info(" + ident + ")")]
                cols_old = [r[1] for r in db.execute("PRAGMA old.table_info(" + ident + ")")]
                if not cols_new or cols_new != cols_old:
                    raise ValueError("Schema delle credenziali non compatibile: ripristino sospeso")
                db.execute("DELETE FROM main." + ident)
                db.execute("INSERT INTO main." + ident + " SELECT * FROM old." + ident)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("DETACH DATABASE old")


def _apply(base: Path, archive: Path, *, preserve_security: bool = True):
    """Chiamare SOLO a processo fermo, prima di initialize()."""
    with tempfile.TemporaryDirectory(prefix="rankup_restore_",dir=_folder(base/"backups_rankup")) as tmp:
        stage = Path(tmp)/"stage"
        stage.mkdir()
        with ZipFile(archive) as z:
            members = _entries(z)
            for member in members:
                # Non estrarre mai le credenziali/audit dal vecchio registro.
                name = member.filename
                if name in (MANIFEST,"rankup_audit.sqlite3"):
                    continue
                dest = stage.joinpath(*PurePosixPath(name).parts)
                if member.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    with z.open(member) as src, dest.open("wb") as dst:
                        shutil.copyfileobj(src,dst)
        if preserve_security:
            _copy_security(base/"rankup.sqlite3",stage/"rankup.sqlite3")
        # Move originals into an on-disk undo directory. In case of an error,
        # reverse all already swapped items before raising.
        undo = Path(tmp)/"undo"
        undo.mkdir()
        moved = []
        try:
            for key in ("rankup.sqlite3","sources","pending","archivi_bandi"):
                new = stage/key
                if not new.exists():
                    continue  # Historic reset backups did not include archive ZIPs.
                old = base/key
                had_old = old.exists()
                if had_old:
                    os.replace(old, undo/key)
                moved.append((key,had_old))
                os.replace(new,old)
            for suffix in ("-wal","-shm"):
                (base/("rankup.sqlite3"+suffix)).unlink(missing_ok=True)
        except Exception:
            for key,had_old in reversed(moved):
                target=base/key
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
                if had_old:
                    os.replace(undo/key,target)
            raise


def apply_pending_restore(base: Path) -> dict | None:
    """Recupera gli stati interrotti: preferisce i dati pre-ripristino."""
    marker = pending_restore(base)
    if marker is None:
        return None
    folder = base/"backups_rankup"
    was_applying = marker.get("stato")=="in_corso"
    chosen_name = marker["backup_pre_ripristino"] if was_applying else marker["file"]
    expected = marker["backup_pre_sha256"] if was_applying else marker["sha256"]
    archive = resolve_backup(folder,chosen_name)
    if _sha(archive)!=expected:
        raise RuntimeError("Backup programmato alterato: avvio bloccato per sicurezza")
    verify_backup(archive)
    if not was_applying:
        marker["stato"]="in_corso"
        _write_marker(base,marker)
    try:
        _apply(base,archive,preserve_security=not was_applying)
    except Exception:
        if not was_applying:
            rescue=resolve_backup(folder,marker["backup_pre_ripristino"])
            if _sha(rescue)!=marker["backup_pre_sha256"]:
                raise RuntimeError("Ripristino fallito e copia di sicurezza non verificabile")
            verify_backup(rescue)
            _apply(base,rescue,preserve_security=False)
        _marker_path(base).unlink(missing_ok=True)
        raise
    _marker_path(base).unlink(missing_ok=True)
    return {"ripristinato":not was_applying, "ripristino_annullato":was_applying,
            "origine":chosen_name}
