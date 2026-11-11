"""Terzo modello Inglese: 50 punti, SSD configurabile e reimport senza contaminare gli altri modelli."""
from io import BytesIO
import zipfile
from pypdf import PdfReader
from test_pdf_import import _setup, _edit, _preview, _confirm, _post


def test_english_model_and_returned_evaluation(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        change=client.put("/api/ssd/MEDS-24%2FC/classificazione",
                          headers={"X-RankUp-Request":"1"},
                          json={"tipo":"inglese","riferimento":"Modello allegato di collaudo"})
        assert change.status_code==200,change.text
        available=client.get("/api/bandi/26-02/ssd").json()["ssd"]
        assert any(x["tipo"]=="inglese" for x in available)
        zip_response=client.get("/api/bandi/26-02/schede/zip")
        assert zip_response.status_code==200,zip_response.text
        with zipfile.ZipFile(BytesIO(zip_response.content)) as archive:
            docs=[archive.read(name) for name in archive.namelist() if name.endswith(".pdf") and not name.startswith("VB_")]
        english=[]
        other=[]
        for data in docs:
            pdf=PdfReader(BytesIO(data))
            fields=pdf.get_fields() or {}
            if "RANKUP-MODEL" in fields:
                english.append(data)
                assert fields["RANKUP-MODEL"].get("/V")=="inglese"
                assert all(f"P{i:02d}" in fields for i in range(1,14))
                assert "P14" not in fields
                assert fields["TIPO-CDS-PICA"].get("/V")
                text=pdf.pages[0].extract_text() or ""
                assert "LINGUA INGLESE" in text
                assert "50" in text
                assert "Tipologia di incarico" in text
                # Dottorato (3) e assegni di ricerca (1): scelta binaria.
                for field_name, fixed in (("P02","3"),("P05","1")):
                    options=fields[field_name].get("/Opt") or []
                    choices=[str(choice[-1] if isinstance(choice,list) else choice)
                             for choice in options]
                    assert set(choices)=={"0",fixed},(field_name,choices)

            else:
                other.append(data)
        assert english and other

        plain=_edit(english[0],{"P01":"2","P02":"3","P08":"8",
                                "P09":"4","P12":"7","P13":"6",
                                "TOT":"30","STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        reviewed=_preview(client,[("inglese_compilata.pdf",plain)])
        assert reviewed["riepilogo"]["regolare"]==1,reviewed
        assert reviewed["schede"][0]["punteggio"]==30
        saved=_confirm(client,reviewed["token"])
        assert saved["acquisite"]==1,saved

        # Il controllo server deve respingere anche un PDF manipolato
        # contenente un valore intermedio, nonostante il menu lo impedisca.
        intermediate=_edit(english[0],{"P02":"2","TOT":"2",
                                       "STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        intermediate_result=_preview(client,[("dottorato_intermedio.pdf",intermediate)])
        assert intermediate_result["riepilogo"]["da_verificare"]==1
        assert "P02" in intermediate_result["schede"][0]["motivo"]

        invalid=_edit(english[0],{"P08":"11","TOT":"11",
                                  "STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        issue=_preview(client,[("inglese_fuori_limite.pdf",invalid)])
        assert issue["riepilogo"]["da_verificare"]==1
        assert "P08" in issue["schede"][0]["motivo"]

        wrong_model=_edit(other[0],{"P01":"2","TOT":"2",
                                    "STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        # Un ordinario/professionalizzante resta distinto dal modello Inglese.
        assert "RANKUP-MODEL" not in (PdfReader(BytesIO(wrong_model)).get_fields() or {})


def test_english_schema_migration_keeps_classifications(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        with server.connect() as db:
            assert db.execute("""SELECT tipo FROM ssd_classificazioni
                WHERE ssd='MEDS-24/C'""").fetchone()["tipo"]=="professionalizzante"
        # Una nuova inizializzazione deve conservare la scelta salvata.
        server.initialize()
        with server.connect() as db:
            assert db.execute("""SELECT tipo FROM ssd_classificazioni
                WHERE ssd='MEDS-24/C'""").fetchone()["tipo"]=="professionalizzante"
            assert "'inglese'" in db.execute("""SELECT sql FROM sqlite_master
                WHERE name='ssd_classificazioni'""").fetchone()[0]
