"""Regressioni: login senza lockout globale, ZIP senza writer-lock, task IA.

Documenti e riferimenti esclusivamente sintetici per il collaudo.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException
from test_pdf_import import _setup, _post
from test_graduatorie import _complete_all


def _completed_bando(client,sheets):
    _complete_all(client,sheets)
    base="/api/bandi/26-02"
    item=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
    assert _post(client,base+"/verbali/graduatorie/genera-definitivo",json={
        "cds":item["cds"],"sede":item["sede"]}).status_code==200
    gid=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]["conferma"]["id"]
    created=_post(client,base+"/graduatorie/approvazioni",
                  json={"graduatorie_ids":[gid]})
    assert created.status_code==200,created.text
    confirmed=_post(client,base+"/graduatorie/approvazioni/"+
                    str(created.json()["id"])+"/conferma",
                    json={"estremi_atto":"RIFERIMENTO DI COLLAUDO NON REALE"})
    assert confirmed.status_code==200,confirmed.text


def test_archive_snapshot_does_not_block_other_writers_and_rejects_stale_zip(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        _completed_bando(client,sheets)
        started=threading.Event()
        proceed=threading.Event()
        original=server.crea_pacchetto
        def slow_bundle(*args,**kwargs):
            started.set()
            assert proceed.wait(12),"Archivio non sbloccato dal test"
            return original(*args,**kwargs)
        monkeypatch.setattr(server,"crea_pacchetto",slow_bundle)
        def try_close():
            try:
                return server._chiudi_bando_con_snapshot("26-02","admin_sintetico")
            except HTTPException as exc:
                return exc
        with ThreadPoolExecutor(max_workers=1) as jobs:
            future=jobs.submit(try_close)
            assert started.wait(12),"La copia SQLite non è arrivata all'assemblaggio"
            began=time.monotonic()
            # Un altro bando viene creato mentre lo ZIP è in preparazione:
            # operazione che in precedenza rischiava 'database is locked'.
            other=_post(client,"/api/bandi",json={
                "codice":"OTHER-2026","anno_accademico":"2026-2027"})
            duration=time.monotonic()-began
            proceed.set()
            assert other.status_code==200,other.text
            assert duration<8,"SQLite writer bloccato durante compressione ZIP"
            result=future.result(timeout=20)
        assert isinstance(result,HTTPException),result
        assert result.status_code==409
        assert "modificat" in result.detail.lower()
        with server.connect() as db:
            assert db.execute("SELECT stato FROM bandi WHERE codice='26-02'").fetchone()[
                "stato"]!="Archiviato"
            assert db.execute("SELECT COUNT(*) FROM bandi_archivi").fetchone()[0]==0
        monkeypatch.setattr(server,"crea_pacchetto",original)
        final=_post(client,"/api/bandi/26-02/archivio/chiudi",json={})
        assert final.status_code==200,final.text
        assert client.get("/api/archivio/"+str(final.json()["archivio_id"])+"/zip").status_code==200


def test_background_archive_returns_job_quickly_and_finishes(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        _completed_bando(client,sheets)
        started=_post(client,"/api/bandi/26-02/archivio/chiudi/avvia",json={})
        assert started.status_code==202,started.text
        job_id=started.json()["lavoro_id"]
        assert job_id
        status=None
        for _ in range(160):
            r=client.get("/api/bandi/26-02/archivio/chiudi/lavoro/"+job_id)
            assert r.status_code==200,r.text
            status=r.json()
            if status["stato"] in ("completato","errore"):break
            time.sleep(0.05)
        assert status["stato"]=="completato",status
        assert client.get("/api/bandi/26-02") .json()["bando"]["stato"]=="Archiviato"
        archive_id=status["risultato"]["archivio_id"]
        assert client.get("/api/archivio/"+str(archive_id)+"/zip").status_code==200


def test_login_not_globally_or_per_username_locked_by_unrelated_origins(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        auth=server.AUTH
        # Simula molti errori provenienti da indirizzi diversi. In precedenza
        # il centesimo errore globale disabilitava anche l'Admin.
        now=time.monotonic()
        with auth._lock:
            auth._failures.extend((now,"sorgente-"+str(x)) for x in range(150))
            auth._failures.extend((now,"origine-attaccante") for _ in range(24))
        assert auth.authenticate("test-admin","PasswordFittiziaLunga12345!",
                                 source="origine-attaccante") is None
        token=auth.authenticate("test-admin","PasswordFittiziaLunga12345!",
                                 source="origine-admin")
        assert token and auth.principal(token)["role"]=="admin"
        # Le stesse otto password errate su una sorgente non bloccano il nome
        # utente per chi entra da un'origine diversa.
        with auth._lock:
            auth._failures.extend((now,"altra-origine") for _ in range(8))
        assert auth.authenticate("test-admin","PasswordFittiziaLunga12345!",
                                 source="indirizzo-legittimo")


def test_no_ollama_runtime_enabled(tmp_path, monkeypatch):
    server, client, _ = _setup(tmp_path, monkeypatch)
    with client:
        assert not hasattr(server, "_ollama_settings")
        assert not hasattr(server, "initialize_ai_work_items")
        assert not any("/ai/occupazione" in route.path for route in server.app.routes)
