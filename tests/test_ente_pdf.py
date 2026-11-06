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
