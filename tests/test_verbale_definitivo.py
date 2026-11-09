"""Verbale definitivo = documento statico e chiusura atomica della procedura.

Tutti i dati sono sintetici e non provengono da atti amministrativi reali.
"""
import hashlib
from io import BytesIO
from reportlab.pdfgen import canvas
from pypdf import PdfReader

from pdf_definitivo import appiattisci_scheda, verbale_definitivo
from test_pdf_import import _setup, _post
from test_graduatorie import _complete_all


def _reportlab_form():
    target=BytesIO()
    doc=canvas.Canvas(target)
    doc.drawString(35,800,"SCHEDA DI COLLAUDO")
    doc.acroForm.textfield(name="P01",tooltip="Punteggio",
                            x=50,y=710,width=200,height=22,
                            value="14.50",forceBorder=True)
    doc.acroForm.textfield(name="NOTE-COMM",tooltip="Nota",
                            x=50,y=660,width=250,height=22,
                            value="VALUTAZIONE SINTETICA",forceBorder=True)
    doc.save()
    return target.getvalue()


def test_flatten_retains_visible_text_and_removes_widgets():
    original=_reportlab_form()
    assert "P01" in PdfReader(BytesIO(original)).get_fields()
    flat=appiattisci_scheda(original)
    reader=PdfReader(BytesIO(flat))
    assert reader.get_fields() is None
    assert "14.50" in reader.pages[0].extract_text()
    assert "VALUTAZIONE SINTETICA" in reader.pages[0].extract_text()
    assert not (reader.pages[0].get("/Annots") or [])
    assert "P01" in PdfReader(BytesIO(original)).get_fields()
    # Stesso nome di campo in più schede: nessuna collisione di AcroForm.
    joined=verbale_definitivo(flat,[original,original])
    doc=PdfReader(BytesIO(joined))
    assert len(doc.pages)==3
    assert doc.get_fields() is None
    assert sum("14.50" in (page.extract_text() or "") for page in doc.pages)==3


def test_generate_final_document_persists_bytes_without_changing_originals(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        _complete_all(client,sheets)
        base="/api/bandi/26-02"
        g=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        data={"cds":g["cds"],"sede":g["sede"]}
        with server.connect() as db:
            paths=[row["percorso"] for row in db.execute("""
                SELECT percorso FROM valutazioni_pdf WHERE stato='Acquisita'
            """).fetchall()]
        originals={p:(server.BASE/p).read_bytes() for p in paths}
        assert all(PdfReader(BytesIO(doc)).get_fields() for doc in originals.values())
        not_yet=client.get(base+"/verbali/graduatorie/pdf-completo",params=data)
        assert not_yet.status_code==409
        finalized=_post(client,base+"/verbali/graduatorie/genera-definitivo",json=data)
        assert finalized.status_code==200,finalized.text
        assert finalized.json()["stato"]=="definitiva"
        downloaded=client.get(base+"/verbali/graduatorie/pdf-completo",params=data)
        assert downloaded.status_code==200,downloaded.text
        pdf=PdfReader(BytesIO(downloaded.content))
        assert pdf.get_fields() is None
        assert all(not any(str(a.get_object().get("/Subtype",""))=="/Widget"
                   for a in (p.get("/Annots") or [])) for p in pdf.pages)
        assert "Nota di valutazione della commissione" in " ".join(
            p.extract_text() or "" for p in pdf.pages)
        with server.connect() as db:
            state=db.execute("SELECT stato,id FROM graduatorie_cds").fetchone()
            assert state["stato"]=="definitiva"
            record=db.execute("SELECT pdf_definitivo,sha256 FROM graduatorie_cds_documenti WHERE graduatoria_id=?",
                              (state["id"],)).fetchone()
            assert record["pdf_definitivo"]==downloaded.content
            assert record["sha256"]==hashlib.sha256(downloaded.content).hexdigest()
        assert client.get(base+"/verbali/graduatorie/pdf-completo",params=data).content==downloaded.content
        assert all((server.BASE/p).read_bytes()==payload for p,payload in originals.items())
        assert _post(client,base+"/verbali/graduatorie/genera-definitivo",json=data).status_code==409
        reopened=_post(client,base+"/verbali/graduatorie/regredisci-stato",
            json={**data,"motivazione":"Riapertura motivata nel collaudo"})
        assert reopened.status_code==200,reopened.text
        archived=client.get(base+"/verbali/graduatorie/revisioni/"+
            str(reopened.json()["revisione_id"])+"/pdf")
        assert archived.status_code==200
        assert archived.content==downloaded.content


def test_failed_document_creation_does_not_close_graduatoria(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        _complete_all(client,sheets)
        base="/api/bandi/26-02"
        g=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        data={"cds":g["cds"],"sede":g["sede"]}
        with server.connect() as db:
            path=db.execute("""SELECT percorso FROM valutazioni_pdf
                               WHERE stato='Acquisita' LIMIT 1""").fetchone()["percorso"]
        src=server.BASE/path
        saved=src.read_bytes()
        try:
            src.unlink()
            failed=_post(client,base+"/verbali/graduatorie/genera-definitivo",json=data)
            assert failed.status_code==409,failed.text
            assert client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]["stato"]=="pronta"
            with server.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM graduatorie_cds").fetchone()[0]==0
                assert db.execute("SELECT COUNT(*) FROM graduatorie_cds_documenti").fetchone()[0]==0
        finally:
            src.write_bytes(saved)
