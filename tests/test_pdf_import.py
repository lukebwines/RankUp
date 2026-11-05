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


def test_legacy_acquired_without_outcome_is_reclassified_without_losing_pdf_or_score(
    tmp_path, monkeypatch
):
    """Una scheda storica da 15 punti senza esito non è un'idoneità valida."""
    import hashlib
    from datetime import datetime,timezone

    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        pdf=next(doc for doc in sheets.values()
                 if "P19" not in PdfReader(BytesIO(doc)).get_fields())
        # Simuliamo un record registrato dalla versione precedente, quando
        # la lettura del totale non richiedeva un esito della commissione.
        with server.connect() as db:
            bid=db.execute("SELECT id FROM bandi WHERE codice='26-02'").fetchone()[0]
            fields=PdfReader(BytesIO(pdf)).get_fields()
            id_pica=str(fields["ID-DOMANDA"].value)
            row=db.execute("""
                SELECT c.id FROM candidature c
                JOIN incarichi i ON i.id=c.incarico_id
                WHERE c.bando_id=? AND c.id_pica=?
                ORDER BY c.id LIMIT 1
            """,(bid,id_pica)).fetchone()
            assert row
            cid=row["id"]
            digest=hashlib.sha256(pdf).hexdigest()
            originals=server.SOURCES/"26-02"/"schede_restituite"
            originals.mkdir(parents=True,exist_ok=True)
            saved=originals/(digest+".pdf")
            saved.write_bytes(pdf)
            timestamp=datetime.now(timezone.utc).isoformat()
            db.execute("""
                INSERT INTO valutazioni_pdf
                (bando_id,candidatura_id,stato,versione,attiva,sha256,
                 nome_originale,percorso,modello,punteggi_json,totale_calcolato,
                 totale_pdf,esito,note,motivo,confermata_da,confermata_il,decisione,metodo)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,(bid,cid,"Acquisita",1,1,digest,"originale.pdf",
                 str(saved.relative_to(server.BASE)),"ordinario","{}",15,
                 "15","","","Controlli superati",
                 "utente-test",timestamp,"da_verificare","automatico"))
            db.commit()
        server.initialize()
        with server.connect() as db:
            migrated=db.execute("""
                SELECT stato,attiva,decisione,totale_calcolato,versione,
                       confermata_il,sha256,motivo FROM valutazioni_pdf
                WHERE sha256=?
            """,(digest,)).fetchone()
            assert migrated["stato"]=="Da verificare"
            assert migrated["attiva"]==0
            assert migrated["decisione"]=="da_verificare"
            assert migrated["totale_calcolato"]==15
            assert migrated["versione"]==1
            assert migrated["confermata_il"]==timestamp
            assert migrated["sha256"]==digest
            events=db.execute("""
                SELECT azione FROM valutazioni_pdf_eventi
                WHERE azione='Riesame automatico acquisizione precedente'
            """).fetchall()
            assert len(events)==1
        server.initialize() # Il riesame non deve aggiungere duplicati.
        with server.connect() as db:
            assert db.execute("""
                SELECT COUNT(*) FROM valutazioni_pdf_eventi
                WHERE azione='Riesame automatico acquisizione precedente'
            """).fetchone()[0]==1
        assert saved.read_bytes()==pdf
        report=client.get("/api/bandi/26-02/valutazioni").json()
        assert report["totale_acquisite"]==0
        assert report["archivio"][0]["stato"]=="Da verificare"
        assert report["archivio"][0]["totale_calcolato"]==15
        # Non è lecito inventare un giudizio positivo usando soltanto il numero.
        response=_post(client,
             f"/api/bandi/26-02/valutazioni/{report['archivio'][0]['id']}/manuale",
             json={
                "candidatura_id":cid,
                "esito":"CANDIDATO AMMESSO IN GRADUATORIA",
                "punteggio":"15",
                "nota_commissione":"",
                "motivazione_tecnica":"Punteggio storico senza esito della commissione.",
                "documento_verificato":True
             })
        assert response.status_code==409,response.text
        assert "non contiene né esito né motivazione" in response.text


def test_professional_three_points_without_outcome_stays_pending_but_shows_provisional_score(
    tmp_path, monkeypatch
):
    """Caso PDF screenshot: 3 punti assegnati, esito '-' e note vuote."""
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        template=next(doc for doc in sheets.values()
                      if "P19" in PdfReader(BytesIO(doc)).get_fields())
        document=_edit(template,{"P05":"3","TOT":"3"})
        preview=_preview(client,[("professional_three.pdf",document)])
        assert preview["riepilogo"]["regolare"]==0
        assert preview["riepilogo"]["da_verificare"]==1
        item=preview["schede"][0]
        assert item["punteggio"]==3
        assert item["decisione"]=="da_verificare"
        assert item["candidatura_id"] is not None
        assert "Esito della commissione non selezionato" in item["motivo"]
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf").fetchone()[0]==0
        result=_confirm(client,preview["token"])
        assert result["da_verificare"]==1
        report=client.get("/api/bandi/26-02/valutazioni").json()
        assert report["totale_acquisite"]==0
        record=report["archivio"][0]
        assert record["stato"]=="Da verificare"
        assert record["decisione"]=="da_verificare"
        assert record["totale_calcolato"] is None
        assert record["punteggio_rilevato"]==3
        assert record["manuale_possibile"] is False
        assert client.get(
            f"/api/bandi/26-02/valutazioni/{record['id']}/documento"
        ).content==document
        # Non basta trascrivere 3 punti: manca l'esito deliberato.
        attempt=_post(
            client,f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
            json={
                "candidatura_id":record["candidatura_id"],
                "esito":"CANDIDATO AMMESSO IN GRADUATORIA",
                "punteggio":"3",
                "nota_commissione":"",
                "motivazione_tecnica":"Esito assente nell'originale, registrazione non lecita.",
                "documento_verificato":True
            }
        )
        assert attempt.status_code==409,attempt.text


def test_old_inactive_acquired_without_outcome_gets_archived_as_unvalidated(
    tmp_path,monkeypatch
):
    """Migrazione include versioni inattive: non restano etichette 'Acquisita' improprie."""
    import hashlib
    from datetime import datetime,timezone
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        template=next(doc for doc in sheets.values()
                      if "P19" not in PdfReader(BytesIO(doc)).get_fields())
        with server.connect() as db:
            bid=db.execute("SELECT id FROM bandi WHERE codice='26-02'").fetchone()[0]
            applicant=db.execute("SELECT id FROM candidature WHERE bando_id=? LIMIT 1",(bid,)).fetchone()[0]
            digest=hashlib.sha256(template).hexdigest()
            directory=server.SOURCES/"26-02"/"schede_restituite"
            directory.mkdir(parents=True,exist_ok=True)
            path=directory/(digest+".pdf")
            path.write_bytes(template)
            db.execute("""
                INSERT INTO valutazioni_pdf
                (bando_id,candidatura_id,stato,versione,attiva,sha256,
                nome_originale,percorso,modello,punteggi_json,totale_calcolato,
                totale_pdf,esito,note,motivo,confermata_da,confermata_il,decisione,metodo)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,(
                bid,applicant,"Acquisita",1,0,digest,"inattiva.pdf",
                str(path.relative_to(server.BASE)),"ordinario","{}",15,
                "15","","","Storica","test",datetime.now(timezone.utc).isoformat(),
                "da_verificare","automatico"
            ))
            db.commit()
        server.initialize()
        with server.connect() as db:
            row=db.execute("SELECT stato,totale_calcolato,attiva FROM valutazioni_pdf WHERE sha256=?",(digest,)).fetchone()
            assert row["stato"]=="Da verificare"
            assert row["attiva"]==0
            assert row["totale_calcolato"]==15


def test_only_admin_can_manually_acquire_a_pdf(tmp_path, monkeypatch):
    """Il ruolo deve essere verificato nel server, anche con POST diretto."""
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        original=next(p for p in sheets.values()
            if "P19" not in PdfReader(BytesIO(p)).get_fields())
        # Una scheda tecnicamente anomala, ma con giudizio esplicito nel PDF.
        document=_edit(original,{
            "STATUS":"CANDIDATO AMMESSO IN GRADUATORIA",
            "P01":"2",
            "TOT":"0",
        })
        preview=_preview(client,[("scheda_riservata.pdf",document)])
        assert preview["riepilogo"]["da_verificare"]==1
        _confirm(client,preview["token"])
        record=client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
        payload={
            "candidatura_id":record["candidatura_id"],
            "esito":"CANDIDATO AMMESSO IN GRADUATORIA",
            "punteggio":"2",
            "nota_commissione":"",
            "motivazione_tecnica":"Il totale Acrobat non e' stato aggiornato nel PDF.",
            "documento_verificato":True,
        }
        original_role=server.AUTH.role
        monkeypatch.setattr(server.AUTH, "role", lambda cookie: (
            "user" if server.AUTH.valid(cookie) else None
        ))
        status=client.get("/api/auth/status")
        assert status.status_code==200
        assert status.json()["role"]=="user"
        # L'utente regolarmente autenticato può leggere il documento.
        readable=client.get(
            f"/api/bandi/26-02/valutazioni/{record['id']}/documento"
        )
        assert readable.status_code==200
        assert readable.content==document
        # Non può invocare il POST riservato attraverso una chiamata diretta.
        denied=_post(client,
            f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
            json=payload)
        assert denied.status_code==403,denied.text
        assert "riservata all'amministratore" in denied.text
        with server.connect() as db:
            row=db.execute(
                "SELECT stato,totale_calcolato FROM valutazioni_pdf WHERE id=?",
                (record["id"],)
            ).fetchone()
            assert row["stato"]=="Da verificare"
            assert row["totale_calcolato"] is None
        monkeypatch.setattr(server.AUTH,"role",original_role)
        assert client.get("/api/auth/status").json()["role"]=="admin"
        approved=_post(client,
            f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
            json=payload)
        assert approved.status_code==200,approved.text
        assert approved.json()["metodo"]=="manuale"
