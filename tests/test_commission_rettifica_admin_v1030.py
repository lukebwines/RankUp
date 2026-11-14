"""Rettifica materiale Admin in anagrafica (dati interamente sintetici)."""
import json
from fastapi.testclient import TestClient
from test_pdf_import import _setup, _post


def test_rettifica_admin_atomic_audit_and_no_propagation(tmp_path, monkeypatch):
    server, admin, _ = _setup(tmp_path, monkeypatch)
    with admin:
        original_bando = admin.get("/api/bandi/26-02/commissioni").json()
        created = _post(admin, "/api/commissioni/anagrafica", json={
            "cds": "CORSO SINTETICO RETTIFICA", "sede": "SEDE TEST",
            "presidente": "Nome Errato", "componenti": "Componente Test",
            "requisiti": "", "anno": ""
        })
        assert created.status_code == 200, created.text
        cid = created.json()["id"]
        rows = admin.get("/api/commissioni/anagrafica").json()["commissioni"]
        before = next(row for row in rows if row["id"] == cid)
        change = {k: before[k] for k in
                  ("cds", "sede", "presidente", "componenti", "requisiti", "anno")}
        change["presidente"] = "Nome Corretto"
        change["expected_impronta"] = before["impronta"]
        result = admin.put(f"/api/commissioni/anagrafica/{cid}/rettifica",
                           json=change, headers={"X-RankUp-Request": "1"})
        assert result.status_code == 200, result.text
        assert result.json()["modificata"] is True
        after = next(row for row in
                     admin.get("/api/commissioni/anagrafica").json()["commissioni"]
                     if row["id"] == cid)
        assert after["presidente"] == "Nome Corretto"
        assert after["id"] == cid
        assert after["impronta"] != before["impronta"]
        assert admin.get("/api/bandi/26-02/commissioni").json() == original_bando
        stale = admin.put(f"/api/commissioni/anagrafica/{cid}/rettifica",
                          json=change, headers={"X-RankUp-Request": "1"})
        assert stale.status_code == 409
        with server.connect() as db:
            audit = db.execute("""SELECT prima_json,dopo_json,autore FROM
                commissioni_catalogo_rettifiche WHERE catalogo_id=?""", (cid,)).fetchall()
            assert len(audit) == 1
            assert json.loads(audit[0]["prima_json"])["presidente"] == "Nome Errato"
            assert json.loads(audit[0]["dopo_json"])["presidente"] == "Nome Corretto"
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_operator_cannot_rettify_even_with_direct_api(tmp_path, monkeypatch):
    server, admin, _ = _setup(tmp_path, monkeypatch)
    with admin:
        created = _post(admin, "/api/commissioni/anagrafica", json={
            "cds": "CORSO FITTIZIO", "sede": "SEDE FITTIZIA",
            "presidente": "Nome Fittizio", "componenti": "",
            "requisiti": "", "anno": ""
        })
        assert created.status_code == 200
        cid = created.json()["id"]
        record = next(r for r in admin.get("/api/commissioni/anagrafica")
                      .json()["commissioni"] if r["id"] == cid)
        _post(admin, "/api/utenti", json={
            "username": "operatore-rettifica",
            "password": "PasswordSintetica123!"
        })
        with TestClient(server.app) as operator:
            logged = _post(operator, "/api/auth/login", json={
                "username": "operatore-rettifica",
                "password": "PasswordSintetica123!"
            })
            assert logged.status_code == 200
            payload = {k: record[k] for k in (
                "cds", "sede", "presidente", "componenti", "requisiti", "anno")}
            payload["presidente"] = "Nome Alterato"
            payload["expected_impronta"] = record["impronta"]
            denied = operator.put(
                f"/api/commissioni/anagrafica/{cid}/rettifica",
                json=payload, headers={"X-RankUp-Request": "1"})
            assert denied.status_code == 403
            assert next(r for r in admin.get("/api/commissioni/anagrafica")
                        .json()["commissioni"] if r["id"] == cid)["presidente"] == "Nome Fittizio"
