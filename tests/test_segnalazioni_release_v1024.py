"""Coerenza metadati del rilascio e presenza modulo Segnalazioni."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "rankup" / "config.yaml"
INDEX = ROOT / "rankup" / "app" / "static" / "index.html"
SERVER = ROOT / "rankup" / "app" / "server.py"
GROQ = ROOT / "rankup" / "app" / "ai_groq.py"
CSS = ROOT / "rankup" / "app" / "static" / "workspace.css"
LIFECYCLE = ROOT / "rankup" / "app" / "db_lifecycle.py"


def test_version_and_css_cache_are_synchronized():
    config = CONFIG.read_text(encoding="utf-8")
    version = re.search(r'^version:\s*"([^"]+)"', config, re.M)
    assert version
    v = version.group(1)
    assert tuple(map(int,v.split("."))) >= (1,0,24)
    html = INDEX.read_text(encoding="utf-8")
    server = SERVER.read_text(encoding="utf-8")
    groq = GROQ.read_text(encoding="utf-8")
    assert f'workspace.css?v={v}' in html
    assert f'version="{v}"' in server
    assert f'"version": "{v}"' in server
    assert f'RankUP/{v}' in groq


def test_segnalazioni_ui_and_migration_are_in_release():
    html = INDEX.read_text(encoding="utf-8")
    server = SERVER.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    migration = LIFECYCLE.read_text(encoding="utf-8")
    assert 'data-page="segnalazioni"' in html
    assert 'async function renderSegnalazioni()' in html
    assert 'class="ru-status-pill ru-ticket-status--' in html
    assert 'function apriSegnalazioni(ambito)' in html
    assert '.ru-workspace .ru-ticket-form{' in css
    current_version = int(re.search(r"SCHEMA_VERSION = ([0-9]+)", migration).group(1))
    assert current_version >= 11
    for action in ('@app.get("/api/bandi/{code}/segnalazioni")',
                   '@app.post("/api/bandi/{code}/segnalazioni", status_code=201)',
                   '@app.put("/api/bandi/{code}/segnalazioni/{segnalazione_id}")',
                   '@app.post("/api/bandi/{code}/segnalazioni/{segnalazione_id}/risposte", status_code=201)'):
        assert action in server
,migration,re.M)
    assert match and int(match.group(1)) >= 11
    for action in ('@app.get("/api/bandi/{code}/segnalazioni")',
                   '@app.post("/api/bandi/{code}/segnalazioni", status_code=201)',
                   '@app.put("/api/bandi/{code}/segnalazioni/{segnalazione_id}")',
                   '@app.post("/api/bandi/{code}/segnalazioni/{segnalazione_id}/risposte", status_code=201)'):
        assert action in server
