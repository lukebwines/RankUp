"""La verifica istruttoria deve risultare sulle schede, senza esiti automatici."""
from io import BytesIO
import zipfile
from pypdf import PdfReader
from test_pdf_import import _setup


def test_pdf_occupation_warning_for_reserved_assignments(tmp_path, monkeypatch):
    server, client, originals = _setup(tmp_path, monkeypatch)
    with client:
        assert all(
            "ATTENZIONE - Verificare occupazione" not in
            (PdfReader(BytesIO(value)).pages[0].extract_text() or "")
            for value in originals.values()
        ), "Nessuna riserva: non va introdotta una segnalazione di impiego"

        # Simulazione deliberatamente fittizia di rettifica del dizionario.
        changed = client.put("/api/pica/tipologie",
                             headers={"X-RankUp-Request":"1"},
                             json={"chiave":"contratto esterno oneroso CSA",
                                   "requisito":"SSN",
                                   "motivo":"Rettifica sintetica di collaudo"})
        assert changed.status_code == 200, changed.text

        # La scheda viene rigenerata, senza modificare il PDF storico.
        response = client.get("/api/bandi/26-02/schede/zip")
        assert response.status_code == 200, response.text
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            sheets = [archive.read(name) for name in archive.namelist()
                      if name.endswith(".pdf") and not name.startswith("VB_")]
        assert len(sheets) == 3
        for data in sheets:
            pdf = PdfReader(BytesIO(data))
            text = pdf.pages[0].extract_text() or ""
            assert "Tipologia di incarico:" in text
            fields = pdf.get_fields() or {}
            assert fields["TIPO-CDS-PICA"].get("/V") == "contratto esterno oneroso CSA"
            assert fields["REQUISITI"].get("/V")
            assert "ATTENZIONE - Verificare occupazione e appartenenza all'ente (SSN)" in text
            assert "NOTE-COMM" in (pdf.get_fields() or {})
            assert "STATUS" in (pdf.get_fields() or {})
            assert (pdf.get_fields() or {})["STATUS"].get("/V") in ("-", "-- Seleziona --")

        # Nessun provvedimento o stato candidato viene cambiato dall'avviso.
        assert all(x["stato"] == "Attiva" for x in
                   client.get("/api/bandi/26-02/candidature").json()["candidature"])
