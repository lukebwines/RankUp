"""RankUP 1.0.27 — Centro Commissioni e importazione PICA.

Dati sintetici, senza nomine o candidature reali.
"""
from pathlib import Path

from fastapi.testclient import TestClient
from test_pdf_import import _setup, _post
from test_import import commissioni_fixture

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-RankUp-Request": "1"}


def _commission_snapshot(server):
    with server.connect() as db:
        return [tuple(row) for row in db.execute("""
            SELECT id,bando_id,cds,sede,presidente,componenti,requisiti,anno,updated_at
            FROM commissioni ORDER BY id
        """).fetchall()]


def test_catalog_suggestions_are_available_to_authenticated_operators_without_auto_association(tmp_path, monkeypatch):
    server, client, _ = _setup(tmp_path, monkeypatch)
    with client:
        original = _commission_snapshot(server)
        response = client.get("/api/bandi/26-02/commissioni/suggerimenti")
        assert response.status_code == 200, response.text
        groups = response.json()["suggerimenti"]
        assert groups
        assert any(g["candidati"] for g in groups)
        assert all("anno_compatibile" in proposal
                   for g in groups for proposal in g["candidati"])
        assert _commission_snapshot(server) == original

        created = _post(client, "/api/utenti", json={
            "username": "test-operatore", "password": "PasswordDiTest123!"
        })
        assert created.status_code == 200, created.text
        with TestClient(server.app) as user:
            assert _post(user, "/api/auth/login", json={
                "username": "test-operatore", "password": "PasswordDiTest123!"
            }).status_code == 200
            available = user.get("/api/bandi/26-02/commissioni/suggerimenti")
            assert available.status_code == 200, available.text
            registry = user.get("/api/commissioni/anagrafica")
            assert registry.status_code == 200, registry.text
            assert registry.json()["commissioni"]
            # L'API storica /admin resta chiusa agli operatori.
            assert user.get("/api/admin/commissioni-globali").status_code == 403
            # La lettura non deve modificare alcuna associazione.
            assert _commission_snapshot(server) == original
        assert client.get("/api/bandi/CODICE-NON-ESISTENTE/commissioni/suggerimenti").status_code == 404


def test_excel_preview_keeps_commissions_and_catalog_unchanged(tmp_path, monkeypatch):
    server, client, _ = _setup(tmp_path, monkeypatch)
    with client:
        before = _commission_snapshot(server)
        with server.connect() as db:
            global_before = db.execute("SELECT COUNT(*) FROM commissioni_catalogo").fetchone()[0]
        filedata = commissioni_fixture()
        for mode in ("solo_nuove", "aggiorna"):
            response = _post(client,
                "/api/bandi/26-02/commissioni/import/anteprima",
                files={"file": ("commissioni.xlsx", filedata,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"mode": mode})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["aggiunte"] >= 0
            assert result["aggiornate"] >= 0
            assert result["inalterate"] >= 0
            assert result["aggiunte"] + result["aggiornate"] + result["inalterate"] == len(result["anteprima"])
            assert _commission_snapshot(server) == before
            with server.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM commissioni_catalogo").fetchone()[0] == global_before


def test_unified_ui_pica_warning_and_version():
    html = (ROOT / "rankup/app/static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "rankup/app/static/workspace.css").read_text(encoding="utf-8")
    server = (ROOT / "rankup/app/server.py").read_text(encoding="utf-8")
    config = (ROOT / "rankup/config.yaml").read_text(encoding="utf-8")
    import re
    match=re.search(r'^version:\s*"(\d+)\.(\d+)\.(\d+)"',config,re.M)
    assert match and tuple(map(int,match.groups())) >= (1,0,27)
    version=".".join(match.groups())
    assert f'version="{version}"' in server
    assert f'workspace.css?v={version}' in html
    assert f'RANKUP · v{version}' in html
    assert 'Centro gestione Commissioni' in html
    assert 'id="cm-unified-rows"' in html
    assert 'id="cm-manage-panel"' in html
    assert 'id="cm-catalog-verified"' in html
    assert '@app.get("/api/bandi/{code}/commissioni/suggerimenti")' in server
    assert '@app.post("/api/bandi/{code}/commissioni/import/anteprima")' in server
    assert "nessuna associazione automatica." in html
    assert 'id="pica-cm-open"' in html
    assert 'id="cm-preview-import"' in html
    assert 'id="cm-import-confirm-check"' in html
    assert '.ru-workspace .ru-cm-layout{' in css
