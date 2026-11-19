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
        db.execute("PRAGMA user_version = 17")
    with sqlite3.connect(path, factory=AutoClosingConnection) as db:
        assert current_schema_version(db) == 17
        assert prepare_schema_upgrade(db, path)
        record_schema_version(db, SCHEMA_VERSION)
    backups = list((tmp_path / "schema_backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as previous:
        assert previous.execute("SELECT dato FROM storico").fetchone()[0] == "immutato"
        assert previous.execute("PRAGMA user_version").fetchone()[0] == 17
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

    # Simula un'installazione reale rimasta allo schema v17: la v18 deve
    # creare la colonna e la tabella economica senza perdere la configurazione.
    with server.connect() as db:
        db.execute("""INSERT INTO co_veneto_config
            (id,dati_json,aggiornato_il,autore) VALUES(1,'{}','2026-11-19','admin_test')""")
        db.execute("DROP TABLE co_veneto_config_eventi")
        db.execute("ALTER TABLE co_veneto_config DROP COLUMN parametri_economici_confermati")
        db.execute("PRAGMA user_version = 17")
        db.commit()

    server.initialize()
    with server.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(co_veneto_config)")}
        assert "parametri_economici_confermati" in columns
        assert db.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='co_veneto_config_eventi'""").fetchone()
        assert db.execute("SELECT autore FROM co_veneto_config WHERE id=1").fetchone()["autore"]=="admin_test"
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    backups = list((tmp_path / "schema_backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as previous:
        assert previous.execute("PRAGMA user_version").fetchone()[0] == 17

    raw = server.connect()
    with raw as db:
        db.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_v18_to_v19_keeps_config_and_requires_new_english_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("RANKUP_DATA_DIR", str(tmp_path))
    (tmp_path / "options.json").write_text(json.dumps({
        "admin_username": "amministratore_test",
        "admin_password": "PasswordFittiziaMoltoLunga123!"
    }))
    import server
    server = importlib.reload(server)
    with server.connect() as db:
        db.execute("""INSERT INTO co_veneto_config
            (id,dati_json,aggiornato_il,autore,parametri_economici_confermati)
            VALUES(1,?,?,?,1)""",
            (json.dumps({"ore_ordinario":"10","ore_professionalizzante":"12",
                         "tariffa_verona":"50","denominazione":"ENTE SINTETICO"}),
             "2026-11-18T10:00:00+01:00","admin_test"))
        db.execute("ALTER TABLE incarichi DROP COLUMN cfu")
        db.execute("PRAGMA user_version = 18")
        db.commit()
    server.initialize()
    with server.connect() as db:
        assert "cfu" in {r["name"] for r in db.execute("PRAGMA table_info(incarichi)")}
        cfg = db.execute("""SELECT dati_json,parametri_economici_confermati,autore
            FROM co_veneto_config WHERE id=1""").fetchone()
        saved = json.loads(cfg["dati_json"])
        assert saved["ore_inglese"] == "15"
        assert saved["denominazione"] == "ENTE SINTETICO"
        assert cfg["parametri_economici_confermati"] == 0
        assert cfg["autore"] == "admin_test"
        assert db.execute("SELECT COUNT(*) FROM co_veneto_config_eventi").fetchone()[0] == 1
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    backups = list((tmp_path / "schema_backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as previous:
        assert previous.execute("PRAGMA user_version").fetchone()[0] == 18
        assert previous.execute("SELECT parametri_economici_confermati FROM co_veneto_config WHERE id=1").fetchone()[0] == 1


def test_v19_to_v20_adds_pending_proposals_without_losing_data(tmp_path,monkeypatch):
    """Aggiornamento reale: nessuna perdita del bando o delle candidature."""
    from test_pdf_import import _setup
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        with server.connect() as db:
            previous=db.execute("SELECT COUNT(*) FROM candidature").fetchone()[0]
            assert previous>0
            db.execute("DROP TABLE valutazioni_pdf_proposte")
            db.execute("PRAGMA user_version = 19")
            db.commit()
        server.initialize()
        with server.connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0]==SCHEMA_VERSION
            assert db.execute("SELECT COUNT(*) FROM candidature").fetchone()[0]==previous
            assert db.execute("""SELECT 1 FROM sqlite_master
                WHERE name='valutazioni_pdf_proposte' AND type='table'""").fetchone()
        backups=list((tmp_path/"schema_backups").glob("*.sqlite3"))
        assert len(backups)==1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0]==19
            assert backup.execute("SELECT COUNT(*) FROM candidature").fetchone()[0]==previous
