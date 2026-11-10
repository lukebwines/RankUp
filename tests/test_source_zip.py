"""Test del controllo ZIP sorgenti, con pacchetti interamente sintetici."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo
import sys
import pytest

# Compatibile anche con pytest avviato con PYTHONPATH limitato a rankup/app.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.verify_source_zip import (
    REQUIRED_FILES, SourceArchiveError, verify_source_zip,
)


def _make_zip(path: Path, *, absent=(), extra=()):
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name in sorted(REQUIRED_FILES - set(absent)):
            archive.writestr("RankUp-source/" + name, b"contenuto di prova")
        for name, data in extra:
            archive.writestr(name, data)
    return path


def test_valid_portable_zip(tmp_path):
    result = verify_source_zip(_make_zip(tmp_path / "src.zip"))
    assert result["root"] == "RankUp-source"
    assert result["files"] == len(REQUIRED_FILES)
    assert len(result["sha256"]) == 64


def test_reject_missing_required_file(tmp_path):
    source = _make_zip(tmp_path / "missing.zip", absent={"rankup/config.yaml"})
    with pytest.raises(SourceArchiveError, match="Mancano file"):
        verify_source_zip(source)


@pytest.mark.parametrize("unsafe", [
    "RankUp-source/../outside.txt",
    "RankUp-source/a\\wrong.txt",
    "RankUp-source/CON.txt",
    "RankUp-source/other:bad.txt",
    "/absolute/path.txt",
    "RankUp-source/.env",
    "RankUp-source/secrets.sqlite3",
])
def test_reject_nonportable_or_sensitive_member(tmp_path, unsafe):
    source = _make_zip(tmp_path / "unsafe.zip", extra=[(unsafe, b"test")])
    with pytest.raises(SourceArchiveError):
        verify_source_zip(source)


def test_reject_case_insensitive_collision(tmp_path):
    source = _make_zip(tmp_path / "collision.zip", extra=[
        ("RankUp-source/README.MD", b"altra versione")
    ])
    with pytest.raises(SourceArchiveError, match="duplicato"):
        verify_source_zip(source)


def test_reject_multiple_roots(tmp_path):
    source = _make_zip(tmp_path / "roots.zip", extra=[("AnotherRoot/test.txt", b"x")])
    with pytest.raises(SourceArchiveError, match="una sola cartella"):
        verify_source_zip(source)


def test_reject_symlink(tmp_path):
    source = tmp_path / "symlink.zip"
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        for name in sorted(REQUIRED_FILES):
            archive.writestr("RankUp-source/" + name, b"OK")
        link = ZipInfo("RankUp-source/link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        archive.writestr(link, b"README.md")
    with pytest.raises(SourceArchiveError, match="simbolico"):
        verify_source_zip(source)
