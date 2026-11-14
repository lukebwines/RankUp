"""RankUP 1.0.28: anagrafica commissioni condivisa e versionamento.

Tutti i dati e nomi qui sono esclusivamente sintetici.
"""
from pathlib import Path
from fastapi.testclient import TestClient
from test_pdf_import import _setup, _post


ROOT = Path(__file__).resolve().parents[1]


def test_operators_manage_registry_and_versions_without_mutating_bandi(tmp_path, monkeypatch):
    server, admin, _ = _setup(tmp_path, monkeypatch)
    with admin:
        original = admin.get("/api/bandi/26-02/commissioni").json()["commissioni"]
        created = _post(admin,"/api/utenti",json={
            "username":"commissionista-test","password":"PasswordLungaDiTest123!"
        })
        assert created.status_code == 200, created.text

        with TestClient(server.app) as operator:
            login = _post(operator,"/api/auth/login",json={
                "username":"commissionista-test","password":"PasswordLungaDiTest123!"
            })
            assert login.status_code == 200 and login.json()["role"] == "user"
            assert operator.get("/api/admin/commissioni-globali").status_code == 403
            catalog = operator.get("/api/commissioni/anagrafica")
            assert catalog.status_code == 200, catalog.text

            new={"cds":"CORSO DI PROVA NON PICA","sede":"SEDE PROVA",
                 "presidente":"Persona Sintetica","componenti":"Componente Prova",
                 "requisiti":"Da verificare con atto ufficiale","anno":""}
            inserted = _post(operator,"/api/commissioni/anagrafica",json=new)
            assert inserted.status_code == 200, inserted.text
            first = inserted.json()["id"]
            assert first > 0
            assert _post(operator,"/api/commissioni/anagrafica",json=new).status_code == 409

            changed=dict(new,componenti="Componente Prova Due")
            updated=_post(operator,f"/api/commissioni/anagrafica/{first}/versioni",json=changed)
            assert updated.status_code == 200, updated.text
            second=updated.json()["id"]
            assert second != first
            assert updated.json()["versione_precedente_id"] == first
            assert _post(operator,f"/api/commissioni/anagrafica/{first}/versioni",
                         json=dict(new,componenti="Terza composizione")).status_code == 409
            assert _post(operator,f"/api/commissioni/anagrafica/{second}/versioni",
                         json=dict(new,sede="ALTRA SEDE")).status_code == 409

            archive = operator.get("/api/commissioni/anagrafica").json()["commissioni"]
            assert {first,second}.issubset({r["id"] for r in archive})
            with server.connect() as db:
                events=db.execute("""
                    SELECT catalogo_id,precedente_id,azione,autore
                    FROM commissioni_catalogo_eventi
                    ORDER BY id
                """).fetchall()
                assert len(events) == 2
                assert events[0]["autore"] == "commissionista-test"
                assert events[1]["precedente_id"] == first
                assert events[1]["autore"] == "commissionista-test"
                assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 12

            # Non devono esserci modifiche implicite alle commissioni dei bandi.
            assert operator.get("/api/bandi/26-02/commissioni").json()["commissioni"] == original


def test_operator_can_associate_only_with_documentary_confirmation(tmp_path, monkeypatch):
    server, admin, _ = _setup(tmp_path, monkeypatch)
    with admin:
        _post(admin, "/api/utenti", json={
            "username":"operatore-verifica", "password":"PasswordLungaDiTest123!"
        })
        with server.connect() as db:
            bando_id=db.execute("SELECT id FROM bandi WHERE codice='26-02'").fetchone()[0]
            # I dati sono sintetici, la rimozione riguarda solo la base di test.
            db.execute("DELETE FROM commissioni WHERE bando_id=?",(bando_id,))
            catalogo=db.execute("""
                SELECT id FROM commissioni_catalogo WHERE cds='CDS DEMO' AND sede='SEDE A'
                ORDER BY id DESC LIMIT 1
            """).fetchone()
            assert catalogo
            catalog_id=int(catalogo[0])
        with TestClient(server.app) as operator:
            assert _post(operator,"/api/auth/login",json={
                "username":"operatore-verifica", "password":"PasswordLungaDiTest123!"
            }).status_code == 200
            declined=_post(operator,"/api/bandi/26-02/commissioni/recupera-globali",
                           json={"ids":[catalog_id],"conferma_verifica":False})
            assert declined.status_code == 400
            assert operator.get("/api/bandi/26-02/commissioni") .json()["commissioni"] == []
            approved=_post(operator,"/api/bandi/26-02/commissioni/recupera-globali",
                           json={"ids":[catalog_id],"conferma_verifica":True})
            assert approved.status_code == 200, approved.text
            assert approved.json()["aggiunte"] == 1
            report=operator.get("/api/bandi/26-02/commissioni").json()["commissioni"]
            assert len(report) == 1
            assert report[0]["presidente"] == "Presidente"


def test_registry_ui_has_no_direct_link_bando_association():
    html = (ROOT/"rankup/app/static/index.html").read_text(encoding="utf-8")
    assert 'id="admin-commissioni-link"' in html
    assert '$("admin-commissioni-link").style.display="flex"' in html
    assert 'api("commissioni/anagrafica")' in html
    assert 'id="cm-archive-new"' in html
    assert 'id="cm-reg-form"' in html
    assert 'id="cm-reg-editor"' in html
    assert 'data-cm-reg-edit' in html
    assert 'state.cmArchivePrefill' in html
    assert 'global-commission-reuse' not in html
    assert 'global-commission-verified' not in html
    assert "Associa a un nuovo bando" not in html
    assert 'id="cm-catalog-associate"' in html
    assert 'conferma_verifica:true' in html
