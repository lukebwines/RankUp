"""Regressioni SQLite, migrazione v1 e watchdog. Solo dati sintetici."""
import importlib
import json
import sqlite3

import pytest

from db_lifecycle import (
    AutoClosingConnection, SCHEMA_VERSION, current_schema_version,
    prepare_schema_upgrade, record_schema_version,
)


def test_connection_closes_after_commit(tmp_path):
    path = tmp_path / "db.sqlite3"
    handle = sqlite3.connect(path, factory=AutoClosingConnection)
    with handle as db:
        db.execute("CREATE TABLE test (value TEXT)")
        db.execute("INSERT INTO test VALUES ('conservato')")
    with pytest.raises(sqlite3.ProgrammingError):
        handle.execute("SELECT 1")
    with sqlite3.connect(path) as verify:
        assert verify.execute("SELECT value FROM test").fetchone()[0] == "conservato"


def test_connection_closes_and_rolls_back_on_error(tmp_path):
    path = tmp_path / "db.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE test (value TEXT)")
    handle = sqlite3.connect(path, factory=AutoClosingConnection)
    with pytest.raises(ValueError):
        with handle as db:
            db.execute("INSERT INTO test VALUES ('non salvato')")
            raise ValueError("rollback")
    with pytest.raises(sqlite3.ProgrammingError):
        handle.execute("SELECT 1")
    with sqlite3.connect(path) as verify:
        assert verify.execute("SELECT COUNT(*) FROM test").fetchone()[0] == 0


def test_existing_database_backup_before_version_update(tmp_path):
    path = tmp_path / "rankup.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE storico (dato TEXT)")
        db.execute("INSERT INTO storico VALUES ('immutato')")
    with sqlite3.connect(path, factory=AutoClosingConnection) as db:
        assert current_schema_version(db) == 0
        assert prepare_schema_upgrade(db, path)
        record_schema_version(db, SCHEMA_VERSION)
    backups = list((tmp_path / "schema_backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as previous:
        assert previous.execute("SELECT dato FROM storico").fetchone()[0] == "immutato"
        assert previous.execute("PRAGMA user_version").fetchone()[0] == 0
    with sqlite3.connect(path, factory=AutoClosingConnection) as db:
        assert current_schema_version(db) == SCHEMA_VERSION
        assert not prepare_schema_upgrade(db, path)
    assert len(list((tmp_path / "schema_backups").glob("*.sqlite3"))) == 1


def test_new_database_does_not_create_unnecessary_backup(tmp_path):
    path = tmp_path / "new.sqlite3"
    with sqlite3.connect(path, factory=AutoClosingConnection) as db:
        assert prepare_schema_upgrade(db, path)
        record_schema_version(db, SCHEMA_VERSION)
    assert not (tmp_path / "schema_backups").exists()


def test_database_with_future_version_is_rejected(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path, factory=AutoClosingConnection) as db:
        db.execute("PRAGMA user_version = 99")
    with sqlite3.connect(path, factory=AutoClosingConnection) as db:
        with pytest.raises(RuntimeError, match="più recente"):
            prepare_schema_upgrade(db, path)


def test_server_migration_and_watchdog(tmp_path, monkeypatch):
    monkeypatch.setenv("RANKUP_DATA_DIR", str(tmp_path))
    (tmp_path / "options.json").write_text(json.dumps({
        "admin_username": "amministratore_test",
        "admin_password": "PasswordFittiziaMoltoLunga123!"
    }))
    import server
    server = importlib.reload(server)
    with server.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert server.health()["status"] == "ok"
    assert server.health()["schema_version"] == SCHEMA_VERSION
    server.initialize()
    assert not (tmp_path / "schema_backups").exists()
    raw = server.connect()
    with raw as db:
        db.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")
