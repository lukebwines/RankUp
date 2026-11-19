"""Fault injection su database SOLO temporanei di test, mai sulla produzione.

I trigger SQLite interrompono l'ultima registrazione dello storico, dopo
l'aggiornamento dello stato. Se il procedimento e' realmente atomico, tutte
le modifiche amministrative devono tornare allo stato precedente.
"""
from fastapi.testclient import TestClient

from test_pdf_import import _setup, _post
from test_regressione_stati import _finalize


def _guasto_audit(server, evento: str):
    """Simula indisponibilita' della scrittura di audit amministrativo."""
    consentiti = {
        "Atti approvati - incarichi chiusi",
        "Regressione approvazione registrata - storico originale conservato",
        "Regressione da definitiva a lavorabile",
    }
    assert evento in consentiti  # il valore SQL e' una costante di test
    with server.connect() as db:
        db.execute(
            "CREATE TRIGGER catastrofe_audit_non_scrivibile "
            "BEFORE INSERT ON graduatorie_eventi "
            f"WHEN NEW.evento = '{evento}' "
            "BEGIN SELECT RAISE(ABORT, 'guasto_audit_sintetico'); END"
        )
        db.commit()


def _ripristina_audit(server):
    with server.connect() as db:
        db.execute("DROP TRIGGER catastrofe_audit_non_scrivibile")
        db.commit()


def _tentativo_durante_guasto(server, endpoint: str, payload: dict):
    """Restituisce l'esito HTTP senza propagare l'errore SQLite al test."""
    client = TestClient(server.app, raise_server_exceptions=False)
    try:
        login = _post(client, "/api/auth/login", json={
            "username": "test-admin",
            "password": "PasswordFittiziaLunga12345!",
        })
        assert login.status_code == 200, login.text
        return _post(client, endpoint, json=payload)
    finally:
        client.close()


def _stato_cds(db, gid):
    return tuple(db.execute(
        "SELECT stato, approvazione_id FROM graduatorie_cds WHERE id=?", (gid,)
    ).fetchone())


def test_guasto_storico_durante_approvazione_annulla_tutte_le_modifiche(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        base, _ = _finalize(client, sheets)
        gid = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]["conferma"]["id"]
        bozza = _post(client, base + "/graduatorie/approvazioni",
                      json={"graduatorie_ids": [gid]})
        assert bozza.status_code == 200, bozza.text
        aid = bozza.json()["id"]
        endpoint = base + f"/graduatorie/approvazioni/{aid}/conferma"
        payload = {"estremi_atto": "ATTO ESCLUSIVAMENTE SINTETICO DI TEST"}
        with server.connect() as db:
            eventi_prima = db.execute("SELECT COUNT(*) FROM graduatorie_eventi").fetchone()[0]
            assert _stato_cds(db, gid) == ("definitiva", None)
        _guasto_audit(server, "Atti approvati - incarichi chiusi")
        try:
            response = _tentativo_durante_guasto(server, endpoint, payload)
            assert response.status_code == 500, response.text
            with server.connect() as db:
                assert _stato_cds(db, gid) == ("definitiva", None)
                row = db.execute(
                    "SELECT stato,approvata_il FROM graduatorie_approvazioni WHERE id=?",
                    (aid,)
                ).fetchone()
                assert row["stato"] == "bozza"
                assert row["approvata_il"] is None
                assert db.execute("SELECT COUNT(*) FROM graduatorie_eventi").fetchone()[0] == eventi_prima
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            _ripristina_audit(server)
        # Dopo il ripristino della scrittura, il procedimento e' ripetibile.
        ok = _post(client, endpoint, json=payload)
        assert ok.status_code == 200, ok.text


def test_guasto_storico_durante_regressione_generale_e_interamente_annullato(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        base, _ = _finalize(client, sheets)
        gid = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]["conferma"]["id"]
        draft = _post(client, base + "/graduatorie/approvazioni",
                      json={"graduatorie_ids": [gid]})
        assert draft.status_code == 200, draft.text
        aid = draft.json()["id"]
        approved = _post(
            client, base + f"/graduatorie/approvazioni/{aid}/conferma",
            json={"estremi_atto": "ATTO ESCLUSIVAMENTE SINTETICO DI TEST"}
        )
        assert approved.status_code == 200, approved.text
        endpoint = base + f"/graduatorie/approvazioni/{aid}/regredisci-stato"
        payload = {
            "motivazione": "Riapertura amministrativa fittizia per collaudo transazionale",
            "riferimento": "PROVVEDIMENTO SINTETICO NON UFFICIALE",
        }
        with server.connect() as db:
            eventi_prima = db.execute("SELECT COUNT(*) FROM graduatorie_eventi").fetchone()[0]
            assert _stato_cds(db, gid) == ("approvata", aid)
        _guasto_audit(server, "Regressione approvazione registrata - storico originale conservato")
        try:
            response = _tentativo_durante_guasto(server, endpoint, payload)
            assert response.status_code == 500, response.text
            with server.connect() as db:
                assert _stato_cds(db, gid) == ("approvata", aid)
                assert db.execute(
                    "SELECT stato FROM graduatorie_approvazioni WHERE id=?", (aid,)
                ).fetchone()[0] == "approvata"
                assert db.execute(
                    "SELECT COUNT(*) FROM graduatorie_approvazioni_regressioni WHERE approvazione_id=?",
                    (aid,)
                ).fetchone()[0] == 0
                assert db.execute("SELECT COUNT(*) FROM graduatorie_eventi").fetchone()[0] == eventi_prima
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            _ripristina_audit(server)
        ok = _post(client, endpoint, json=payload)
        assert ok.status_code == 200, ok.text


def test_guasto_storico_durante_riapertura_cds_non_lascia_revisioni_orfane(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        base, body = _finalize(client, sheets)
        gid = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]["conferma"]["id"]
        endpoint = base + "/verbali/graduatorie/regredisci-stato"
        payload = {**body, "motivazione": "Riapertura fittizia per collaudo del guasto di scrittura"}
        with server.connect() as db:
            eventi_prima = db.execute("SELECT COUNT(*) FROM graduatorie_eventi").fetchone()[0]
        _guasto_audit(server, "Regressione da definitiva a lavorabile")
        try:
            response = _tentativo_durante_guasto(server, endpoint, payload)
            assert response.status_code == 500, response.text
            with server.connect() as db:
                assert _stato_cds(db, gid) == ("definitiva", None)
                assert db.execute(
                    "SELECT COUNT(*) FROM graduatorie_cds_revisioni WHERE graduatoria_id=?",
                    (gid,)
                ).fetchone()[0] == 0
                assert db.execute(
                    "SELECT COUNT(*) FROM graduatorie_cds_riaperte WHERE graduatoria_id=?",
                    (gid,)
                ).fetchone()[0] == 0
                assert db.execute("SELECT COUNT(*) FROM graduatorie_eventi").fetchone()[0] == eventi_prima
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            _ripristina_audit(server)
        ok = _post(client, endpoint, json=payload)
        assert ok.status_code == 200, ok.text
