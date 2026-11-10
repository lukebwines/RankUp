"""Dashboard per anno accademico; solo dati esplicitamente associati da PICA/operatore.

Le procedure e i riferimenti usano dati sintetici, non atti amministrativi.
"""
from io import BytesIO
from pathlib import Path
from openpyxl import load_workbook
from test_import import pica_fixture
from test_pdf_import import _setup, _post
from test_graduatorie import _complete_all


def _row(client,code):
    response=client.get("/api/bandi")
    assert response.status_code==200,response.text
    return next(b for b in response.json()["bandi"] if b["codice"]==code)


def test_dashboard_bando_esistente_senza_anno_non_presume_aa(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        original=_row(client,"26-02")
        assert original["anno_accademico"]==""
        assert original["incarichi"]==2
        assert original["candidature"]==3
        assert original["valutazioni_acquisite"]==0
        assert original["cds_in_lavorazione"]==1

        saved=client.put("/api/bandi/26-02/riferimenti",
            headers={"X-RankUp-Request":"1"},json={
                "repertorio":"","protocollo":"","data_avviso":"",
                "anno_accademico":"2026-2027","expected_updated_at":None
            })
        assert saved.status_code==200,saved.text
        associated=_row(client,"26-02")
        assert associated["anno_accademico"]=="2026-2027"
        assert associated["cds_in_lavorazione"]==1

        _complete_all(client,sheets)
        evaluated=_row(client,"26-02")
        assert evaluated["valutazioni_acquisite"]==3
        assert evaluated["cds_in_lavorazione"]==1
        group=client.get("/api/bandi/26-02/verbali/graduatorie").json()["graduatorie"][0]
        sealed=_post(client,"/api/bandi/26-02/verbali/graduatorie/genera-definitivo",json={
            "cds":group["cds"],"sede":group["sede"]
        })
        assert sealed.status_code==200,sealed.text
        now=_row(client,"26-02")
        assert now["valutazioni_acquisite"]==3
        assert now["cds_in_lavorazione"]==0


def test_anno_esplicito_nel_nuovo_bando_e_validazioni(tmp_path,monkeypatch):
    server,client,_sheets=_setup(tmp_path,monkeypatch)
    with client:
        invalid=_post(client,"/api/bandi",json={
            "codice":"PROVA-ANNO","anno_accademico":"2026-2028"})
        assert invalid.status_code==400,invalid.text
        created=_post(client,"/api/bandi",json={
            "codice":"PROVA-ANNO","anno_accademico":"2027-2028"})
        assert created.status_code==200,created.text
        assert created.json()["anno_accademico"]=="2027-2028"
        assert _row(client,"PROVA-ANNO")["anno_accademico"]=="2027-2028"
        assert client.get("/api/bandi/PROVA-ANNO/riferimenti").json()[
            "riferimenti"]["anno_accademico"]=="2027-2028"
        with server.connect() as db:
            events=db.execute("""
              SELECT prima_json,dopo_json FROM bandi_riferimenti_eventi
              WHERE bando_id=(SELECT id FROM bandi WHERE codice='PROVA-ANNO')
            """).fetchall()
        assert len(events)==1
        assert "2027-2028" in events[0]["dopo_json"]
        assert "2027-2028" not in events[0]["prima_json"]


def test_import_pica_non_sovrascrive_anno_bando_gia_preparato(tmp_path,monkeypatch):
    _,client,_sheets=_setup(tmp_path,monkeypatch)
    with client:
        prepared=_post(client,"/api/bandi",json={
            "codice":"26-03","anno_accademico":"2027-2028"})
        assert prepared.status_code==200,prepared.text
        book=load_workbook(BytesIO(pica_fixture()))
        book.active["A1"]="univr 26-03 - esportazione di test"
        output=BytesIO()
        book.save(output)
        uploaded=_post(client,"/api/pica/preview",files={
            "file":("pica_26_03_sintetico.xlsx",output.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        })
        assert uploaded.status_code==200,uploaded.text
        token=uploaded.json()["token"]
        mismatch=_post(client,"/api/pica/commit",json={
            "token":token,"anno_accademico":"2026-2027"})
        assert mismatch.status_code==409,mismatch.text
        assert _row(client,"26-03")["candidature"]==0
        imported=_post(client,"/api/pica/commit",json={
            "token":token,"anno_accademico":"2027-2028"})
        assert imported.status_code==200,imported.text
        row=_row(client,"26-03")
        assert row["anno_accademico"]=="2027-2028"
        assert row["candidature"]==3
        assert row["incarichi"]==2
        assert row["cds_in_lavorazione"]==1


def test_aa_first_ui_keeps_original_four_icons():
    html=(Path(__file__).resolve().parents[1]/
          "rankup/app/static/index.html").read_text(encoding="utf-8")
    assert 'id="academic-year-select"' in html
    assert 'id="band-select"' in html
    assert html.index('id="academic-year-select"') < html.index('id="band-select"')
    assert 'id="add-academic-year"' in html
    assert 'var icons=[\u0027ri-folders\u0027,\u0027ri-users\u0027,\u0027ri-book\u0027,\u0027ri-clipboard\u0027];' in html
    for label in ("Bandi aperti nell'A.A.","Candidature PICA del bando",
                  "Incarichi nel bando","Candidature valutate",
                  "CdS/sedi ancora in lavorazione"):
        assert label in html
    assert 'state.preview.anno_accademico=state.year' in html
    assert 'anno_accademico:state.preview.anno_accademico' in html
    assert "anno_accademico:state.year" in html
    assert "Da associare (A.A. mancante)" in html
    assert "Tutti i bandi dell’A.A." in html
    assert "var shown=selected?[selected]:scoped;" in html
    assert 'scoped.reduce(' not in html
    assert 'count("candidature")' in html
    assert 'count("incarichi")' in html
    assert 'count("valutazioni_acquisite")' in html
    assert 'count("cds_in_lavorazione")' in html
    assert 'state.active=bandiAnno().length?bandiAnno()[0].codice:"";' not in html
    assert "state.active=\"\";" in html

