"""Recupero assistito dei PDF appiattiti: solo dati sintetici."""
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from test_pdf_import import _setup, _preview, _confirm, _post
from pdf_import import visible_rankup_ids


def _static_pdf(marker: str, *, score="2", outcome="CANDIDATO IDONEO") -> bytes:
    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=(595, 842))
    pdf.drawString(30, 800, "SCHEDA SINTETICA DI COLLAUDO NON UTILIZZABILE COME ATTO")
    pdf.drawString(30, 770, f"TOTALE: {score}")
    pdf.drawString(30, 745, outcome)
    if marker:
        pdf.drawString(30, 36, marker)
    pdf.showPage()
    pdf.save()
    return out.getvalue()


def _marker(doc):
    fields = PdfReader(BytesIO(doc)).get_fields() or {}
    return str(fields["RANKUP-ID"]["/V"])


def test_flattened_pdf_identified_only_as_pending_and_recovered_by_admin(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        original = next(d for d in sheets.values()
                        if "P19" not in PdfReader(BytesIO(d)).get_fields())
        marker = _marker(original)
        flat = _static_pdf(marker)
        assert not PdfReader(BytesIO(flat)).get_fields()
        assert visible_rankup_ids(flat) == [marker]
        preview = _preview(client, [("appiattita.pdf", flat)])
        item = preview["schede"][0]
        assert item["stato"] == "da_verificare", item
        assert item["id_pica"] == marker.split("-")[2]
        assert item["punteggio"] is None
        assert item["decisione"] == "da_verificare"
        assert "identificativo testuale riconosciuto" in item["motivo"]
        _confirm(client, preview["token"])
        record = client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
        assert record["candidatura_id"] == item["candidatura_id"]
        response = _post(client,
            f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
            json={"candidatura_id": record["candidatura_id"],
                  "esito": "CANDIDATO IDONEO",
                  "punteggio": "2", "nota_commissione": "",
                  "motivazione_tecnica": "PDF appiattito, esito e punteggio verificati visivamente.",
                  "documento_verificato": True})
        assert response.status_code == 200, response.text
        assert response.json()["punteggio"] == 2
        assert client.get(
            f"/api/bandi/26-02/valutazioni/{record['id']}/documento"
        ).content == flat
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf_eventi").fetchone()[0] == 1


def test_flattened_pdf_unknown_id_is_not_accepted_manually(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        original = next(d for d in sheets.values()
                        if "P19" not in PdfReader(BytesIO(d)).get_fields())
        marker = _marker(original)
        flat = _static_pdf(marker[:-10] + "0000000000")
        preview = _preview(client, [("id_sbagliato.pdf", flat)])
        item = preview["schede"][0]
        assert item["stato"] == "da_verificare"
        assert item["candidatura_id"] is None
        _confirm(client, preview["token"])
        record = client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
        with server.connect() as db:
            candidate_id = db.execute("SELECT id FROM candidature WHERE stato='Attiva' LIMIT 1").fetchone()[0]
        response = _post(client,
            f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
            json={"candidatura_id": candidate_id,
                  "esito": "CANDIDATO IDONEO",
                  "punteggio": "2", "nota_commissione": "",
                  "motivazione_tecnica": "Documento volutamente alterato per collaudo.",
                  "documento_verificato": True})
        assert response.status_code == 409, response.text
        assert "RANKUP-ID" in response.text
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf WHERE stato='Acquisita'").fetchone()[0] == 0


def test_flattened_pdf_without_visible_id_cannot_be_approved(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        flat = _static_pdf("")
        preview = _preview(client, [("id_assente.pdf", flat)])
        assert preview["schede"][0]["candidatura_id"] is None
        _confirm(client, preview["token"])
        record = client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
        with server.connect() as db:
            candidate_id = db.execute("SELECT id FROM candidature WHERE stato='Attiva' LIMIT 1").fetchone()[0]
        response = _post(client,
            f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
            json={"candidatura_id": candidate_id,
                  "esito": "CANDIDATO IDONEO",
                  "punteggio": "2", "nota_commissione": "",
                  "motivazione_tecnica": "Documento senza identificativo per collaudo.",
                  "documento_verificato": True})
        assert response.status_code == 409, response.text
