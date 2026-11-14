"""Diagnostica SQLite RankUP in sola lettura, senza estrarre dati personali.

Esempio: python scripts/audit_database.py /data/rankup.sqlite3
Eseguire preferibilmente su copia coerente creata con SQLite backup.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3


def audit(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    # read-only SQLite: nessun INSERT, UPDATE, DELETE o PRAGMA mutabile.
    from urllib.parse import quote
    db = sqlite3.connect(f"file:{quote(str(path.resolve()), safe='/')}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        report = {
            "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
            "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0],
            "integrity_check": [r[0] for r in db.execute("PRAGMA integrity_check")],
            "foreign_key_violations": [
                {"table": r[0], "rowid": r[1], "parent": r[2], "foreign_key": r[3]}
                for r in db.execute("PRAGMA foreign_key_check")
            ],
            "checks": {}
        }
        def count(label: str, tables_needed: set[str], sql: str) -> None:
            report["checks"][label] = (db.execute(sql).fetchone()[0]
                                       if tables_needed <= tables else None)
        count("candidature_incarico_di_altro_bando",
              {"candidature", "incarichi"}, """
              SELECT COUNT(*) FROM candidature c
              JOIN incarichi i ON i.id=c.incarico_id
              WHERE c.bando_id<>i.bando_id""")
        count("commissioni_duplicate_stesso_bando_cds_sede",
              {"commissioni"}, """
              SELECT COUNT(*) FROM (
                SELECT bando_id,LOWER(TRIM(cds)),LOWER(TRIM(sede))
                FROM commissioni GROUP BY 1,2,3 HAVING COUNT(*)>1
              )""")
        count("commissioni_anagrafica_senza_presidente",
              {"commissioni_catalogo"}, """
              SELECT COUNT(*) FROM commissioni_catalogo
              WHERE TRIM(presidente)=''""")
        count("commissioni_anagrafica_senza_componenti",
              {"commissioni_catalogo"}, """
              SELECT COUNT(*) FROM commissioni_catalogo
              WHERE TRIM(componenti)=''""")
        count("eventi_catalogo_senza_voce",
              {"commissioni_catalogo_eventi","commissioni_catalogo"}, """
              SELECT COUNT(*) FROM commissioni_catalogo_eventi e
              LEFT JOIN commissioni_catalogo c ON c.id=e.catalogo_id
              WHERE c.id IS NULL""")
        count("rettifiche_senza_voce",
              {"commissioni_catalogo_rettifiche","commissioni_catalogo"}, """
              SELECT COUNT(*) FROM commissioni_catalogo_rettifiche e
              LEFT JOIN commissioni_catalogo c ON c.id=e.catalogo_id
              WHERE c.id IS NULL""")
        count("graduatorie_cds_senza_bando",
              {"graduatorie_cds","bandi"}, """
              SELECT COUNT(*) FROM graduatorie_cds g
              LEFT JOIN bandi b ON b.id=g.bando_id WHERE b.id IS NULL""")
        count("segnalazioni_commissione_altro_bando",
              {"segnalazioni","commissioni"}, """
              SELECT COUNT(*) FROM segnalazioni s
              JOIN commissioni c ON c.id=s.commissione_id
              WHERE s.bando_id<>c.bando_id""")
        count("candidature_duplicate_stessa_associazione_pica",
              {"candidature"}, """
              SELECT COUNT(*) FROM (
                SELECT bando_id,incarico_id,id_pica FROM candidature
                GROUP BY 1,2,3 HAVING COUNT(*)>1
              )""")
        return report
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit SQLite RankUP read-only")
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.database), ensure_ascii=False, indent=2))
