"""Regressioni backup e ripristino RankUP: esclusivamente dati sintetici."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path
from zipfile import ZipFile

import pytest

from backup_manager import (
    PHRASE, create_backup, import_backup, list_backups,
    verify_backup, queue_restore, pending_restore, apply_pending_restore,
    resolve_backup,
)


def _db(path: Path, *, bando: str, password: str):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE bandi (id INTEGER PRIMARY KEY, codice TEXT NOT NULL);
            CREATE TABLE rankup_admin_auth (id INTEGER PRIMARY KEY, verifier TEXT);
            CREATE TABLE rankup_politiche_sicurezza (id INTEGER PRIMARY KEY, policy TEXT);
            CREATE TABLE rankup_utenti (id INTEGER PRIMARY KEY, password TEXT);
            CREATE TABLE rankup_utenti_eventi (id INTEGER PRIMARY KEY, action TEXT);
            PRAGMA user_version=16;
        """)
        conn.execute("INSERT INTO bandi VALUES(1,?)", (bando,))
        conn.execute("INSERT INTO rankup_admin_auth VALUES(1,?)", (password,))
        conn.execute("INSERT INTO rankup_politiche_sicurezza VALUES(1,'policy')")
        conn.execute("INSERT INTO rankup_utenti VALUES(1,?)", (password,))
        conn.execute("INSERT INTO rankup_utenti_eventi VALUES(1,'created')")


def _get(path: Path, query: str):
    with sqlite3.connect(path) as conn:
        return conn.execute(query).fetchone()[0]


def test_backup_create_verify_import_and_reject_traversal(tmp_path):
    base = tmp_path / "rankup"
    base.mkdir()
    _db(base / "rankup.sqlite3", bando="BANDO-A", password="HASH-A")
    (base / "sources").mkdir()
    (base / "sources" / "file.pdf").write_bytes(b"pdf sintetico")
    (base / "archivi_bandi").mkdir()
    (base / "archivi_bandi" / "archivio.zip").write_bytes(b"zip sintetico")
    name = create_backup(base)
    saved = resolve_backup(base / "backups_rankup", name)
    assert verify_backup(saved)["valido"] is True
    assert verify_backup(saved)["archivi_finali_inclusi"] is True
    with ZipFile(saved) as z:
        assert z.read("sources/file.pdf") == b"pdf sintetico"
        assert z.read("archivi_bandi/archivio.zip") == b"zip sintetico"
    assert list_backups(base / "backups_rankup")[0]["tipo"] == "Manuale"
    other = import_backup(base, io.BytesIO(saved.read_bytes()))
    assert verify_backup(resolve_backup(base / "backups_rankup", other))["valido"]
    with pytest.raises(ValueError, match="Nome"):
        resolve_backup(base / "backups_rankup", "../../secrets.zip")
    with pytest.raises(ValueError):
        import_backup(base, io.BytesIO(b"not a zip"))
    malicious = io.BytesIO()
    with ZipFile(malicious, "w") as z:
        with ZipFile(saved) as original:
            z.writestr("rankup.sqlite3", original.read("rankup.sqlite3"))
            z.writestr("manifest.json", original.read("manifest.json"))
        z.writestr("../danger", "malicious")
    with pytest.raises(ValueError, match="Percorso"):
        import_backup(base, io.BytesIO(malicious.getvalue()))
    assert not (tmp_path / "danger").exists()


def test_restart_restore_preserves_live_credentials_and_audit(tmp_path):
    base = tmp_path / "rankup"
    base.mkdir()
    _db(base / "rankup.sqlite3", bando="VECCHIO", password="HASH-OLD")
    (base / "sources").mkdir()
    (base / "sources" / "old.pdf").write_text("old")
    (base / "archivi_bandi").mkdir()
    (base / "archivi_bandi" / "old.zip").write_bytes(b"old archive")
    with sqlite3.connect(base / "rankup_audit.sqlite3") as db:
        db.execute("CREATE TABLE security_event (id integer)")
        db.execute("INSERT INTO security_event VALUES(1)")
    first = create_backup(base)
    with sqlite3.connect(base / "rankup.sqlite3") as conn:
        conn.execute("UPDATE bandi SET codice='NUOVO'")
        conn.execute("UPDATE rankup_admin_auth SET verifier='HASH-NEW'")
        conn.execute("UPDATE rankup_utenti SET password='HASH-NEW'")
    (base / "sources" / "old.pdf").unlink()
    (base / "sources" / "new.pdf").write_text("new")
    (base / "archivi_bandi" / "old.zip").unlink()
    (base / "archivi_bandi" / "new.zip").write_bytes(b"new archive")
    with sqlite3.connect(base / "rankup_audit.sqlite3") as conn:
        conn.execute("INSERT INTO security_event VALUES(2)")
    plan = queue_restore(base,first)
    assert plan["richiede_riavvio"] is True
    assert pending_restore(base)["file"] == first
    assert _get(base / "rankup.sqlite3", "SELECT codice FROM bandi") == "NUOVO"
    applied = apply_pending_restore(base)
    assert applied["ripristinato"] is True
    assert pending_restore(base) is None
    assert _get(base / "rankup.sqlite3", "SELECT codice FROM bandi") == "VECCHIO"
    assert _get(base / "rankup.sqlite3", "SELECT verifier FROM rankup_admin_auth") == "HASH-NEW"
    assert _get(base / "rankup.sqlite3", "SELECT password FROM rankup_utenti") == "HASH-NEW"
    assert _get(base / "rankup_audit.sqlite3", "SELECT count(*) FROM security_event") == 2
    assert (base / "sources" / "old.pdf").exists()
    assert not (base / "sources" / "new.pdf").exists()
    assert (base / "archivi_bandi" / "old.zip").exists()
    assert not (base / "archivi_bandi" / "new.zip").exists()
    rescue = resolve_backup(base / "backups_rankup", plan["backup_pre_ripristino"])
    assert verify_backup(rescue)["valido"]


def test_backup_ui_and_routes_are_admin_only():
    root = Path(__file__).resolve().parents[1]
    html = (root / "rankup/app/static/index.html").read_text(encoding="utf-8")
    server = (root / "rankup/app/server.py").read_text(encoding="utf-8")
    assert "Backup e azzeramento archivi" in html
    assert "backupManagementMarkup" in html
    assert 'id="backup-create"' in html
    assert 'id="backup-restore-form"' in html
    assert 'id="reset-data-form"' in html
    for token in ("/api/admin/backups", "/api/admin/backups/import",
                  "/api/admin/backups/{filename}/verify",
                  "/api/admin/backups/{filename}/download",
                  "/api/admin/backups/{filename}/restore"):
        assert token in server
    for function in ("elenco_backup_manual", "crea_backup_manual",
                     "importa_backup_manual", "verifica_backup_manual",
                     "scarica_backup_manual", "programma_ripristino_backup"):
        code = server.split("def " + function + "(", 1)[1].split("\n\n", 1)[0]
        assert "require_admin(request)" in code
    assert "AUTH.verify_admin_password(body.password_admin)" in server
    assert "RESTORE_AT_BOOT = apply_pending_restore(BASE)" in server
