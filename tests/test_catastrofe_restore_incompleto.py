"""Catastrofe: PICA incompleto dopo restore, PDF della Commissione immutabile.

Dati fittizi. La ricostruzione finale usa una TERZA istanza isolata:
non equivale ancora a una funzione di riparazione guidata del database.
"""
from hashlib import sha256
from io import BytesIO

from openpyxl import load_workbook
from pypdf import PdfReader

from test_import import pica_fixture
from test_pdf_import import _setup, _edit, _preview, _confirm


def _scheda_compilata_originale(tmp_path, monkeypatch):
    emissione = tmp_path / "emissione"
    emissione.mkdir()
    _, client, schede = _setup(emissione, monkeypatch)
    with client:
        pdf = next(
            data for data in schede.values()
            if str(PdfReader(BytesIO(data)).get_fields()["ID-DOMANDA"]["/V"]) == "1002"
        )
        assert "P19" not in PdfReader(BytesIO(pdf)).get_fields()
        compilato = _edit(pdf, {
            "P01": "2", "TOT": "2",
            "STATUS": "CANDIDATO AMMESSO IN GRADUATORIA",
        })
    return compilato


def _pica_senza_una_candidatura():
    """Il backup contiene il bando ma non la candidatura della scheda rientrata."""
    workbook = load_workbook(BytesIO(pica_fixture()))
    sheet = workbook.active
    assert str(sheet.cell(5, 1).value) == "1002"
    sheet.delete_rows(5)
    result = BytesIO()
    workbook.save(result)
    return result.getvalue()


def test_restore_incompleto_non_assegna_pdf_e_ne_conserva_i_byte(tmp_path, monkeypatch):
    compilato = _scheda_compilata_originale(tmp_path, monkeypatch)
    sha = sha256(compilato).hexdigest()
    directory = tmp_path / "restore_incompleto"
    directory.mkdir()
    server, client, _ = _setup(
        directory, monkeypatch,
        pica_data=_pica_senza_una_candidatura(),
        generate_sheets=False
    )
    with client:
        esito = _preview(client, [("commissione_compilato.pdf", compilato)])
        assert esito["riepilogo"]["da_verificare"] == 1, esito
        assert esito["schede"][0]["candidatura_id"] is None
        assert "Identificativo RankUp non riconosciuto" in esito["schede"][0]["motivo"]
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf").fetchone()[0] == 0

        # Il PDF non viene attribuito arbitrariamente; la conferma di
        # acquisizione tecnica deve custodire l'originale Da verificare.
        conservazione = _confirm(client, esito["token"])
        assert conservazione["acquisite"] == 0, conservazione
        assert conservazione["da_verificare"] == 1, conservazione
        with server.connect() as db:
            rows = db.execute(
                "SELECT candidatura_id,stato,sha256,percorso FROM valutazioni_pdf"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["candidatura_id"] is None
            assert rows[0]["stato"] == "Da verificare"
            assert rows[0]["sha256"] == sha
            assert (directory / rows[0]["percorso"]).read_bytes() == compilato
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_pdf_conservato_dal_guasto_rientra_con_pica_originale_ricostruito(
    tmp_path, monkeypatch
):
    compilato = _scheda_compilata_originale(tmp_path, monkeypatch)
    guasto = tmp_path / "restore_incompleto"
    guasto.mkdir()
    server_danneggiato, client_danneggiato, _ = _setup(
        guasto, monkeypatch,
        pica_data=_pica_senza_una_candidatura(),
        generate_sheets=False
    )
    with client_danneggiato:
        anteprima = _preview(client_danneggiato, [("scheda_da_salvare.pdf", compilato)])
        assert anteprima["riepilogo"]["da_verificare"] == 1, anteprima
        assert _confirm(client_danneggiato, anteprima["token"])["da_verificare"] == 1
        with server_danneggiato.connect() as db:
            row = db.execute("SELECT percorso FROM valutazioni_pdf").fetchone()
            pdf_da_conservazione = (guasto / row["percorso"]).read_bytes()
            assert pdf_da_conservazione == compilato

    # Ricostruzione completa in NUOVA istanza con PICA originale.
    # Nessun PDF viene generato durante il ripristino.
    completo = tmp_path / "ricostruito_completo"
    completo.mkdir()
    server, client, schede = _setup(completo, monkeypatch, generate_sheets=False)
    assert not schede
    with client:
        recupero = _preview(client, [("pdf_originale_immutato.pdf", pdf_da_conservazione)])
        assert recupero["riepilogo"]["regolare"] == 1, recupero
        assert recupero["schede"][0]["punteggio"] == 2
        acquisita = _confirm(client, recupero["token"])
        assert acquisita["acquisite"] == 1, acquisita
        with server.connect() as db:
            r = db.execute(
                "SELECT stato,sha256,percorso FROM valutazioni_pdf "
                "WHERE stato='Acquisita'"
            ).fetchone()
            assert r["sha256"] == sha256(compilato).hexdigest()
            assert (completo / r["percorso"]).read_bytes() == compilato
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
