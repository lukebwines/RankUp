"""Test di compatibilità delle schede PDF RankUP.

Queste prove usano esclusivamente candidature sintetiche e risalvataggi
programmati con pypdf. NON attestano la compatibilità di Anteprima macOS,
Adobe Acrobat o altri programmi reali: servono PDF campione prodotti con
quei programmi per il collaudo successivo.
"""
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject

from test_pdf_import import _setup, _edit, _preview


def _ordinary(sheets):
    return next(doc for doc in sheets.values()
                if "P19" not in PdfReader(BytesIO(doc)).get_fields())


def _filled(doc):
    return _edit(doc, {
        "P01": "2", "TOT": "2",
        "STATUS": "CANDIDATO IDONEO",
        "NOTE-COMM": "Valutazione sintetica di collaudo",
    })


def _resave(doc, *, remove_metadata=False):
    """Riscrive la struttura PDF preservando i campi del modulo."""
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(doc)))
    if remove_metadata:
        writer.add_metadata({"/Producer": "RankUP synthetic PDF compatibility test"})
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_roundtrip_resave_preserves_identity_scores_and_decision(tmp_path, monkeypatch):
    _, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        original = _filled(_ordinary(sheets))
        control = _preview(client, [("originale.pdf", original)])["schede"][0]
        assert control["stato"] == "regolare", control
        assert control["punteggio"] == 2
        assert control["decisione"] == "idoneo"
        for name, document in (
            ("risalvato.pdf", _resave(original)),
            ("metadati-riscritti.pdf", _resave(original, remove_metadata=True)),
        ):
            sheet = _preview(client, [(name, document)])["schede"][0]
            assert sheet["stato"] == "regolare", (name, sheet)
            for key in ("candidatura_id", "id_pica", "modello", "punteggio",
                        "punteggi", "esito", "decisione", "note", "totale_pdf"):
                assert sheet[key] == control[key], (name, key, sheet, control)


def test_missing_identifier_is_not_silently_imported(tmp_path, monkeypatch):
    _, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        damaged = _edit(_filled(_ordinary(sheets)), {"RANKUP-ID": ""})
        sheet = _preview(client, [("identificativo-perso.pdf", damaged)])["schede"][0]
        assert sheet["stato"] == "da_verificare", sheet
        assert sheet["candidatura_id"] is None
        assert "Identificativo RankUp" in sheet["motivo"]


def test_missing_commission_score_is_not_silently_accepted(tmp_path, monkeypatch):
    _, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        damaged = _edit(_filled(_ordinary(sheets)), {"P01": ""})
        sheet = _preview(client, [("punteggio-perso.pdf", damaged)])["schede"][0]
        assert sheet["stato"] == "da_verificare", sheet
        assert sheet["punteggio"] is None


def test_inconsistent_saved_total_is_not_silently_accepted(tmp_path, monkeypatch):
    _, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        damaged = _edit(_filled(_ordinary(sheets)), {"TOT": "0"})
        sheet = _preview(client, [("totale-non-aggiornato.pdf", damaged)])["schede"][0]
        assert sheet["stato"] == "da_verificare", sheet
        assert "Totale PDF non aggiornato" in sheet["motivo"]


def test_corrupted_required_field_is_not_silently_accepted(tmp_path, monkeypatch):
    _, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        damaged = _edit(_filled(_ordinary(sheets)), {"ID-DOMANDA": "PICA-ALTERATO"})
        sheet = _preview(client, [("candidatura-discordante.pdf", damaged)])["schede"][0]
        assert sheet["stato"] == "da_verificare", sheet
        assert "Dati della candidatura discordanti" in sheet["motivo"]
