"""Test sintetici per l'indicatore ZIP CO Veneto nelle approvazioni."""
import sqlite3

from co_veneto import approval_zip_state


def _database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE co_veneto_lotti (approvazione_id INTEGER, creato_il TEXT);
        CREATE TABLE co_veneto_xml (
            id INTEGER PRIMARY KEY, approvazione_id INTEGER,
            incarico_id INTEGER, creato_il TEXT
        );
        CREATE TABLE co_veneto_pratiche (
            approvazione_id INTEGER, incarico_id INTEGER, aggiornato_il TEXT
        );
        CREATE TABLE co_veneto_config (id INTEGER, aggiornato_il TEXT);
    """)
    return db


def _snapshot():
    return {"selezioni": [{"incarichi": [
        {"id": 10, "vincitore_id": 100},
        {"id": 11, "vincitore_id": 101},
        {"id": 12, "vincitore_id": None},
    ]}]}


def test_zip_status_requires_all_awarded_jobs_and_detects_changes():
    with _database() as db:
        snapshot = _snapshot()
        base = "2026-11-18T12:00:00.000000+01:00"
        later = "2026-11-18T12:00:00.000001+01:00"
        assert approval_zip_state(db, 1, snapshot) == "non_generato"

        db.execute("INSERT INTO co_veneto_lotti VALUES(?,?)", (1, base))
        assert approval_zip_state(db, 1, snapshot) == "parziale"
        db.execute("INSERT INTO co_veneto_xml VALUES(?,?,?,?)", (1, 1, 10, base))
        assert approval_zip_state(db, 1, snapshot) == "parziale"
        db.execute("INSERT INTO co_veneto_xml VALUES(?,?,?,?)", (2, 1, 11, base))
        assert approval_zip_state(db, 1, snapshot) == "generato"

        # Anche modifiche nello stesso secondo sono rilevate con microsecondi.
        db.execute("INSERT INTO co_veneto_pratiche VALUES(?,?,?)", (1, 10, later))
        assert approval_zip_state(db, 1, snapshot) == "da_rigenerare"
        db.execute("INSERT INTO co_veneto_xml VALUES(?,?,?,?)", (3, 1, 10, later))
        assert approval_zip_state(db, 1, snapshot) == "generato"

        # Modificare i dati comuni dell'Ateneo rende obsoleti i file precedenti.
        db.execute("INSERT INTO co_veneto_config VALUES(?,?)", (1, later))
        assert approval_zip_state(db, 1, snapshot) == "da_rigenerare"
        newer = "2026-11-18T12:00:00.000002+01:00"
        db.execute("INSERT INTO co_veneto_xml VALUES(?,?,?,?)", (4, 1, 10, newer))
        db.execute("INSERT INTO co_veneto_xml VALUES(?,?,?,?)", (5, 1, 11, newer))
        assert approval_zip_state(db, 1, snapshot) == "generato"


def test_zip_status_is_scoped_to_approval():
    with _database() as db:
        stamp = "2026-11-18T12:00:00+01:00"
        db.execute("INSERT INTO co_veneto_lotti VALUES(?,?)", (2, stamp))
        db.execute("INSERT INTO co_veneto_xml VALUES(?,?,?,?)", (1, 2, 10, stamp))
        assert approval_zip_state(db, 1, _snapshot()) == "non_generato"
        assert approval_zip_state(db, 2, _snapshot()) == "parziale"
