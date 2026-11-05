"""Collaudo sintetico della lettura senza salvataggio e della conferma.

I dati sono fittizi; nessun nominativo o dato di candidature reali.
"""
from io import BytesIO
import importlib
import json
import zipfile
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject
from fastapi.testclient import TestClient

from test_import import pica_fixture, commissioni_fixture


def _post(client,path,**kwargs):
    return client.post(path,headers={"X-RankUp-Request":"1"},**kwargs)


def _edit(data: bytes, updates: dict[str,str]) -> bytes:
    writer=PdfWriter()
    writer.append(PdfReader(BytesIO(data)))
    fields=writer._root_object["/AcroForm"].get_object()["/Fields"]
    changed=set()
    for ref in fields:
        field=ref.get_object()
        name=str(field.get("/T",""))
        if name in updates:
            field[NameObject("/V")]=TextStringObject(str(updates[name]))
            changed.add(name)
    assert changed==set(updates), f"Campi mancanti: {set(updates)-changed}"
    out=BytesIO()
    writer.write(out)
    return out.getvalue()


def _setup(tmp_path,monkeypatch):
    monkeypatch.setenv("RANKUP_DATA_DIR",str(tmp_path))
    (tmp_path/"options.json").write_text(json.dumps({
        "admin_username":"test-admin",
        "admin_password":"PasswordFittiziaLunga12345!"
    }))
    import server
    importlib.reload(server)
    client=TestClient(server.app)
    login=_post(client,"/api/auth/login",
        json={"username":"test-admin","password":"PasswordFittiziaLunga12345!"})
    assert login.status_code==200,login.text
    preview=_post(client,"/api/pica/preview",
        files={"file":("test.xlsx",pica_fixture(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert preview.status_code==200,preview.text
    commit=_post(client,"/api/pica/commit",json={"token":preview.json()["token"]})
    assert commit.status_code==200,commit.text
    commissions=_post(client,"/api/bandi/26-02/commissioni/import",
        files={"file":("commissioni.xlsx",commissioni_fixture(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert commissions.status_code==200,commissions.text
    model=_post(client,"/api/ssd/MEDS-24%2FC/classificazione",
        json={"tipo":"professionalizzante"})
    assert model.status_code==200,model.text
    response=client.get("/api/bandi/26-02/schede/zip")
    assert response.status_code==200,response.text
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        sheets={name:archive.read(name) for name in archive.namelist()}
    assert len(sheets)==3
    return server,client,sheets


def _preview(client,pdfs):
    files=[("files",(name,contents,"application/pdf"))
           for name,contents in pdfs]
    response=_post(client,"/api/bandi/26-02/valutazioni/anteprima",files=files)
    assert response.status_code==200,response.text
    return response.json()


def _confirm(client,token,revision=False,expected=200):
    response=_post(client,"/api/bandi/26-02/valutazioni/conferma",
        json={"token":token,"conferma_rettifiche":revision})
    assert response.status_code==expected,response.text
    return response.json()


def test_preview_does_not_update_database_and_confirm_preserves_anomalies(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        originals=list(sheets.values())
        ordinary=[doc for doc in originals if "P19" not in PdfReader(BytesIO(doc)).get_fields()]
        assert len(ordinary)==2
        regular=_edit(ordinary[0],{"P01":"2","TOT":"2"})
        anomalous=ordinary[1]  # Tutti zero senza esito e nota
        result=_preview(client,[("regolare.pdf",regular),("incompleta.pdf",anomalous)])
        assert result["riepilogo"]["regolare"]==1
        assert result["riepilogo"]["da_verificare"]==1
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf").fetchone()[0]==0
        committed=_confirm(client,result["token"])
        assert committed["acquisite"]==1
        assert committed["da_verificare"]==1
        status=client.get("/api/bandi/26-02/valutazioni").json()
        assert status["totale_attese"]==3
        assert status["totale_acquisite"]==1
        assert not status["cds_sedi"][0]["complete"]
        with server.connect() as db:
            values=db.execute(
                "SELECT stato,attiva,totale_calcolato,percorso FROM valutazioni_pdf ORDER BY id"
            ).fetchall()
            assert [v["stato"] for v in values]==["Acquisita","Da verificare"]
            assert values[0]["totale_calcolato"]==2
            assert values[1]["totale_calcolato"] is None
            assert (tmp_path/values[0]["percorso"]).is_file()
            assert (tmp_path/values[1]["percorso"]).is_file()
        duplicate=_preview(client,[("regolare.pdf",regular),("incompleta.pdf",anomalous)])
        assert duplicate["riepilogo"]["duplicato"]==2


def test_total_mismatch_requires_corrected_pdf(tmp_path,monkeypatch):
    _,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        ordinary=next(data for data in sheets.values()
                      if "P19" not in PdfReader(BytesIO(data)).get_fields())
        wrong=_edit(ordinary,{"P01":"2","TOT":"0"})
        result=_preview(client,[("errata.pdf",wrong)])
        assert result["riepilogo"]["da_verificare"]==1
        assert "Totale PDF non aggiornato" in result["schede"][0]["motivo"]
        proper=_edit(ordinary,{"P01":"2","TOT":"2"})
        corrected=_preview(client,[("corretta.pdf",proper)])
        assert corrected["riepilogo"]["regolare"]==1
        assert corrected["schede"][0]["punteggio"]==2


def test_professional_activity_values_are_numeric_without_tracking_labels(tmp_path,monkeypatch):
    _,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        pdf=next(data for data in sheets.values()
                 if "P19" in PdfReader(BytesIO(data)).get_fields())
        updated=_edit(pdf,{"attivita0":"1.5","plus0":"0.5",
                           "extraField0":"2","TOT":"2"})
        result=_preview(client,[("professional.pdf",updated)])
        assert result["riepilogo"]["regolare"]==1
        assert result["schede"][0]["punteggio"]==2
        assert not result["schede"][0]["motivo"].startswith("Subtotale")


def test_revision_requires_explicit_consent_and_preserves_old_version(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        ordinary=next(data for data in sheets.values()
                      if "P19" not in PdfReader(BytesIO(data)).get_fields())
        initial=_edit(ordinary,{"P01":"2","TOT":"2"})
        first=_preview(client,[("prima.pdf",initial)])
        _confirm(client,first["token"])
        new=_edit(ordinary,{"P02":"2","TOT":"2"})
        correction=_preview(client,[("nuova.pdf",new)])
        assert correction["riepilogo"]["rettifica"]==1
        _confirm(client,correction["token"],expected=409)
        renewed=_preview(client,[("nuova.pdf",new)])
        result=_confirm(client,renewed["token"],revision=True)
        assert result["rettificate"]==1
        with server.connect() as db:
            records=db.execute(
                "SELECT versione,attiva FROM valutazioni_pdf WHERE stato='Acquisita' ORDER BY versione"
            ).fetchall()
            assert [(r["versione"],r["attiva"]) for r in records]==[(1,0),(2,1)]


def test_unknown_candidacy_and_tampered_identity_never_change_scores(tmp_path,monkeypatch):
    _,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        ordinary=next(data for data in sheets.values()
                      if "P19" not in PdfReader(BytesIO(data)).get_fields())
        tampered=_edit(ordinary,{"ID-DOMANDA":"ID SBAGLIATO","P01":"2","TOT":"2"})
        preview=_preview(client,[("tampered.pdf",tampered)])
        assert preview["riepilogo"]["da_verificare"]==1
        assert "discordanti" in preview["schede"][0]["motivo"]
        assert client.get("/api/bandi/26-02/valutazioni").json()["totale_acquisite"]==0
