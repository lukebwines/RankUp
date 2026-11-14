"""Prova di ripristino isolato della componente SQLite di RankUP.

Legge una copia/backup del DB e ne ricrea una copia temporanea, senza
toccare il database di origine. NON certifica il ripristino dell'intero
Home Assistant: opzioni, originali e configurazione dell'add-on richiedono
test separati del backup Home Assistant.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from urllib.parse import quote


def verify_sqlite_restore(source: Path, sources: Path | None = None,
                          max_size_mb: int = 1024) -> dict:
    source=Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.is_symlink():
        raise ValueError("Il database sorgente non deve essere un link simbolico")
    size=source.stat().st_size
    limit=max_size_mb*1024*1024
    if size>limit:
        raise ValueError("DB oltre il limite del test: utilizzare un supporto separato con spazio adeguato")
    # Il ripristino deve avere spazio libero sufficiente, senza saturare il Pi.
    if shutil.disk_usage(tempfile.gettempdir()).free < size*2 + 100*1024*1024:
        raise ValueError("Spazio insufficiente per una prova di ripristino sicura")
    uri="file:"+quote(str(source),safe="/")+"?mode=ro"
    report={"database_originale_byte":size,"integrita":None,
            "violazioni_fk":None,"file_pica_verificati":0,
            "file_pica_mancanti":0,"file_pica_non_coerenti":0,
            "esito":"da_verificare"}
    with tempfile.TemporaryDirectory(prefix="rankup-restore-test-") as directory:
        destination=Path(directory)/"ripristino_sintetico.sqlite3"
        with sqlite3.connect(uri,uri=True) as src, sqlite3.connect(destination) as dest:
            src.backup(dest)
        with sqlite3.connect(destination) as restored:
            report["integrita"]=restored.execute("PRAGMA integrity_check").fetchone()[0]
            report["violazioni_fk"]=len(restored.execute("PRAGMA foreign_key_check").fetchall())
            if sources is not None:
                table=restored.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='bandi' AND type='table'").fetchone()
                if table:
                    for code,digest in restored.execute(
                        "SELECT codice,source_hash FROM bandi WHERE source_hash<>''"):
                        folder=Path(sources)/code
                        matches=[p for p in (folder/"pica_originale.xls",
                                             folder/"pica_originale.xlsx")
                                 if p.is_file() and not p.is_symlink()]
                        if not matches:
                            report["file_pica_mancanti"]+=1
                            continue
                        report["file_pica_verificati"]+=1
                        if not any(hashlib.sha256(p.read_bytes()).hexdigest()==digest
                                   for p in matches):
                            report["file_pica_non_coerenti"]+=1
    report["esito"]="ok" if (
        report["integrita"]=="ok" and report["violazioni_fk"]==0 and
        report["file_pica_mancanti"]==0 and report["file_pica_non_coerenti"]==0
    ) else "attenzione"
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(description="Prova offline di ripristino SQLite RankUP")
    parser.add_argument("database",type=Path,help="Copia coerente o backup SQLite")
    parser.add_argument("--sources",type=Path,default=None,help="Cartella originali PICA, se disponibile")
    parser.add_argument("--max-size-mb",type=int,default=1024)
    args=parser.parse_args()
    if args.max_size_mb<1:
        parser.error("max-size-mb deve essere positivo")
    print(json.dumps(verify_sqlite_restore(args.database,args.sources,args.max_size_mb),
                     ensure_ascii=False,indent=2))
