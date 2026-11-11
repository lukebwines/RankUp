"""Collaudo v1.0.3: verbale descrittivo editabile e reimpaginazione integrale."""
from io import BytesIO
from zipfile import ZipFile

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject

from test_pdf_import import _setup, _post
from test_graduatorie import _complete_all
from verbale_seduta import nome_verbale


def _fill_form(pdf: bytes, values: dict) -> bytes:
    writer=PdfWriter(clone_from=BytesIO(pdf))
    objects=writer._root_object["/AcroForm"].get_object()["/Fields"]
    written=set()
    for obj in objects:
        field=obj.get_object()
        key=str(field.get("/T",""))
        if key in values:
            field[NameObject("/V")]=TextStringObject(values[key])
            written.add(key)
    assert written==set(values)
    buffer=BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _form_from_zip(client, base="/api/bandi/26-02"):
    response=client.get(base+"/schede/zip")
    assert response.status_code==200,response.text
    with ZipFile(BytesIO(response.content)) as z:
        names=[name for name in z.namelist() if name.startswith("VB_")]
        assert len(names)==1,names
        return names[0],z.read(names[0])


def test_generated_description_is_in_sheet_zip_and_names_are_short(tmp_path,monkeypatch):
    _,client,_=_setup(tmp_path,monkeypatch)
    with client:
        name,data=_form_from_zip(client)
        assert len(name)<=55
        assert name.endswith(".pdf")
        reader=PdfReader(BytesIO(data))
        fields=reader.get_fields() or {}
        assert {"data_seduta","ora_seduta","riassunto_discussione"}<=set(fields)
        assert str(reader.metadata.get("/Keywords","")).startswith("RANKUP-VERBALE:")
        assert reader.pages
        assert "VERBALE DELLA COMMISSIONE DI VALUTAZIONE" in (
            reader.pages[0].extract_text() or "")
    long_name=nome_verbale("26-02",
        "SCIENZE DELLE PROFESSIONI SANITARIE DELLA RIABILITAZIONE E INSEGNAMENTI",
        "VERONA")
    assert len(long_name)<=55
    assert nome_verbale("26-02","FISIOTERAPIA","VERONA")=="VB_FISIOTERAPIA_VR_26-02.pdf"


def test_missing_description_blocks_definitive_cds(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        _complete_all(client,sheets)
        group=client.get("/api/bandi/26-02/verbali/graduatorie").json()["graduatorie"][0]
        with server.connect() as db:
            db.execute("UPDATE verbali_descrittivi SET attivo=0")
        response=_post(client,"/api/bandi/26-02/verbali/graduatorie/genera-definitivo",
            json={"cds":group["cds"],"sede":group["sede"]})
        assert response.status_code==409,response.text
        assert "obbligatorio" in response.text
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM graduatorie_cds").fetchone()[0]==0


def test_long_discussion_is_read_entirely_and_static_fields_disappear(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        _complete_all(client,sheets)
        base="/api/bandi/26-02"
        group=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        first=client.get(base+"/verbali/descrittivi").json()["verbali"][0]
        original=client.get(base+"/verbali/descrittivi/"+str(first["id"])+
                            "/pdf?tipo=originale").content
        # Il testo supera di molte volte il campo a scorrimento del PDF.
        discussion="\n".join(
            "Paragrafo di collaudo "+str(i)+": "+("valutazione e discussione. "*8)
            for i in range(65)
        )+"\nULTIMA FRASE DOCUMENTALE DI VERIFICA"
        updated=_fill_form(original,{
            "data_seduta":"10/11/2026","ora_seduta":"16:25",
            "riassunto_discussione":discussion,
        })
        saved=_post(client,base+"/verbali/descrittivi",
            data={"cds":group["cds"],"sede":group["sede"]},
            files={"file":("rinominato.pdf",updated,"application/pdf")})
        assert saved.status_code==200,saved.text
        assert saved.json()["versione"]==2
        doc=client.get(base+"/verbali/descrittivi").json()["verbali"][0]
        static_response=client.get(base+"/verbali/descrittivi/"+str(doc["id"])+"/pdf")
        assert static_response.status_code==200
        static=PdfReader(BytesIO(static_response.content))
        assert len(static.pages)>3
        assert not static.get_fields()
        alltext="\n".join(p.extract_text() or "" for p in static.pages)
        assert "ULTIMA FRASE DOCUMENTALE DI VERIFICA" in alltext
        assert "10/11/2026" in alltext
        assert "16:25" in alltext
        assert all(
            not any(str(a.get_object().get("/Subtype",""))=="/Widget"
                    for a in (p.get("/Annots") or []))
            for p in static.pages)
        with server.connect() as db:
            rows=db.execute(
                "SELECT versione,attivo,discussione,pdf_originale FROM verbali_descrittivi ORDER BY versione"
            ).fetchall()
            assert len(rows)==2
            assert rows[0]["attivo"]==0 and rows[1]["attivo"]==1
            assert rows[1]["discussione"]==discussion
            assert bytes(rows[0]["pdf_originale"])==original
        final=_post(client,base+"/verbali/graduatorie/genera-definitivo",
                    json={"cds":group["cds"],"sede":group["sede"]})
        assert final.status_code==200,final.text
        result=client.get(base+"/verbali/graduatorie/pdf-completo",params={
            "cds":group["cds"],"sede":group["sede"]})
        assert result.status_code==200
        merged=PdfReader(BytesIO(result.content))
        assert not merged.get_fields()
        text="\n".join(p.extract_text() or "" for p in merged.pages)
        assert "ULTIMA FRASE DOCUMENTALE DI VERIFICA" in text
        assert "Graduatoria della Commissione" in text


def test_reject_description_for_other_course_or_unfilled(tmp_path,monkeypatch):
    _,client,_=_setup(tmp_path,monkeypatch)
    with client:
        group=client.get("/api/bandi/26-02/verbali/graduatorie").json()["graduatorie"][0]
        base="/api/bandi/26-02/verbali/descrittivi"
        _,pdf=_form_from_zip(client)
        missing_source=_post(client,base,
            data={"cds":group["cds"],"sede":group["sede"]},
            files={"file":("vuoto.pdf",pdf,"application/pdf")})
        assert missing_source.status_code==409
        refs=client.get("/api/bandi/26-02/riferimenti").json()["riferimenti"]
        payload={k:refs.get(k) or "" for k in
            ("repertorio","protocollo","data_avviso","data_scadenza","anno_accademico")}
        payload.update(testo_verbale_bando="Testo unicamente sintetico per il test",
                       expected_updated_at=refs.get("aggiornato_il"))
        assert client.put("/api/bandi/26-02/riferimenti",
            headers={"X-RankUp-Request":"1"},json=payload).status_code==200
        _,pdf=_form_from_zip(client)
        no_fields=_post(client,base,data={"cds":group["cds"],"sede":group["sede"]},
                        files={"file":("vuoto.pdf",pdf,"application/pdf")})
        assert no_fields.status_code==422,no_fields.text
        other=_fill_form(pdf,{"data_seduta":"09/11/2026","ora_seduta":"09:15",
                               "riassunto_discussione":"Verbale sintetico"})
        wrong=_post(client,base,data={"cds":"CdS diverso","sede":group["sede"]},
                    files={"file":("altro.pdf",other,"application/pdf")})
        assert wrong.status_code in (409,422),wrong.text


def test_acroform_fields_follow_text_and_no_orphan_signature(tmp_path,monkeypatch):
    """I widget devono seguire i Flowable e non sovrapporre testo e piè pagina."""
    from verbale_seduta import genera_pdf
    import re
    context = {
        "codice":"PROVA-PDF", "cds":"CORSO DI PROVA", "sede":"SEDE DI PROVA",
        "presidente":"PRESIDENTE DI PROVA", "componenti":"COMPONENTE DI PROVA",
        "anno_accademico":"", "repertorio":"", "protocollo":"",
        "data_avviso":"",
        "testo_bando":"\n".join(
            "Disposizione interamente sintetica di collaudo, con riferimenti non utilizzabili "
            "in un procedimento reale e finalizzata soltanto alla verifica dell'impaginazione."
            for _ in range(45)
        ),
    }
    raw=genera_pdf(context,editable=True)
    pdf=PdfReader(BytesIO(raw))
    assert len(pdf.pages)>=2
    assert "[DA VERIFICARE]" not in (pdf.pages[0].extract_text() or "")
    widgets={}
    for page_number,page in enumerate(pdf.pages):
        for a in page.get("/Annots") or []:
            annot=a.get_object()
            if str(annot.get("/Subtype",""))!="/Widget":
                continue
            name=str(annot.get("/T",""))
            rect=[float(v) for v in annot.get("/Rect")]
            # Contenuto entro i margini di stampa e fuori dal footer.
            assert rect[0]>=48, (page_number,name,rect)
            assert rect[1]>=62, (page_number,name,rect)
            assert rect[2]<=float(page.mediabox.width)-48, (page_number,name,rect)
            assert rect[3]<=float(page.mediabox.height)-110, (page_number,name,rect)
            widgets[name]=(page_number,rect)
    assert set(widgets)=={"data_seduta","ora_seduta","riassunto_discussione"}
    # Al contrario della versione errata, data e ora non sono sul fondo pagina.
    assert widgets["data_seduta"][0]==0
    assert widgets["ora_seduta"][0]==0
    assert widgets["data_seduta"][1][1]>450
    assert widgets["ora_seduta"][1][1]>450
    assert widgets["riassunto_discussione"][1][0]>48
    assert "Il campo può scorrere durante la compilazione" not in " ".join(
        p.extract_text() or "" for p in pdf.pages)
    assert "Il Presidente" in (pdf.pages[-1].extract_text() or "")


def test_verbale_discussione_on_third_page_does_not_create_signature_only_page():
    """Scenario di tre pagine: spazio riassunto ampio, firma non isolata."""
    from verbale_seduta import genera_pdf
    context={
        "codice":"PROVA-3", "cds":"LOGOPEDIA - CORSO SINTETICO DI COLLAUDO",
        "sede":"SEDE DI PROVA", "presidente":"[DA VERIFICARE]",
        "componenti":"[DA VERIFICARE]", "anno_accademico":"",
        "repertorio":"","protocollo":"","data_avviso":"",
        "testo_bando":"\n".join(
            "Condizioni generali e disposizioni riportate solo a scopo di prova "
            "dell'impaginazione di un documento multipagina. "
            "Nessun dato di procedimenti o candidati reali."
            for _ in range(62)),
    }
    pdf=PdfReader(BytesIO(genera_pdf(context,editable=True)))
    # La pagina della firma deve contenere anche almeno una parte sostanziale
    # del verbale e non soltanto un blocco di sottoscrizione.
    last=pdf.pages[-1].extract_text() or ""
    assert len(last.split()) > 25,(len(pdf.pages),last)
    assert "Il Presidente" in last
    assert any("Riassunto della discussione" in (p.extract_text() or "")
               for p in pdf.pages)
