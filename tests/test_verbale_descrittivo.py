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
        _,pdf=_form_from_zip(client)
        group=client.get("/api/bandi/26-02/verbali/graduatorie").json()["graduatorie"][0]
        base="/api/bandi/26-02/verbali/descrittivi"
        no_fields=_post(client,base,data={"cds":group["cds"],"sede":group["sede"]},
                        files={"file":("vuoto.pdf",pdf,"application/pdf")})
        assert no_fields.status_code==422,no_fields.text
        other=_fill_form(pdf,{"data_seduta":"09/11/2026","ora_seduta":"09:15",
                               "riassunto_discussione":"Verbale sintetico"})
        wrong=_post(client,base,data={"cds":"CdS diverso","sede":group["sede"]},
                    files={"file":("altro.pdf",other,"application/pdf")})
        assert wrong.status_code in (409,422),wrong.text
