"""Catastrofe: PDF gia' distribuito e compilato importabile dopo restore.

Dati PICA e schede esclusivamente sintetici. Le due directory sono
istanze separate; non si collegano mai al database in produzione.
"""
from hashlib import sha256
from io import BytesIO

from pypdf import PdfReader

from test_pdf_import import _setup, _edit, _preview, _confirm


def test_scheda_gia_compilata_importabile_dopo_ripristino_pre_emissione(tmp_path, monkeypatch):
    emissione = tmp_path / "emissione"
    ripristinata = tmp_path / "ripristinata"
    emissione.mkdir()
    ripristinata.mkdir()

    # FASE 1: la Commissione riceve e compila una scheda PDF originale.
    _, client_emissione, originali = _setup(emissione, monkeypatch)
    with client_emissione:
        ordinaria = next(
            data for data in originali.values()
            if "P19" not in PdfReader(BytesIO(data)).get_fields()
        )
        pdf_compilato = _edit(ordinaria, {
            "P01": "2",
            "TOT": "2",
            "STATUS": "CANDIDATO AMMESSO IN GRADUATORIA",
        })
        sha_originale = sha256(pdf_compilato).hexdigest()
        identificativo = str(PdfReader(BytesIO(pdf_compilato)).get_fields()["RANKUP-ID"]["/V"])

    # FASE 2: un ripristino antecedente all'emissione fa perdere
    # le tracce di generazione, ma ricostruisce lo stesso PICA originale.
    server, client, _ = _setup(ripristinata, monkeypatch)
    with client:
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf").fetchone()[0] == 0

        # FASE 3: importiamo ESATTAMENTE i bytes gia' restituiti dalla
        # Commissione; vietato rigenerare o modificare la scheda.
        risultato = _preview(client, [("compilata_prima_del_guasto.pdf", pdf_compilato)])
        assert risultato["riepilogo"]["regolare"] == 1, risultato
        scheda = risultato["schede"][0]
        assert scheda["stato"] == "regolare", scheda
        assert scheda["punteggio"] == 2, scheda
        acquisizione = _confirm(client, risultato["token"])
        assert acquisizione["acquisite"] == 1, acquisizione

        with server.connect() as db:
            registrate = db.execute(
                "SELECT percorso,sha256 FROM valutazioni_pdf "
                "WHERE stato='Acquisita' AND attiva=1"
            ).fetchall()
            assert len(registrate) == 1
            assert registrate[0]["sha256"] == sha_originale
            assert (ripristinata / registrate[0]["percorso"]).read_bytes() == pdf_compilato
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        # Non deve essere possibile acquisire due volte lo stesso documento.
        duplicato = _preview(client, [("identica_compilata.pdf", pdf_compilato)])
        assert duplicato["riepilogo"]["duplicato"] == 1, duplicato
        assert identificativo


def test_pdf_con_identificativo_discordante_dopo_restore_non_e_associato(tmp_path, monkeypatch):
    emissione = tmp_path / "emissione"
    ripristinata = tmp_path / "ripristinata"
    emissione.mkdir()
    ripristinata.mkdir()
    _, client_emissione, originali = _setup(emissione, monkeypatch)
    with client_emissione:
        ordinaria = next(
            data for data in originali.values()
            if "P19" not in PdfReader(BytesIO(data)).get_fields()
        )
        campi = PdfReader(BytesIO(ordinaria)).get_fields()
        identificativo = str(campi["RANKUP-ID"]["/V"])
        alterato = _edit(ordinaria, {
            "RANKUP-ID": identificativo + "-NON-CORRISPONDE",
            "P01": "2",
            "TOT": "2",
            "STATUS": "CANDIDATO AMMESSO IN GRADUATORIA",
        })

    server, client, _ = _setup(ripristinata, monkeypatch)
    with client:
        risultato = _preview(client, [("identificativo_non_valido.pdf", alterato)])
        assert risultato["riepilogo"]["da_verificare"] == 1, risultato
        assert "Identificativo RankUp non riconosciuto" in risultato["schede"][0]["motivo"]
        assert risultato["schede"][0]["candidatura_id"] is None
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf").fetchone()[0] == 0
