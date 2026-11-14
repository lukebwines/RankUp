"""Un bando archiviato deve essere immutabile anche con URL minuscoli o misti.

Tutti i dati dei test sono sintetici, senza effetti sui bandi reali.
"""
import json
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from test_pdf_import import _setup, _post


def _archive_for_test(server, code="ABC-2026", registered=False):
    with server.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE bandi SET codice=?, stato='Archiviato' WHERE codice='26-02'", (code,))
        if registered:
            db.execute("""
                INSERT INTO bandi_archivi
                (codice,nome_file,sha256,bytes,file_count,creato_da,
                 creato_il,stato_precedente,riepilogo_json)
                VALUES(?,?,?,?,?,?,?,?,?)
            """,(code,"rankup_bando_ABC-2026_20261110T090000Z_0000000000.zip",
                 "0"*64,0,0,"admin_test","2026-11-10T09:00:00+00:00",
                 "Candidature importate",json.dumps({"collaudo":True})))
        db.commit()


@pytest.mark.parametrize("path_code",["ABC-2026","abc-2026","AbC-2026"])
def test_middleware_blocks_all_case_variants_after_archive(tmp_path,monkeypatch,path_code):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        _archive_for_test(server)
        # Le GET rimangono lecite: l'archivio è consultabile.
        view=client.get(f"/api/bandi/{path_code}")
        assert view.status_code==200,view.text
        assert view.json()["bando"]["stato"]=="Archiviato"

        attempts=[
            ("PUT",f"/api/bandi/{path_code}/riferimenti",{
                "json":{"repertorio":"REPERTORIO SOLO DI TEST",
                        "protocollo":"","data_avviso":"","anno_accademico":"",
                        "expected_updated_at":None}
            }),
            ("POST",f"/api/bandi/{path_code}/commissioni/manuale",{
                "json":{"cds":"CDS DI TEST","sede":"VERONA",
                        "presidente":"Presidente sintetico",
                        "componenti":"Componente sintetico"}
            }),
            ("POST",f"/api/bandi/{path_code}/commissioni/recupera-globali",{
                "json":{"ids":[1],"conferma_verifica":True}
            }),
            ("POST",f"/api/bandi/{path_code}/valutazioni/conferma",{
                "json":{"token":"TOKEN_DI_TEST","conferma_rettifiche":False}
            }),
            ("POST",f"/api/bandi/{path_code}/verbali/graduatorie/genera-definitivo",{
                "json":{"cds":"CDS DI TEST","sede":"VERONA"}
            }),
            ("POST",f"/api/bandi/{path_code}/graduatorie/approvazioni",{
                "json":{"graduatorie_ids":[1]}
            }),
            ("POST",f"/api/bandi/{path_code}/archivio/chiudi",{
                "json":{}
            }),
        ]
        for method,url,params in attempts:
            r=client.request(method,url,headers={"X-RankUp-Request":"1"},**params)
            assert r.status_code==409,(method,url,r.status_code,r.text)
            assert "archiviato" in r.json()["detail"].lower()
        with server.connect() as db:
            b=db.execute("SELECT stato FROM bandi WHERE codice='ABC-2026'").fetchone()
            assert b["stato"]=="Archiviato"
            assert db.execute("SELECT COUNT(*) FROM bandi_riferimenti").fetchone()[0]==0
            assert db.execute("SELECT COUNT(*) FROM bandi_archivi").fetchone()[0]==0


def test_archived_numeric_code_is_still_blocked(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        with server.connect() as db:
            db.execute("UPDATE bandi SET stato='Archiviato' WHERE codice='26-02'")
            db.commit()
        blocked=client.put("/api/bandi/26-02/riferimenti",
            headers={"X-RankUp-Request":"1"},
            json={"repertorio":"TEST","protocollo":"",
                  "data_avviso":"","anno_accademico":"",
                  "expected_updated_at":None})
        assert blocked.status_code==409


def test_transaction_guard_covers_direct_handler_call(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        _archive_for_test(server)
        # Chiamata diretta: bypassa per definizione il middleware ASGI,
        # ma NON la seconda barriera nella transazione SQLite.
        with pytest.raises(HTTPException) as e:
            server.save_riferimenti_bando("abc-2026",server.RiferimentiBando(
                repertorio="ATTO SOLO DI TEST",protocollo="",data_avviso="",
                anno_accademico="",expected_updated_at=None),
                Request({"type":"http","headers":[],"method":"PUT"}))
        assert e.value.status_code==409
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM bandi_riferimenti").fetchone()[0]==0


def test_operator_is_blocked_but_formal_admin_reopening_still_works(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        created=_post(admin,"/api/utenti",json={
            "username":"operatore_test","password":"PasswordTest123!"})
        assert created.status_code==200,created.text
        _archive_for_test(server,registered=True)
        with TestClient(server.app) as user:
            authenticated=_post(user,"/api/auth/login",json={
                "username":"operatore_test","password":"PasswordTest123!"})
            assert authenticated.status_code==200,authenticated.text
            assert authenticated.json()["role"]=="user"
            denied=_post(user,"/api/bandi/abc-2026/commissioni/manuale",json={
                "cds":"CDS TEST","sede":"VERONA","presidente":"TEST",
                "componenti":"TEST"})
            assert denied.status_code==409
            reopen=_post(user,"/api/bandi/abc-2026/archivio/riapri",json={
                "motivazione":"Riapertura formale motivata per collaudo",
                "riferimento":"RIFERIMENTO DI TEST"})
            assert reopen.status_code==403,reopen.text

        approved=_post(admin,"/api/bandi/abc-2026/archivio/riapri",json={
            "motivazione":"Riapertura formale motivata per collaudo",
            "riferimento":"RIFERIMENTO SINTETICO DI TEST"})
        assert approved.status_code==200,approved.text
        with server.connect() as db:
            assert db.execute("SELECT stato FROM bandi WHERE codice='ABC-2026'").fetchone()[
                "stato"]=="Candidature importate"
            row=db.execute("SELECT riaperto_il,motivo_riapertura FROM bandi_archivi").fetchone()
            assert row["riaperto_il"] and "collaudo" in row["motivo_riapertura"]

        permitted=admin.put("/api/bandi/abc-2026/riferimenti",
            headers={"X-RankUp-Request":"1"},
            json={"repertorio":"TEST DOPO RIAPERTURA","protocollo":"",
                  "data_avviso":"","anno_accademico":"",
                  "expected_updated_at":None})
        assert permitted.status_code==200,permitted.text


def test_middleware_and_transaction_coverage_of_all_bando_writers():
    from pathlib import Path
    import re
    source=(Path(__file__).resolve().parents[1]/"rankup/app/server.py").read_text()
    assert "code=parts[3].upper()" in source
    assert "CODE_RE.fullmatch(code) and not is_formal_reopening" in source
    routes=list(re.finditer(
        r'@app\.(?:post|put|delete|patch)\("/api/bandi/\{code\}/([^"]+)"\)',source))
    exempt={"archivio/chiudi","archivio/riapri",
            "verbali/graduatorie/genera-definitivo"}
    for i,route in enumerate(routes):
        path=route.group(1)
        if path in exempt:continue
        end=source.find("\n@app.",route.end())
        block=source[route.start():end if end>=0 else None]
        assert "_require_bando_aperto(db,code)" in block,path
