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
        assert all(x["ente_richiesto"] == "NESSUNA RISERVA" for x in before)
        uploaded=_post(admin,uri,files={"file":("bando.pdf",sample_pdf(),"application/pdf")})
        assert uploaded.status_code==200,uploaded.text
        data=uploaded.json()
        assert data["pagine"]==1
        assert data["evidenze"]
        assert any("AOUI" in x["testo"] for x in data["evidenze"])
        after=admin.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        assert all(x["ente_richiesto"] == "NESSUNA RISERVA" for x in after)
        assert (tmp_path/"sources"/"26-02"/"bando_requisiti_originale.pdf").is_file()


def test_pdf_ente_rejects_non_pdf(tmp_path,monkeypatch):
    _,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        r=_post(admin,"/api/bandi/26-02/ente/bando-pdf",
                files={"file":("bad.txt",b"not a pdf","text/plain")})
        assert r.status_code==400,r.text



def test_ollama_does_not_process_bando_pdf(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        result=_post(admin,"/api/bandi/26-02/ente/ai/anteprima",json={})
        assert result.status_code==404
        rows=admin.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        assert all(row["ente_richiesto"]=="NESSUNA RISERVA" for row in rows)


def test_prepared_bando_can_receive_pdf_before_pica(tmp_path,monkeypatch):
    from test_users import _empty_setup
    from test_import import pica_fixture
    _,admin=_empty_setup(tmp_path,monkeypatch)
    with admin:
        created=_post(admin,"/api/bandi",json={"codice":"26-02"})
        assert created.status_code==200,created.text
        assert created.json()["stato"]=="In preparazione"
        saved=_post(admin,"/api/bandi/26-02/ente/bando-pdf",
                   files={"file":("bando.pdf",sample_pdf(),"application/pdf")})
        assert saved.status_code==200,saved.text
        preview=_post(admin,"/api/pica/preview",
            files={"file":("candidature.xlsx",pica_fixture(),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert preview.status_code==200,preview.text
        committed=_post(admin,"/api/pica/commit",json={"token":preview.json()["token"]})
        assert committed.status_code==200,committed.text
        rows=admin.get("/api/bandi/26-02/candidature").json()["candidature"]
        assert len(rows)==3
        assert admin.get("/api/bandi").json()["bandi"][0]["stato"]=="Candidature importate"
        assert (tmp_path/"sources"/"26-02"/"bando_requisiti_originale.pdf").is_file()
        duplicate=_post(admin,"/api/pica/preview",
            files={"file":("candidature.xlsx",pica_fixture(),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert duplicate.status_code==409,duplicate.text
