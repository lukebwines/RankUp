"""Gestione graduatorie CdS, bozze parziali e chiusura solo dopo gli atti."""
from io import BytesIO
from pypdf import PdfReader
from test_pdf_import import _setup, _post, _edit, _preview, _confirm


def _complete_all(client, sheets):
    docs=[]
    for idx,(filename,pdf) in enumerate(sheets.items(),1):
        filled=_edit(pdf,{"P01":str(idx),"TOT":str(idx),
                          "STATUS":"CANDIDATO AMMESSO IN GRADUATORIA",
                          "NOTE-COMM":"Nota di valutazione della commissione "+str(idx)})
        docs.append(("compilata_"+str(idx)+".pdf",filled))
    preview=_preview(client,docs)
    assert preview["riepilogo"]["regolare"]==len(docs),preview
    result=_confirm(client,preview["token"])
    assert result["acquisite"]==len(docs),result


def test_graduatoria_cdse_approvazione_parziale(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        code="26-02"
        base=f"/api/bandi/{code}"
        early=client.get(base+"/verbali/graduatorie")
        assert early.status_code==200,early.text
        assert early.json()["graduatorie"][0]["pronta"] is False
        assert early.json()["graduatorie"][0]["schede_validate"]==0
        assert early.json()["graduatorie"][0]["schede_da_risolvere"]==3
        assert early.json()["graduatorie"][0]["verbale_generabile"] is False
        body={"cds":early.json()["graduatorie"][0]["cds"],"sede":early.json()["graduatorie"][0]["sede"]}
        assert _post(client,base+"/verbali/graduatorie/definitiva",json=body).status_code==409
        assert client.get(base+"/verbali/graduatorie/pdf",params=body).status_code==409
        assert client.get(base+"/verbali/graduatorie/pdf-completo",params=body).status_code==409

        _complete_all(client,sheets)
        view=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        assert view["pronta"] is True, view["criticita"]
        assert view["schede_validate"]==view["totale_candidature"]==3
        assert view["schede_da_risolvere"]==0
        assert view["verbale_generabile"] is True
        assert len(view["incarichi"])==2
        pdf=client.get(base+"/verbali/graduatorie/pdf",params=body)
        assert pdf.status_code==200,pdf.text[:300] if pdf.status_code!=200 else ""
        text="\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf.content)).pages)
        assert "Nota di valutazione della commissione" in text
        assert "Candidato" in text
        assert client.get(base+"/verbali/graduatorie/pdf-completo",params=body).status_code==409
        sealed=_post(client,base+"/verbali/graduatorie/genera-definitivo",json=body)
        assert sealed.status_code==200,sealed.text
        assert sealed.json()["stato"]=="definitiva"
        joined=client.get(base+"/verbali/graduatorie/pdf-completo",params=body)
        assert joined.status_code==200,joined.text[:200] if joined.status_code!=200 else ""
        assert joined.headers["content-type"]=="application/pdf"
        merged=PdfReader(BytesIO(joined.content))
        original_pages=sum(len(PdfReader(BytesIO(x)).pages) for x in sheets.values())
        assert len(merged.pages)==len(PdfReader(BytesIO(pdf.content)).pages)+original_pages
        joined_text="\\n".join(page.extract_text() or "" for page in merged.pages)
        assert "Graduatoria della Commissione" in joined_text
        assert "Nota di valutazione della commissione" in joined_text
        assert merged.get_fields() is None
        assert all(not any(str(a.get_object().get("/Subtype",""))=="/Widget"
                   for a in (p.get("/Annots") or [])) for p in merged.pages)
        assert "Nota di valutazione della commissione" in joined_text
        complete_after=client.get(base+"/verbali/graduatorie/pdf-completo",params=body)
        assert complete_after.status_code==200
        assert complete_after.content==joined.content  # byte immutabili, non rigenerati
        assert len(PdfReader(BytesIO(complete_after.content)).pages)==len(merged.pages)
        assert _post(client,base+"/verbali/graduatorie/definitiva",json=body).status_code==409
        view=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        gid=view["conferma"]["id"]
        drafted=_post(client,base+"/graduatorie/approvazioni",
                      json={"graduatorie_ids":[gid]})
        assert drafted.status_code==200,drafted.text
        approval_id=drafted.json()["id"]
        # Creare una bozza non chiude formalmente le posizioni.
        assert client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]["stato"]=="definitiva"
        general=client.get(base+f"/graduatorie/approvazioni/{approval_id}/pdf")
        assert general.status_code==200,general.text[:200] if general.status_code!=200 else ""
        txt="\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(general.content)).pages)
        assert "BOZZA DA SOTTOPORRE AD APPROVAZIONE" in txt
        assert "ID PICA" in txt
        assert "Candidato Uno" in txt or "Candidato Due" in txt
        assert _post(client,base+f"/graduatorie/approvazioni/{approval_id}/conferma",
                     json={"estremi_atto":"[DA VERIFICARE]"}).status_code==400
        confirmed=_post(client,base+f"/graduatorie/approvazioni/{approval_id}/conferma",
                        json={"estremi_atto":"ATTO DI COLLAUDO VERIFICATO"})
        assert confirmed.status_code==200,confirmed.text
        assert confirmed.json()["graduatorie_chiuse"]==1
        assert client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]["stato"]=="approvata"
        assert _post(client,base+f"/graduatorie/approvazioni/{approval_id}/conferma",
                     json={"estremi_atto":"ALTRO ATTO"}).status_code==409
        report=client.get(base+f"/graduatorie/approvazioni/{approval_id}/pdf")
        assert report.status_code==200
        approved_text=PdfReader(BytesIO(report.content)).pages[0].extract_text()
        assert "Graduatoria" in approved_text
        assert "ATTI APPROVATI" in approved_text
        assert "Estremi del provvedimento" not in approved_text
        assert "ATTO DI COLLAUDO VERIFICATO" not in approved_text
        # Gli estremi dell'atto rimangono nel database, non sono stampati.
        with server.connect() as db:
            assert db.execute("SELECT estremi_atto FROM graduatorie_approvazioni WHERE id=?",
                              (approval_id,)).fetchone()[0]=="ATTO DI COLLAUDO VERIFICATO"
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM graduatorie_eventi").fetchone()[0]>=3


def test_parita_blocca_graduatoria(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        docs=[]
        for idx,pdf in enumerate(sheets.values()):
            filled=_edit(pdf,{"P01":"2","TOT":"2",
                              "STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
            docs.append((str(idx)+".pdf",filled))
        preview=_preview(client,docs)
        assert _confirm(client,preview["token"])["acquisite"]==3
        g=client.get("/api/bandi/26-02/verbali/graduatorie").json()["graduatorie"][0]
        assert not g["pronta"]
        assert any("Parita" in x for x in g["criticita"])
        attempt=_post(client,"/api/bandi/26-02/verbali/graduatorie/definitiva",
                      json={"cds":g["cds"],"sede":g["sede"]})
        assert attempt.status_code==409
        tied=next(x for x in g["incarichi"] if len(x["candidature"])>1)
        selected=tied["candidature"][0]["candidatura_id"]
        fixed=client.put("/api/bandi/26-02/verbali/graduatorie/"+
                         str(tied["id"])+"/vincitore",
                         headers={"X-RankUp-Request":"1"},
                         json={"candidatura_id":selected,
                               "motivazione":"Decisione motivata della Commissione nel collaudo",
                               "riferimento":"Documento formale sintetico di prova"})
        assert fixed.status_code==200,fixed.text
        refreshed=client.get("/api/bandi/26-02/verbali/graduatorie").json()["graduatorie"][0]
        assert refreshed["pronta"],refreshed["criticita"]
        assert refreshed["verbale_generabile"] is True
        assert next(x for x in refreshed["incarichi"] if x["id"]==tied["id"])["vincitore_id"]==selected


def test_verbale_idoneo_senza_note_non_indica_da_verificare():
    """La nota facoltativa assente non è un dato amministrativo da verificare."""
    from graduatorie import render_graduatoria_pdf
    data = {
        "cds": "CdS sintetico", "sede": "Sede sintetica", "stato": "BOZZA",
        "incarichi": [{
            "insegnamento": "Insegnamento sintetico",
            "modulo": "Modulo sintetico", "ssd": "SSD sintetico",
            "vincitore_id": 1,
            "candidature": [{
                "candidatura_id": 1, "id_pica": "00001",
                "cognome": "Candidato", "nome": "Prova",
                "decisione": "idoneo", "esito": "CANDIDATO IDONEO",
                "posizione": 1, "punteggio": "12.00", "note": ""
            }]
        }]
    }
    pdf = render_graduatoria_pdf("TEST", data)
    text = "\\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)
    assert "CANDIDATO IDONEO" in text
    assert "12.00" in text
    assert "[DA VERIFICARE]" not in text
    assert "—" in text or "-" in text


def test_cds_pdf_and_sealed_verbale_include_pica_partition_but_original_is_immutable(tmp_path, monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        with server.connect() as db:
            job=db.execute("SELECT id FROM incarichi ORDER BY id LIMIT 1").fetchone()
            assert job
            db.execute("""UPDATE incarichi SET ul=?,partizione_studenti=?,codice_partizione=?
                          WHERE id=?""",
                       ("UL sintetica","Partizione sintetica A","A",job["id"]))
        _complete_all(client,sheets)
        base="/api/bandi/26-02/verbali/graduatorie"
        scope=client.get(base).json()["graduatorie"][0]
        selection={"cds":scope["cds"],"sede":scope["sede"]}
        draft=client.get(base+"/pdf",params=selection)
        assert draft.status_code==200,draft.text if draft.status_code!=200 else ""
        draft_text=" ".join(
            " ".join((p.extract_text() or "").split())
            for p in PdfReader(BytesIO(draft.content)).pages)
        assert "UL: UL sintetica" in draft_text, draft_text
        assert "Partizione: Partizione sintetica A (cod. A)" in draft_text, draft_text
        result=_post(client,base+"/genera-definitivo",json=selection)
        assert result.status_code==200,result.text
        sealed=client.get(base+"/pdf-completo",params=selection)
        assert sealed.status_code==200
        sealed_text=" ".join(
            " ".join((p.extract_text() or "").split())
            for p in PdfReader(BytesIO(sealed.content)).pages)
        assert "UL: UL sintetica" in sealed_text, sealed_text
        assert "Partizione: Partizione sintetica A (cod. A)" in sealed_text, sealed_text
        with server.connect() as db:
            db.execute("UPDATE incarichi SET ul=? WHERE id=?",("Altro valore di collaudo",job["id"]))
        again=client.get(base+"/pdf-completo",params=selection)
        assert again.status_code==200 and again.content==sealed.content
