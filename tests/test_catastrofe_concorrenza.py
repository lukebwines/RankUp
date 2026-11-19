"""Concorrenza di due sessioni Admin su dati esclusivamente sintetici.

Non modifica il database installato: _setup crea un'istanza temporanea.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi.testclient import TestClient

from test_pdf_import import _post, _setup
from test_regressione_stati import _finalize


def test_due_sessioni_non_possono_approvare_due_volte_la_stessa_bozza(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        base, _ = _finalize(client, sheets)
        gid = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]["conferma"]["id"]
        draft = _post(client, base + "/graduatorie/approvazioni",
                      json={"graduatorie_ids": [gid]})
        assert draft.status_code == 200, draft.text
        aid = draft.json()["id"]
        endpoint = base + f"/graduatorie/approvazioni/{aid}/conferma"
        payload = {"estremi_atto": "PROVVEDIMENTO SINTETICO PER TEST CONCORRENTE"}
        other = TestClient(server.app)
        try:
            login = _post(other, "/api/auth/login", json={
                "username": "test-admin",
                "password": "PasswordFittiziaLunga12345!",
            })
            assert login.status_code == 200, login.text
            start = Event()

            def approva(session):
                assert start.wait(timeout=15), "Test concorrente non avviato"
                return _post(session, endpoint, json=payload)

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(approva, session) for session in (client, other)]
                start.set()
                esiti = [future.result(timeout=35) for future in futures]
            assert sorted(r.status_code for r in esiti) == [200, 409], [
                (r.status_code, r.text) for r in esiti
            ]
            with server.connect() as db:
                cds = db.execute(
                    "SELECT stato,approvazione_id FROM graduatorie_cds WHERE id=?", (gid,)
                ).fetchone()
                assert cds["stato"] == "approvata"
                assert cds["approvazione_id"] == aid
                assert db.execute(
                    "SELECT stato FROM graduatorie_approvazioni WHERE id=?", (aid,)
                ).fetchone()[0] == "approvata"
                assert db.execute(
                    "SELECT COUNT(*) FROM graduatorie_eventi "
                    "WHERE evento='Atti approvati - incarichi chiusi' AND oggetto_id=?",
                    (aid,)
                ).fetchone()[0] == 1
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            other.close()
