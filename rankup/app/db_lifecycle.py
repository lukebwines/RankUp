"""Gestione delle connessioni e prima migrazione versionata del DB RankUP.

PRAGMA user_version identifica la versione strutturale. Per database già
popolati viene generato uno snapshot SQLite integro PRIMA dell'upgrade.
I passaggi iniziali sono idempotenti, per consentire un nuovo tentativo se un
aggiornamento viene interrotto prima della registrazione della versione.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import secrets
import sqlite3


SCHEMA_VERSION = 11


class AutoClosingConnection(sqlite3.Connection):
    """Preserva commit/rollback del context manager e poi chiude la connessione."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def current_schema_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def prepare_schema_upgrade(db: sqlite3.Connection, db_path: Path,
                           target_version: int = SCHEMA_VERSION) -> bool:
    """Controlla la versione e fa un backup prima della prima migrazione.

    True: eseguire le migrazioni fino a target_version.
    False: database già aggiornato; non alterarlo.
    """
    current = current_schema_version(db)
    if current > target_version:
        raise RuntimeError(
            f"Database RankUP v{current} più recente del software v{target_version}: "
            "aggiornamento bloccato per evitare modifiche incompatibili."
        )
    if current == target_version:
        return False

    # Su un database appena creato non ci sono dati da salvare.
    has_tables = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    if has_tables:
        folder = db_path.parent / "schema_backups"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{db_path.stem}_pre_schema_v{current}_{stamp}_{secrets.token_hex(4)}.sqlite3"
        dest = folder / filename
        dest.touch(mode=0o600, exist_ok=False)
        try:
            with sqlite3.connect(dest, factory=AutoClosingConnection) as backup:
                db.backup(backup)
                result = backup.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError("Backup SQLite non integro: migrazione annullata")
            dest.chmod(0o600)
        except Exception:
            dest.unlink(missing_ok=True)
            raise
    return True


def record_schema_version(db: sqlite3.Connection, version: int) -> None:
    """Da chiamare soltanto dopo l'esito positivo della migrazione vN."""
    if version < 1 or version > SCHEMA_VERSION:
        raise ValueError("Versione della migrazione non valida")
    db.execute(f"PRAGMA user_version = {version}")
