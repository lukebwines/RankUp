"""Restore trial operates only on isolated copies and checks integrity."""
import sqlite3
from pathlib import Path
from scripts.verify_restore import verify_sqlite_restore


def test_isolated_restore_integrity(tmp_path):
    db_path=tmp_path/"source.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, p INTEGER REFERENCES parent(id))")
        db.execute("INSERT INTO parent VALUES (1)")
        db.execute("INSERT INTO child VALUES (1,1)")
    result=verify_sqlite_restore(db_path,max_size_mb=5)
    assert result["esito"]=="ok"
    assert result["integrita"]=="ok"
    assert result["violazioni_fk"]==0
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM child").fetchone()[0]==1
