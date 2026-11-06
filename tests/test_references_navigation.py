"""Controlli per la collocazione dei riferimenti del singolo bando.

I dati utilizzati sono soltanto sintetici e non costituiscono atti ufficiali.
"""
from pathlib import Path

from test_pdf_import import _setup


def test_references_are_a_procedure_page_not_in_global_bandi():
    html=(Path(__file__).resolve().parents[1]/"rankup"/"app"/"static"/"index.html").read_text(encoding="utf-8")
    assert '<button data-page="riferimenti"' in html
    nav=html.split('<nav id="nav">',1)[1].split("</nav>",1)[0]
    assert 'data-page="bandi"' not in nav
    assert (nav.index('data-page="dashboard"')
            < nav.index("PROCEDIMENTO DEL BANDO ATTIVO")
            < nav.index('data-page="riferimenti"')
            < nav.index('data-page="incarichi"'))
    assert 'riferimenti:"Riferimenti del bando"' in html
    assert 'if(state.page==="riferimenti")return renderBandoReferences();' in html

    dashboard=html.split("function dashboard(){",1)[1].split("function go(page)",1)[0]
    assert "renderBandoReferences()" not in dashboard
    assert "bando-refs" not in dashboard
    assert "Importa esportazione PICA" in dashboard
    assert 'id="pica-file"' in dashboard
    assert '$("preview-btn").onclick=preview' in dashboard

    refs=html.split("async function renderBandoReferences(){",1)[1].split("async function preview(){",1)[0]
    assert 'if(state.page!=="riferimenti")return;' in refs
    assert 'state.page!=="riferimenti"' in refs
    for field in ("repertorio","protocollo","data_avviso","anno_accademico"):
        assert field in refs
    assert 'expected_updated_at:r.aggiornato_il||null' in refs
    assert 'encodeURIComponent(code)+"/riferimenti"' in refs


def test_reference_values_remain_isolated_per_bando(tmp_path,monkeypatch):
    """La modifica al menu non deve cambiare l'associazione dei riferimenti."""
    from datetime import datetime, timezone
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        code="26-02"
        payload={
            "repertorio":"REPERTORIO DI TEST",
            "protocollo":"PROTOCOLLO DI TEST",
            "data_avviso":"",
            "anno_accademico":"",
            "expected_updated_at":None,
        }
        saved=client.put("/api/bandi/"+code+"/riferimenti",
            headers={"X-RankUp-Request":"1"},json=payload)
        assert saved.status_code==200,saved.text
        info=client.get("/api/bandi/"+code+"/riferimenti").json()["riferimenti"]
        assert info["repertorio"]=="REPERTORIO DI TEST"
        assert info["protocollo"]=="PROTOCOLLO DI TEST"

        # Secondo bando fittizio, senza importare dati da documenti esemplificativi.
        with server.connect() as db:
            now=datetime.now(timezone.utc).isoformat()
            db.execute("""
                INSERT INTO bandi
                  (codice,created_at,source_filename,source_hash,import_at)
                VALUES(?,?,?,?,?)
            """,("TEST-REF",now,"sintetico.xlsx","hash-di-test",now))
            db.commit()
        other=client.get("/api/bandi/TEST-REF/riferimenti")
        assert other.status_code==200,other.text
        refs=other.json()["riferimenti"]
        assert refs["repertorio"]==""
        assert refs["protocollo"]==""
        still=client.get("/api/bandi/"+code+"/riferimenti").json()["riferimenti"]
        assert still["repertorio"]=="REPERTORIO DI TEST"
