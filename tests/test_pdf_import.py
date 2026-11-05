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
    model=client.put("/api/ssd/MEDS-24%2FC/classificazione",
        headers={"X-RankUp-Request":"1"},
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
        regular=_edit(ordinary[0],{"P01":"2","TOT":"2","STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        anomalous=ordinary[1]  # Tutti zero senza esito e nota
        result=_preview(client,[("regolare.pdf",regular),("incompleta.pdf",anomalous)])
        assert result["riepilogo"]["regolare"]==1, result["schede"]
        assert result["riepilogo"]["da_verificare"]==1, result["schede"]
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
            ids=[r["id"] for r in db.execute("SELECT id FROM valutazioni_pdf ORDER BY id")]
        for index,original in zip(ids,(regular,anomalous)):
            downloaded=client.get("/api/bandi/26-02/valutazioni/"+str(index)+"/documento")
            assert downloaded.status_code==200
            assert downloaded.content==original
        duplicate=_preview(client,[("regolare.pdf",regular),("incompleta.pdf",anomalous)])
        assert duplicate["riepilogo"]["duplicato"]==2


def test_total_mismatch_requires_corrected_pdf(tmp_path,monkeypatch):
    _,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        ordinary=next(data for data in sheets.values()
                      if "P19" not in PdfReader(BytesIO(data)).get_fields())
        wrong=_edit(ordinary,{"P01":"2","TOT":"0","STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        result=_preview(client,[("errata.pdf",wrong)])
        assert result["riepilogo"]["da_verificare"]==1
        assert "Totale PDF non aggiornato" in result["schede"][0]["motivo"]
        proper=_edit(ordinary,{"P01":"2","TOT":"2","STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        corrected=_preview(client,[("corretta.pdf",proper)])
        assert corrected["riepilogo"]["regolare"]==1
        assert corrected["schede"][0]["punteggio"]==2


def test_professional_activity_values_are_numeric_without_tracking_labels(tmp_path,monkeypatch):
    _,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        pdf=next(data for data in sheets.values()
                 if "P19" in PdfReader(BytesIO(data)).get_fields())
        updated=_edit(pdf,{"attivita0":"1.5","plus0":"0.5",
                           "extraField0":"2","TOT":"2","STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        result=_preview(client,[("professional.pdf",updated)])
        assert result["riepilogo"]["regolare"]==1
        assert result["schede"][0]["punteggio"]==2
        assert not result["schede"][0]["motivo"].startswith("Subtotale")


def test_revision_requires_explicit_consent_and_preserves_old_version(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        ordinary=next(data for data in sheets.values()
                      if "P19" not in PdfReader(BytesIO(data)).get_fields())
        initial=_edit(ordinary,{"P01":"2","TOT":"2","STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        first=_preview(client,[("prima.pdf",initial)])
        _confirm(client,first["token"])
        new=_edit(ordinary,{"P02":"2","TOT":"2","STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
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


def test_zip_with_21_blank_pdfs_reports_all_21_in_preview(tmp_path, monkeypatch):
    """Un PDF vergine per ciascuna voce ZIP deve comparire nel risultato.

    Usa soltanto schede sintetiche dei fixture, senza file dei candidati reali.
    """
    _, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        original = list(sheets.values())
        archive_bytes = BytesIO()
        with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
            for n in range(21):
                archive.writestr(
                    f"OSTEOPATIA/VERONA/candidato_sintetico_{n + 1:02d}.pdf",
                    original[n % len(original)]
                )
            archive.writestr("__MACOSX/OSTEOPATIA/VERONA/._candidato_sintetico_01.pdf",
                             b"Finder resource-fork metadata")
            archive.writestr("OSTEOPATIA/VERONA/.DS_Store",b"Finder metadata")
        result = _post(client, "/api/bandi/26-02/valutazioni/anteprima",
            files={"files": ("schede_21.zip", archive_bytes.getvalue(), "application/zip")})
        assert result.status_code == 200, result.text
        body = result.json()
        summary = body["riepilogo"]
        assert summary["pdf_rilevati"] == 21
        assert summary["pdf_analizzati"] == 21
        assert summary["totale_classificati"] == 21
        assert len(body["schede"]) == 21
        assert sum(summary[key] for key in
            ("regolare", "rettifica", "da_verificare", "duplicato")) == 21


def test_explicit_non_idoneo_and_escluso_are_concluded_without_score(tmp_path,monkeypatch):
    """Esito negativo motivato: NULL, mai zero, e candidatura conclusa."""
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        originals=list(sheets.values())
        ordinary=[doc for doc in originals if "P19" not in PdfReader(BytesIO(doc)).get_fields()]
        negative=_edit(ordinary[0],{
            "STATUS":"CANDIDATO NON IDONEO",
            "NOTE-COMM":"Valutazione negativa motivata dalla commissione nel documento di prova."
        })
        excluded=_edit(ordinary[1],{
            "STATUS":"CANDIDATO CON REQUISITO ENTE DI APPARTENENZA MANCANTE",
            "NOTE-COMM":"Requisito di appartenenza mancante nella documentazione sintetica."
        })
        preview=_preview(client,[("non_idoneo.pdf",negative),("escluso.pdf",excluded)])
        assert preview["riepilogo"]["regolare"]==2,preview["schede"]
        assert {x["decisione"] for x in preview["schede"]}=={"non_idoneo","escluso"}
        assert all(x["punteggio"] is None for x in preview["schede"])
        result=_confirm(client,preview["token"])
        assert result["acquisite"]==2
        summary=client.get("/api/bandi/26-02/valutazioni").json()
        assert summary["totale_acquisite"]==2
        assert summary["totale_idonee"]==0
        assert summary["totale_non_idonee"]==1
        assert summary["totale_escluse"]==1
        with server.connect() as db:
            values=db.execute("""
                SELECT decisione,totale_calcolato,metodo,esito,note FROM valutazioni_pdf
                WHERE stato='Acquisita' ORDER BY id
            """).fetchall()
            assert [r["decisione"] for r in values]==["non_idoneo","escluso"]
            assert all(r["totale_calcolato"] is None and r["metodo"]=="automatico" for r in values)


def test_negative_without_note_is_not_concluded(tmp_path,monkeypatch):
    _,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        ordinary=next(p for p in sheets.values()
            if "P19" not in PdfReader(BytesIO(p)).get_fields())
        neg=_edit(ordinary,{"STATUS":"CANDIDATO NON IDONEO"})
        preview=_preview(client,[("senza_nota.pdf",neg)])
        assert preview["riepilogo"]["da_verificare"]==1
        assert "Motivazione" in preview["schede"][0]["motivo"]
        assert client.get("/api/bandi/26-02/valutazioni").json()["totale_acquisite"]==0


def test_manual_idoneo_accepts_documented_total_and_logs_original(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        ordinary=next(p for p in sheets.values()
            if "P19" not in PdfReader(BytesIO(p)).get_fields())
        broken=_edit(ordinary,{
            "STATUS":"CANDIDATO AMMESSO IN GRADUATORIA",
            "P01":"2","TOT":"0"
        })
        preview=_preview(client,[("lettore_mac.pdf",broken)])
        assert preview["riepilogo"]["da_verificare"]==1
        _confirm(client,preview["token"])
        record=client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
        assert record["stato"]=="Da verificare"
        payload={
            "candidatura_id":record["candidatura_id"],
            "esito":"CANDIDATO AMMESSO IN GRADUATORIA",
            "punteggio":"2.00",
            "nota_commissione":"",
            "motivazione_tecnica":"Il programma PDF non ha aggiornato il totale.",
            "documento_verificato":True
        }
        response=_post(client,f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
                       json={**payload,"documento_verificato":False})
        assert response.status_code==400,response.text
        response=_post(client,f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
                       json=payload)
        assert response.status_code==200,response.text
        assert response.json()["decisione"]=="idoneo"
        summary=client.get("/api/bandi/26-02/valutazioni").json()
        assert summary["totale_acquisite"]==1
        assert summary["totale_idonee"]==1
        row=summary["archivio"][0]
        assert row["metodo"]=="manuale" and row["totale_calcolato"]==2
        downloaded=client.get(f"/api/bandi/26-02/valutazioni/{record['id']}/documento")
        assert downloaded.content==broken
        with server.connect() as db:
            events=db.execute("SELECT * FROM valutazioni_pdf_eventi").fetchall()
            assert len(events)==1
            assert events[0]["azione"]=="Acquisizione manuale"


def test_manual_negative_requires_original_note_and_uses_null_score(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        ordinary=next(p for p in sheets.values()
            if "P19" not in PdfReader(BytesIO(p)).get_fields())
        with_note=_edit(ordinary,{"NOTE-COMM":"Esito negativo deliberato nel verbale sintetico"})
        preview=_preview(client,[("da_verificare.pdf",with_note)])
        _confirm(client,preview["token"])
        record=client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
        data={
            "candidatura_id":record["candidatura_id"],
            "esito":"CANDIDATO NON IDONEO",
            "punteggio":None,"nota_commissione":"",
            "motivazione_tecnica":"Documento non estraibile automaticamente, revisione necessaria.",
            "documento_verificato":True
        }
        target=f"/api/bandi/26-02/valutazioni/{record['id']}/manuale"
        no_note=_post(client,target,json=data)
        assert no_note.status_code==400,no_note.text
        with_score=_post(client,target,json={**data,
            "nota_commissione":"Esito negativo deliberato nel verbale sintetico",
            "punteggio":"0"})
        assert with_score.status_code==400,with_score.text
        result=_post(client,target,json={**data,
            "nota_commissione":"Esito negativo deliberato nel verbale sintetico"})
        assert result.status_code==200,result.text
        with server.connect() as db:
            row=db.execute("""
                SELECT decisione,totale_calcolato,note,metodo
                FROM valutazioni_pdf WHERE id=?
            """,(record["id"],)).fetchone()
            assert row["decisione"]=="non_idoneo"
            assert row["totale_calcolato"] is None
            assert row["metodo"]=="manuale"


def test_manual_wrong_pica_and_filter_scope_rejected(tmp_path,monkeypatch):
    _,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        original=next(p for p in sheets.values()
            if "P19" not in PdfReader(BytesIO(p)).get_fields())
        preview=_preview(client,[("da_verificare.pdf",original)])
        _confirm(client,preview["token"])
        record=client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
        list_result=client.get("/api/bandi/26-02/valutazioni/candidature",
           params={"sede":"SEDE A"}).json()["candidature"]
        assert len(list_result)==3
        wrong=next(c for c in list_result if c["id"]!=record["candidatura_id"])
        bad=_post(client,f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
            json={"candidatura_id":wrong["id"],
                  "esito":"CANDIDATO UNICO IDONEO","punteggio":"2",
                  "nota_commissione":"",
                  "motivazione_tecnica":"Verifica PICA diversa dalla scheda di prova.",
                  "documento_verificato":True})
        assert bad.status_code==409,bad.text
        empty_site=client.get("/api/bandi/26-02/valutazioni",
             params={"sede":"SEDE B"}).json()
        assert empty_site["totale_attese"]==0
        assert empty_site["archivio"]==[]
        right_site=client.get("/api/bandi/26-02/valutazioni",
             params={"sede":"SEDE A"}).json()
        assert right_site["totale_attese"]==3
        assert len(right_site["archivio"])==1
        # Il filtro selezionato non può accettare schede riconosciute di altro CdS/sede.
        response=_post(client,"/api/bandi/26-02/valutazioni/anteprima",
            params={"sede":"SEDE B"},
            files={"files":("different_site.pdf",original,"application/pdf")})
        assert response.status_code==400,response.text
        assert "diversi" in response.text
