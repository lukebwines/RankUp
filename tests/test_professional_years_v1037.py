"""Controlli sintetici: quinquennio professionale automatico, PDF non alterato."""
from io import BytesIO
import zipfile

from pypdf import PdfReader

from professional import _anni_attivita_professionale, make_professional
from test_pdf_import import _setup


def _pdf(aa):
    return PdfReader(BytesIO(make_professional({
        "anno": aa,
        "bando": "TEST-PDF",
        "id_pica": "DOMANDA-FITTIZIA",
        "cognome": "ESEMPIO",
        "nome": "TEST",
        "codice_fiscale": "",
        "cds": "Corso sintetico",
        "insegnamento": "Modulo sintetico",
        "sede": "Sede sintetica",
        "modulo": "Attivita sintetica",
        "ul": "",
        "requisiti": "[DA VERIFICARE]",
        "tipo_cds": "[DA VERIFICARE]",
        "scheda_id": "RU-TEST-PDF-SINTETICO",
    }, "professionalizzante")))


def _rects(pdf):
    return {
        name: tuple(float(n) for n in field["/Rect"])
        for page in pdf.pages
        for field in page["/Annots"]
        for name in [str(field.get_object().get("/T", ""))]
        if name
    }


def test_quinquennio_from_selected_academic_year():
    assert _anni_attivita_professionale("2026-2027") == [
        "2025", "2024", "2023", "2022", "2021"]
    assert _anni_attivita_professionale("2027-2028") == [
        "2026", "2025", "2024", "2023", "2022"]
    assert _anni_attivita_professionale("2028-2029") == [
        "2027", "2026", "2025", "2024", "2023"]
    assert _anni_attivita_professionale("2027/2028") == [
        "2026", "2025", "2024", "2023", "2022"]
    for invalid in ("", "2027-2029", "2027", "[DA VERIFICARE]", "2027-0000"):
        assert _anni_attivita_professionale(invalid) == ["[DA VER.]"] * 5


def test_pdf_years_change_but_layout_fields_and_scores_do_not():
    old = _pdf("2026-2027")
    new = _pdf("2027-2028")
    assert len(old.pages) == len(new.pages) == 1
    assert tuple(old.pages[0].mediabox) == tuple(new.pages[0].mediabox)
    assert _rects(old) == _rects(new)
    assert set(old.get_fields()) == set(new.get_fields())
    assert "TOT" in new.get_fields()
    assert "STATUS" in new.get_fields()
    assert all(f"extraField{i}" in new.get_fields() for i in range(5))
    old_text = old.pages[0].extract_text()
    new_text = new.pages[0].extract_text()
    for year in range(2021, 2026):
        assert f"Anno {year}" in old_text
    for year in range(2022, 2027):
        assert f"Anno {year}" in new_text
    assert "Anno 2021" not in new_text
    assert "Anno 2026" not in old_text


def test_pdf_generation_uses_academic_year_saved_in_bando(tmp_path, monkeypatch):
    _, client, _ = _setup(tmp_path, monkeypatch)
    with client:
        current = client.get("/api/bandi/26-02/riferimenti")
        assert current.status_code == 200
        changed = client.put("/api/bandi/26-02/riferimenti", headers={
            "X-RankUp-Request": "1"
        }, json={
            "anno_accademico": "2027-2028",
            "expected_updated_at": current.json()["riferimenti"]["aggiornato_il"],
        })
        assert changed.status_code == 200, changed.text
        zip_response = client.get("/api/bandi/26-02/schede/zip")
        assert zip_response.status_code == 200, zip_response.text
        with zipfile.ZipFile(BytesIO(zip_response.content)) as archive:
            pdfs = [PdfReader(BytesIO(archive.read(name)))
                    for name in archive.namelist()
                    if name.endswith(".pdf") and not name.startswith("VB_")]
        prof = [pdf for pdf in pdfs if "P19" in pdf.get_fields()]
        assert len(prof) == 1
        assert len(prof[0].pages) == 1
        text = prof[0].pages[0].extract_text()
        assert "Anno 2026" in text
        assert "Anno 2022" in text
        assert "Anno 2021" not in text
