"""Cinque esiti per tre modelli, punteggio degli idonei e compatibilità storica."""
from io import BytesIO
from pypdf import PdfReader
from test_pdf_import import _setup, _edit, _preview


CURRENT = (
    "CANDIDATO IDONEO",
    "CANDIDATO NON IDONEO - PROFILO PROFESSIONALE NON COERENTE",
    "CANDIDATO NON AMMISSIBILE - REQUISITO ENTE NON SODDISFATTO",
    "CANDIDATO NON AMMISSIBILE - REQUISITI GENERALI NON SODDISFATTI",
    "CANDIDATO NON IDONEO - VALUTAZIONE NEGATIVA DELLA COMMISSIONE",
)


def _status_options(data):
    options = PdfReader(BytesIO(data)).get_fields()["STATUS"].get("/Opt") or []
    return tuple(str(option[-1] if isinstance(option,list) else option)
                 for option in options)


def test_five_statuses_in_ordinary_and_professional(tmp_path,monkeypatch):
    _, client, sheets = _setup(tmp_path,monkeypatch)
    with client:
        kinds=set()
        for data in sheets.values():
            fields=PdfReader(BytesIO(data)).get_fields()
            kinds.add("professionalizzante" if "P19" in fields else "ordinario")
            assert _status_options(data)[1:]==CURRENT
            assert "CANDIDATO UNICO IDONEO" not in _status_options(data)
        assert kinds=={"ordinario","professionalizzante"}


def test_five_statuses_in_english(tmp_path,monkeypatch):
    _, client, _ = _setup(tmp_path,monkeypatch)
    with client:
        response=client.put("/api/ssd/MEDS-24%2FC/classificazione",
            headers={"X-RankUp-Request":"1"},json={"tipo":"inglese"})
        assert response.status_code==200,response.text
        import zipfile
        response=client.get("/api/bandi/26-02/schede/zip")
        assert response.status_code==200
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            forms=[archive.read(name) for name in archive.namelist() if name.endswith(".pdf")]
        english=[data for data in forms
                 if "RANKUP-MODEL" in (PdfReader(BytesIO(data)).get_fields() or {})]
        assert english
        assert _status_options(english[0])[1:]==CURRENT


def test_new_positive_requires_points_and_negative_note(tmp_path,monkeypatch):
    _,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        ordinary=next(data for data in sheets.values()
                      if "P19" not in PdfReader(BytesIO(data)).get_fields())
        valid=_edit(ordinary,{"P01":"2","TOT":"2","STATUS":CURRENT[0]})
        ok=_preview(client,[("idoneo.pdf",valid)])
        assert ok["riepilogo"]["regolare"]==1,ok
        assert ok["schede"][0]["decisione"]=="idoneo"
        assert ok["schede"][0]["punteggio"]==2
        missing=_edit(ordinary,{"P01":"","STATUS":CURRENT[0]})
        issue=_preview(client,[("senza_punteggi.pdf",missing)])
        assert issue["riepilogo"]["da_verificare"]==1,issue
        for status,decision in ((CURRENT[1],"non_idoneo"),(CURRENT[2],"escluso"),
                                (CURRENT[3],"escluso"),(CURRENT[4],"non_idoneo")):
            negative=_edit(ordinary,{"STATUS":status,
                "NOTE-COMM":"Decisione motivata della commissione (dato sintetico di prova)."})
            accepted=_preview(client,[("esito.pdf",negative)])
            assert accepted["riepilogo"]["regolare"]==1,accepted
            assert accepted["schede"][0]["decisione"]==decision
            assert accepted["schede"][0]["punteggio"] is None
            unmotivated=_edit(ordinary,{"STATUS":status})
            pending=_preview(client,[("esito_senza_motivo.pdf",unmotivated)])
            assert pending["riepilogo"]["da_verificare"]==1,pending


def test_legacy_statuses_remain_readable(tmp_path,monkeypatch):
    _,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        ordinary=next(data for data in sheets.values()
                      if "P19" not in PdfReader(BytesIO(data)).get_fields())
        historic=_edit(ordinary,{"P01":"2","TOT":"2",
                                 "STATUS":"CANDIDATO UNICO IDONEO"})
        old=_preview(client,[("storica.pdf",historic)])
        assert old["riepilogo"]["regolare"]==1,old
        assert old["schede"][0]["decisione"]=="idoneo"
