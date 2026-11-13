"""Collaudo export RankUP 1.0.19, con dati soltanto sintetici.

Gli esempi sono test e non rappresentano candidature reali.
"""
from io import BytesIO
from openpyxl import load_workbook

from ai_ente_excel import render_ente_report_xlsx,HEADERS,MIME_XLSX
from test_ai_queue_v109 import _prepare,HEADERS as HTTP_HEADERS
from test_ai_commission_v1018 import _add_sheet
from test_pdf_import import _post


def _book(reply):
    assert reply.status_code==200,reply.text
    assert reply.headers["content-type"].startswith(MIME_XLSX)
    assert reply.headers["cache-control"]=="no-store"
    assert reply.headers["content-disposition"].endswith('.xlsx"')
    assert reply.content.startswith(b"PK")
    return load_workbook(BytesIO(reply.content))


def test_excel_admin_only_and_contains_all_five_actual_pica_fields(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        rows=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()["casi"]
        first=rows[0]
        report=client.get("/api/admin/ai/report.xlsx",params={"code":"26-02"})
        wb=_book(report)
        assert wb.sheetnames==["Candidature","Valori AI","Riepilogo"]
        sheet=wb["Candidature"]
        assert [x.value for x in sheet[1]]==HEADERS
        assert sheet.max_row==len(rows)+1
        assert sheet.freeze_panes=="C2"
        assert len(sheet.tables)==1
        assert sheet.tables["RankUPVerificaEnte"].ref.endswith(str(sheet.max_row))
        assert sheet.auto_filter.ref is None  # I filtri sono propri della tabella Excel
        record=next(r for r in sheet.iter_rows(min_row=2,values_only=True)
                    if r[0]==str(first["id_pica"]))
        assert record[1]==first["cognome"]
        assert record[2]==first["nome"]
        assert record[7]==(first["campi_pica_originali"].get("Azienda/ente") or "Non dichiarato")
        assert record[8]==(first["campi_pica_originali"].get("Ateneo") or "Non dichiarato")
        assert record[9]==(first["campi_pica_originali"].get("Occupazione") or "Non dichiarato")
        assert record[10]==(first["campi_pica_originali"].get("Profilo professionale") or "Non dichiarato")
        assert record[11]==(first["campi_pica_originali"].get("Qualifica") or "Non dichiarato")
        assert record[19]=="Da valutare"
        assert record[21] is None and record[22] is None
        assert wb["Valori AI"].max_row==len(rows)+1
        assert wb["Riepilogo"]["B10"].value==len(rows)
        assert client.get("/api/admin/ai/report.xlsx",params={
            "code":"26-02","graduatoria":"invalid"}).status_code==400
        assert client.get("/api/admin/ai/report.xlsx",params={
            "code":"26-02","ricerca":"a"*121}).status_code==400


def test_xlsx_filters_match_screen_and_pdf_including_search(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        with server.connect() as db:
            bid,items=server._ai_scope(db,"26-02","","")
            first,second=items[:2]
            _add_sheet(db,bid,first["id"],"idoneo",17.5)
            _add_sheet(db,bid,second["id"],"non_idoneo",None)
            db.commit()
        all_rows=client.get("/api/admin/ai/verifiche",params={
            "code":"26-02","graduatoria":"inserito"})
        assert all_rows.status_code==200,all_rows.text
        assert all_rows.json()["visualizzati"]==1
        selected=client.get("/api/admin/ai/report.xlsx",params={
            "code":"26-02","esito":"DA APPROFONDIRE",
            "graduatoria":"inserito","ricerca":str(first["id_pica"])})
        wb=_book(selected)
        assert wb["Candidature"].max_row==2
        row=list(wb["Candidature"].iter_rows(min_row=2,values_only=True))[0]
        assert row[0]==str(first["id_pica"])
        assert row[19]=="In graduatoria · provvisoria"
        assert row[21]==1
        assert row[22]==17.5
        assert row[23]=="No"
        summary=wb["Riepilogo"]
        assert summary["B6"].value=="DA APPROFONDIRE"
        assert summary["B8"].value=="inserito"
        assert summary["B9"].value==str(first["id_pica"])
        assert summary["B10"].value==1
        mismatch=client.get("/api/admin/ai/report.xlsx",params={
            "code":"26-02","graduatoria":"inserito",
            "ricerca":"999999999-INESISTENTE"})
        assert mismatch.status_code==200
        empty=load_workbook(BytesIO(mismatch.content))
        assert empty["Candidature"].max_row==1
        pdf=client.get("/api/admin/ai/report.pdf",params={
            "code":"26-02","graduatoria":"inserito",
            "ricerca":str(first["id_pica"])})
        assert pdf.status_code==200
        assert pdf.headers["content-type"]=="application/pdf"
        assert client.get("/api/admin/ai/report.pdf",params={
            "code":"26-02","ricerca":"a"*121}).status_code==400


def test_sanitize_formula_injection_and_preserve_leading_zero_identifier():
    row={
        "id_pica":"0001234","cognome":"=2+3","nome":"Esempio",
        "cds":"Corso di prova","sede":"Sede di prova",
        "insegnamento":"Insegnamento sintetico",
        "requisito":"ALTRO ATENEO",
        "campi_pica_originali":{
            "Azienda/ente":"=HYPERLINK(\"https://evil.example\",\"Click\")",
            "Ateneo":"+SUM(1,2)","Occupazione":"@NOW()",
            "Profilo professionale":"-1+4","Qualifica":"Ricercatore",
        },
        "campi_pica_ai":{"Ateneo":"DENOMINAZIONE NON TRASMETTIBILE"},
        "fonti_pica":["Qualifica"],
        "esito":"DA APPROFONDIRE",
        "tipo_approfondimento":"DATI INSUFFICIENTI",
        "commissione":{"stato_commissione":"Da valutare",
            "stato_graduatoria":"da_valutare"},
    }
    payload=render_ente_report_xlsx("TEST-01","Corso di prova",
                                   "Sede di prova",[row],{})
    wb=load_workbook(BytesIO(payload),data_only=False)
    cells=list(wb["Candidature"].iter_rows(min_row=2,max_row=2))[0]
    assert cells[0].value=="0001234" and cells[0].data_type=="s"
    for col in (1,7,8,9,10):
        assert cells[col].data_type=="s"
        assert cells[col].value.startswith("'")
    assert not any(cell.data_type=="f" for sheet in wb for rr in sheet for cell in rr)
    assert wb["Valori AI"]["E2"].value=="DENOMINAZIONE NON TRASMETTIBILE"


def test_xlsx_endpoint_restricted_to_admin(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    server,admin=_prepare(tmp_path,monkeypatch)
    with admin:
        created=_post(admin,"/api/utenti",json={
            "username":"operatore-export","password":"PasswordUtenteTest123!"})
        assert created.status_code==200,created.text
        with TestClient(server.app) as guest:
            assert _post(guest,"/api/auth/login",json={
                "username":"operatore-export","password":"PasswordUtenteTest123!"}).status_code==200
            assert guest.get("/api/admin/ai/report.xlsx?code=26-02").status_code==403


def test_ui_offers_excel_download_using_filters_and_no_new_ai_call():
    from pathlib import Path
    html=(Path(__file__).parents[1]/"rankup/app/static/index.html").read_text("utf-8")
    assert 'id="ai-report-excel"' in html
    assert 'data-xlsx-download' in html
    assert '"api/admin/ai/report.xlsx?"+filters' in html
    assert 'p.set("ricerca",$("ai-ricerca").value.trim())' in html
    assert 'downloadLinkedXlsx' in html
