"""Segnalazioni interne di bando e commissione."""
import importlib
import json

from fastapi.testclient import TestClient

from test_commission_management import _auth, _commit_pica, _up


def test_segnalazioni_operatore_admin_e_isolamento(tmp_path, monkeypatch):
    monkeypatch.setenv("RANKUP_DATA_DIR", str(tmp_path))
    (tmp_path / "options.json").write_text(json.dumps({
        "admin_username": "test-admin",
        "admin_password": "PasswordFittiziaLunga12345!",
    }))
    import server
    importlib.reload(server)

    with TestClient(server.app) as admin, TestClient(server.app) as operatore:
        _auth(admin)
        _commit_pica(admin)
        password = "PasswordOperatore12345!"
        assert _up(admin, "/api/utenti", json={
            "username": "operatore", "password": password,
        }).status_code in (200, 201)
        path = "/api/bandi/26-02/segnalazioni"
        assert operatore.get(path).status_code == 401
        assert _up(operatore, "/api/auth/login", json={
            "username": "operatore", "password": password,
        }).status_code == 200
        with server.connect() as db:
            bando_id = db.execute("SELECT id FROM bandi WHERE codice='26-02'").fetchone()[0]
            commissione_id = db.execute("""
                INSERT INTO commissioni (bando_id,cds,sede,presidente,componenti,updated_at)
                VALUES (?, 'CdS di prova', 'SEDE A', 'Presidente', '', '2026-01-01')
            """, (bando_id,)).lastrowid
        payload = {
            "titolo": "Verbale mancante", "testo": "Richiesto il reinvio",
            "commissione_id": commissione_id, "assegnato_a": "test-admin",
        }
        created = _up(operatore, path, json=payload)
        assert created.status_code == 201, created.text
        item = path + "/" + str(created.json()["id"])
        assert _up(operatore, path, json={**payload, "commissione_id": None}).status_code == 201
        assert _up(admin, item + "/risposte", json={"testo": "Ricevuto"}).status_code == 201
        update = {"stato": "in_carico", "assegnato_a": "test-admin", "revisione": 1}
        assert _up(admin, item, method="PUT", json=update).status_code == 200
        assert _up(admin, item, method="PUT", json=update).status_code == 409

        data = admin.get(path).json()
        assert data["utente"] == "test-admin"
        assert data["segnalazioni"][0]["commissione_id"] is None
        ticket = data["segnalazioni"][1]
        assert ticket["autore"] == "operatore"
        assert ticket["commissione_id"] == commissione_id
        assert ticket["stato"] == "in_carico"
        assert ticket["risposte"][0]["autore"] == "test-admin"

        with server.connect() as db:
            db.execute("""
                INSERT INTO bandi (codice,created_at,source_filename,source_hash,import_at)
                VALUES ('26-99', '', '', '', '')
            """)
            db.execute("UPDATE bandi SET stato='Archiviato' WHERE id=?", (bando_id,))
        assert _up(operatore, "/api/bandi/26-99/segnalazioni", json=payload).status_code == 404
        assert _up(admin, item.replace("26-02","26-99"), method="PUT", json=update).status_code == 404
        assert _up(operatore, path, json=payload).status_code == 409
        assert admin.get(path).json()["sola_lettura"] is True
