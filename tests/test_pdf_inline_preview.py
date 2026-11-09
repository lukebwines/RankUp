"""Anteprima PDF nativa di Chrome: stessi byte e controlli dello scaricamento."""
from io import BytesIO

from pypdf import PdfReader
from test_pdf_import import _setup, _post
from test_graduatorie import _complete_all


def _assert_pdf_response(response, disposition):
    assert response.status_code == 200, response.text[:300] if response.status_code != 200 else ""
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith(disposition + "; filename=")
    assert response.content.startswith(b"%PDF-")
    return len(PdfReader(BytesIO(response.content)).pages)


def test_verbale_pdf_can_open_inline_and_download(tmp_path, monkeypatch):
    _, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        _complete_all(client, sheets)
        base="/api/bandi/26-02"
        area=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        params={"cds":area["cds"],"sede":area["sede"]}
        original=client.get(base+"/verbali/graduatorie/pdf-completo",params=params)
        pages=_assert_pdf_response(original,"attachment")
        preview=client.get(base+"/verbali/graduatorie/pdf-completo",
                           params={**params,"inline":"true"})
        assert _assert_pdf_response(preview,"inline")==pages
        assert [p.extract_text() for p in PdfReader(BytesIO(original.content)).pages] == [
            p.extract_text() for p in PdfReader(BytesIO(preview.content)).pages]

        finalized=_post(client,base+"/verbali/graduatorie/definitiva",json=params)
        assert finalized.status_code==200,finalized.text
        regress=_post(client,base+"/verbali/graduatorie/regredisci-stato",
            json={**params,"motivazione":"Collaudo riapertura e archivio completo"})
        assert regress.status_code==200,regress.text
        archived_url=base+"/verbali/graduatorie/revisioni/"+str(regress.json()["revisione_id"])+"/pdf"
        assert _assert_pdf_response(client.get(archived_url),"attachment")==pages
        assert _assert_pdf_response(client.get(archived_url,params={"inline":"true"}),"inline")==pages


def test_general_approval_pdf_can_open_inline(tmp_path, monkeypatch):
    _, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        _complete_all(client, sheets)
        base="/api/bandi/26-02"
        g=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        data={"cds":g["cds"],"sede":g["sede"]}
        assert _post(client,base+"/verbali/graduatorie/definitiva",json=data).status_code==200
        gid=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]["conferma"]["id"]
        draft=_post(client,base+"/graduatorie/approvazioni",json={"graduatorie_ids":[gid]})
        assert draft.status_code==200,draft.text
        approval_id=draft.json()["id"]
        assert _post(client,base+f"/graduatorie/approvazioni/{approval_id}/conferma",
                     json={"estremi_atto":"ATTO SINTETICO DI COLLAUDO"}).status_code==200
        endpoint=base+f"/graduatorie/approvazioni/{approval_id}/pdf"
        _assert_pdf_response(client.get(endpoint),"attachment")
        _assert_pdf_response(client.get(endpoint,params={"inline":"true"}),"inline")
