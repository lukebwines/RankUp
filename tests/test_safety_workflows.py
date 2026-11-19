"""Test end-to-end con dati sintetici: blocchi prima degli atti amministrativi."""
from test_pdf_import import _setup, _post
from test_regressione_stati import _finalize


def test_regressione_sostanziale_richiede_documento_commissione(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        base, body = _finalize(client, sheets)
        endpoint = base + "/verbali/graduatorie/regredisci-stato"
        absent = _post(client, endpoint, json={
            **body, "tipo": "sostanziale",
            "motivazione": "Rettifica della valutazione verificata, prova sintetica",
        })
        assert absent.status_code == 400, absent.text
        assert client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]["stato"] == "definitiva"
        reopened = _post(client, endpoint, json={
            **body, "tipo": "sostanziale",
            "motivazione": "Rettifica della valutazione verificata, prova sintetica",
            "riferimento": "Verbale Commissione di test verificato",
        })
        assert reopened.status_code == 200, reopened.text
        revision = reopened.json()["revisione_id"]
        state = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]
        assert state["riapertura_attiva"] == revision
        add = _post(client, base + "/verbali/graduatorie/rettifica-documentata", json={
            "revisione_id": revision,
            "motivazione": "Integrazione istruttoria di collaudo documentata",
            "riferimento": "Documento integrativo della Commissione di test",
        })
        assert add.status_code == 200, add.text
        with server.connect() as db:
            event = db.execute("""SELECT dettagli_json FROM graduatorie_eventi
                WHERE evento='Riferimento Commissione integrativo'
                ORDER BY id DESC LIMIT 1""").fetchone()
            assert event and str(revision) in event["dettagli_json"]


def test_approvazione_blocca_pdf_definitivo_modificato(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        base, body = _finalize(client, sheets)
        ready = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]
        gid = ready["conferma"]["id"]
        draft = _post(client, base + "/graduatorie/approvazioni",
                      json={"graduatorie_ids": [gid]})
        assert draft.status_code == 200, draft.text
        approval_id = draft.json()["id"]
        with server.connect() as db:
            original = db.execute("""SELECT pdf_definitivo FROM graduatorie_cds_documenti
               WHERE graduatoria_id=?""",(gid,)).fetchone()
            assert original is not None
            db.execute("""UPDATE graduatorie_cds_documenti SET pdf_definitivo=?
               WHERE graduatoria_id=?""",(bytes(original["pdf_definitivo"])+b"ALTERATO",gid))
            db.commit()
        attempt = _post(client, base + f"/graduatorie/approvazioni/{approval_id}/conferma",
                        json={"estremi_atto": "PROVVEDIMENTO SINTETICO DI TEST"})
        assert attempt.status_code == 409, attempt.text
        after = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]
        assert after["stato"] == "definitiva"
        with server.connect() as db:
            state = db.execute("""SELECT stato FROM graduatorie_approvazioni
                WHERE id=?""",(approval_id,)).fetchone()[0]
        assert state == "bozza"
