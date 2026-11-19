"""Contatori delle segnalazioni non risolte, nel menu di RankUP.

I casi sono sintetici e non modificano i bandi reali.
"""
import importlib
import json

from fastapi.testclient import TestClient
from test_commission_management import _auth, _commit_pica, _up
from test_pdf_import import _complete_operator_onboarding


def test_contatore_isola_eventi_sistema_e_ignora_archiviati(tmp_path, monkeypatch):
    monkeypatch.setenv("RANKUP_DATA_DIR", str(tmp_path))
    (tmp_path / "options.json").write_text(json.dumps({
        "admin_username": "test-admin",
        "admin_password": "PasswordFittiziaLunga12345!",
    }))
    import server
    importlib.reload(server)

    with TestClient(server.app) as admin, TestClient(server.app) as operatore:
        endpoint = "/api/segnalazioni/contatori"
        assert operatore.get(endpoint).status_code == 401
        _auth(admin)
        _commit_pica(admin)
        password = "PasswordOperatore12345!"
        assert _up(admin, "/api/utenti", json={
            "username": "operatore", "password": password,
        }).status_code in (200, 201)
        assert _up(operatore, "/api/auth/login", json={
            "username": "operatore", "password": password,
        }).status_code == 200
        _complete_operator_onboarding(operatore, "operatore", password)

        with server.connect() as db:
            bando_id = db.execute(
                "SELECT id FROM bandi WHERE codice='26-02'"
            ).fetchone()[0]
            rows = [
                ("Messaggio di test aperto", "operatore", "aperta"),
                ("Richiesta di test in carico", "operatore", "in_carico"),
                ("[CRITICO] Controllo sintetico", server.SYSTEM_ALERT_ACTOR, "aperta"),
                ("[ATTENZIONE] Test risolto", server.SYSTEM_ALERT_ACTOR, "risolta"),
            ]
            for title, author, state in rows:
                db.execute("""INSERT INTO segnalazioni
                    (bando_id,commissione_id,titolo,testo,autore,
                     assegnato_a,stato,creata_il)
                    VALUES (?,NULL,?,?,?,?,?,?)""",
                    (bando_id,title,"Test sintetico",author,
                     "test-admin",state,"2026-11-19T00:00:00"))
            db.commit()

        counts = admin.get(endpoint)
        assert counts.status_code == 200, counts.text
        assert counts.json()["totale"] == 3
        assert counts.json()["critiche"] == 1
        assert counts.json()["per_bando"] == [
            {"codice": "26-02", "totale": 3, "critiche": 1}
        ]

        visible = operatore.get(endpoint)
        assert visible.status_code == 200, visible.text
        assert visible.json()["totale"] == 2
        assert visible.json()["critiche"] == 0
        listing = operatore.get("/api/bandi/26-02/segnalazioni")
        assert listing.status_code == 200
        assert all(x["autore"] != server.SYSTEM_ALERT_ACTOR
                   for x in listing.json()["segnalazioni"])

        with server.connect() as db:
            db.execute("UPDATE segnalazioni SET stato='risolta' WHERE autore=?",
                       (server.SYSTEM_ALERT_ACTOR,))
            db.commit()
        assert admin.get(endpoint).json()["critiche"] == 0
        assert admin.get(endpoint).json()["totale"] == 2

        with server.connect() as db:
            db.execute("UPDATE bandi SET stato='Archiviato' WHERE id=?",
                       (bando_id,))
            db.commit()
        assert admin.get(endpoint).json()["totale"] == 0
        assert operatore.get(endpoint).json()["totale"] == 0
