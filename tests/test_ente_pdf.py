"""Test sintetici della lettura PDF dei requisiti ente."""
from io import BytesIO
from reportlab.pdfgen import canvas
from test_pdf_import import _setup, _post


def sample_pdf():
    mem=BytesIO()
    doc=canvas.Canvas(mem,pagesize=(595,842))
    doc.drawString(40,750,"ALLEGATO UFFICIALE DI TEST")
    doc.drawString(40,700,"MODULO SINTETICO - AOUI")
    doc.drawString(40,675,"MODULO SINTETICO 2 - SSN")
    doc.showPage()
    doc.save()
    return mem.getvalue()


def test_pdf_ente_upload_returns_evidence_without_assigning_requirements(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        uri="/api/bandi/26-02/ente/bando-pdf"
        before=admin.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        assert all(not x["ente_richiesto"] for x in before)
        uploaded=_post(admin,uri,files={"file":("bando.pdf",sample_pdf(),"application/pdf")})
        assert uploaded.status_code==200,uploaded.text
        data=uploaded.json()
        assert data["pagine"]==1
        assert data["evidenze"]
        assert any("AOUI" in x["testo"] for x in data["evidenze"])
        after=admin.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        assert all(not x["ente_richiesto"] for x in after)
        assert (tmp_path/"sources"/"26-02"/"bando_requisiti_originale.pdf").is_file()


def test_pdf_ente_rejects_non_pdf(tmp_path,monkeypatch):
    _,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        r=_post(admin,"/api/bandi/26-02/ente/bando-pdf",
                files={"file":("bad.txt",b"not a pdf","text/plain")})
        assert r.status_code==400,r.text


def test_ai_opt_in_is_disabled_by_default_and_does_not_send_candidates(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        _post(admin,"/api/bandi/26-02/ente/bando-pdf",
              files={"file":("bando.pdf",sample_pdf(),"application/pdf")})
        endpoint="/api/bandi/26-02/ente/ai/anteprima"
        no_consent=_post(admin,endpoint,json={"consenso_documento_pubblico":False})
        assert no_consent.status_code==400,no_consent.text
        disabled=_post(admin,endpoint,json={"consenso_documento_pubblico":True})
        assert disabled.status_code==503,disabled.text
        import json
        (tmp_path/"options.json").write_text(json.dumps({
            "admin_username":"test-admin","admin_password":"this-is-a-long-test-password",
            "ai_enabled":True,"ai_api_key":"synthetic-test-key-123456"
        }),encoding="utf-8")
        captured={}
        def fake_ai(text,assignments,api_key):
            captured["text"]=text
            captured["assignments"]=assignments
            captured["api_key"]=api_key
            return [{"key":assignments[0]["key"],"ente_proposto":"AOUI",
                     "citazione":"MODULO SINTETICO - AOUI","stato":"DA CONFERMARE"}]
        monkeypatch.setattr(server,"ai_propose_ente",fake_ai)
        result=_post(admin,endpoint,json={"consenso_documento_pubblico":True})
        assert result.status_code==200,result.text
        assert result.json()["proposte"][0]["ente_proposto"]=="AOUI"
        assert "cognome" not in str(captured["assignments"])
        assert "id_pica" not in str(captured["assignments"])
        # L'AI non scrive requisiti nell'archivio, nemmeno quando propone un ente.
        rows=admin.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        assert all(not row["ente_richiesto"] for row in rows)
