"""Verifica integrità e portabilità dei percorsi di uno ZIP dei sorgenti RankUP.

Controlla il contenuto del pacchetto GIT (non l'archivio dei bandi):
CRC, dimensioni, percorsi relativi POSIX, collisioni Windows/macOS, file
essenziali, assenza di database/credenziali e riestrazione in area temporanea.
Non prova che l'add-on possa girare direttamente su Windows o macOS.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata
from zipfile import BadZipFile, ZipFile

REQUIRED_FILES = frozenset({
    "README.md",
    "ROADMAP.md",
    "repository.yaml",
    "rankup/config.yaml",
    "rankup/Dockerfile",
    "rankup/app/requirements.txt",
    "rankup/app/server.py",
    "rankup/app/db_lifecycle.py",
    "rankup/app/static/index.html",
    "rankup/app/title_date_check.py",
    "tests/test_db_lifecycle.py",
})
MAX_ZIP_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 10000
WINDOWS_RESERVED = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE
)
ILLEGAL_WINDOWS = '<>:"\\|?*'
BLOCKED_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}
BLOCKED_FILES = {"options.json", ".env", ".env.local"}
BLOCKED_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".pem", ".key"}


class SourceArchiveError(ValueError):
    """Lo ZIP non soddisfa i requisiti di distribuzione sorgenti."""


def _safe_components(name: str) -> tuple[str, ...]:
    if not name or name.startswith("/") or "\\" in name:
        raise SourceArchiveError(f"Percorso ZIP non portabile: {name!r}")
    parts = name.rstrip("/").split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise SourceArchiveError(f"Percorso ZIP non valido: {name!r}")
    for part in parts:
        if (any(ch in ILLEGAL_WINDOWS or ord(ch) < 32 for ch in part)
                or part.endswith((".", " "))
                or WINDOWS_RESERVED.fullmatch(part)
                or len(part.encode("utf-8")) > 255):
            raise SourceArchiveError(f"Nome non portabile: {name!r}")
    return tuple(parts)


def verify_source_zip(path: str | Path) -> dict[str, object]:
    """Verifica l'archivio completo e prova un'estrazione isolata."""
    path = Path(path)
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise SourceArchiveError("ZIP sorgenti troppo grande")
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_MEMBERS:
                raise SourceArchiveError("Numero di elementi ZIP non valido")
            names = set()
            roots = set()
            actual_files = set()
            files = []
            expanded = 0
            for info in members:
                parts = _safe_components(info.filename)
                roots.add(parts[0])
                portable_name = unicodedata.normalize(
                    "NFC", "/".join(parts).casefold()
                )
                if portable_name in names:
                    raise SourceArchiveError(
                        f"Nome duplicato o incompatibile con filesystem case-insensitive: {info.filename}"
                    )
                names.add(portable_name)
                unix_mode = info.external_attr >> 16
                if (unix_mode & 0o170000) == 0o120000:
                    raise SourceArchiveError("Link simbolico non consentito nei sorgenti")
                if info.is_dir():
                    continue
                if len(parts) < 2:
                    raise SourceArchiveError("File fuori dalla cartella radice")
                subpath = "/".join(parts[1:])
                lowered = {item.casefold() for item in parts[1:]}
                if (BLOCKED_PARTS & lowered
                        or parts[-1].casefold() in BLOCKED_FILES
                        or Path(parts[-1]).suffix.casefold() in BLOCKED_SUFFIXES):
                    raise SourceArchiveError(f"File non adatto ai sorgenti: {subpath}")
                actual_files.add(subpath)
                expanded += info.file_size
                if expanded > MAX_EXPANDED_BYTES:
                    raise SourceArchiveError("ZIP sorgenti espanso troppo grande")
                files.append((info, parts))
            if len(roots) != 1:
                raise SourceArchiveError("Lo ZIP deve contenere una sola cartella radice")
            missing = REQUIRED_FILES - actual_files
            if missing:
                raise SourceArchiveError(
                    "Mancano file essenziali: " + ", ".join(sorted(missing))
                )

            bad = archive.testzip()
            if bad is not None:
                raise SourceArchiveError(f"CRC ZIP non valido: {bad}")

            # Prova di estrazione con percorsi già verificati e dimensioni limitate.
            with tempfile.TemporaryDirectory(prefix="rankup_source_check_") as temp:
                dest = Path(temp)
                for info, parts in files:
                    target = dest.joinpath(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=64 * 1024)
                    if target.stat().st_size != info.file_size:
                        raise SourceArchiveError(
                            f"Dimensione estratta non coerente: {info.filename}"
                        )
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            return {
                "root": next(iter(roots)),
                "files": len(files),
                "expanded_bytes": expanded,
                "sha256": digest,
            }
    except BadZipFile as exc:
        raise SourceArchiveError("Archivio ZIP corrotto o non valido") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Verifica ZIP sorgenti RankUP")
    parser.add_argument("zip_path", type=Path)
    args = parser.parse_args()
    try:
        result = verify_source_zip(args.zip_path)
    except (SourceArchiveError, OSError) as exc:
        parser.exit(1, f"ZIP NON VALIDO: {exc}\n")
    print(
        f"ZIP sorgenti verificato: {result['files']} file, "
        f"{result['expanded_bytes']} byte decompressi, "
        f"radice={result['root']}, SHA-256={result['sha256']}"
    )


if __name__ == "__main__":
    main()
