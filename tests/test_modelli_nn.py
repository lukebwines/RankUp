"""Scelte modello per incarichi con SSD NN: dati sintetici, nessuna procedura reale."""
from io import BytesIO
import zipfile

from pypdf import PdfReader

from test_pdf_import import _setup, _edit, _preview, _confirm


def _pdf_for(sheets, key):
    for pdf in sheets:
        marker = str((PdfReader(BytesIO(pdf)).get_fields() or {}).get(
            "RANKUP-ID", {}).get("/V") or "")
        if marker.endswith("-" + key[:10]):
            return pdf
    raise AssertionError("Scheda non trovata per incarico test")


def test_nn_choice_is_per_job_and_used_by_generation_and_pdf_import(tmp_path, monkeypatch):
    server,client,originals=_setup(tmp_path,monkeypatch)
    with client:
        with server.connect() as db:
            jobs=[dict(row) for row in db.execute(
                "SELECT id,key,ssd FROM incarichi ORDER BY id")]
            assert len(jobs)==2
            wrong=client.put(
                "/api/bandi/26-02/incarichi/"+jobs[0]["key"]+"/modello-scheda",
                headers={"X-RankUp-Request":"1"},
                json={"tipo":"inglese","riferimento":"Documento di collaudo"})
            assert wrong.status_code==409
            for job in jobs:
                db.execute("UPDATE incarichi SET ssd='NN' WHERE id=?",(job["id"],))
        url="/api/bandi/26-02/incarichi/"+jobs[0]["key"]+"/modello-scheda"
        listing=client.get("/api/bandi/26-02/incarichi/modelli-nn")
        assert listing.status_code==200,listing.text
        assert len(listing.json()["incarichi"])==2
        assert all(x["modello_effettivo"]=="ordinario" for x in listing.json()["incarichi"])
        assert all(not x["bloccato"] for x in listing.json()["incarichi"])
        assert listing.json()["incarichi"][0]["modello_esplicito"] is None

        no_ref=client.put(url,headers={"X-RankUp-Request":"1"},
                          json={"tipo":"inglese","riferimento":"[DA VERIFICARE]"})
        assert no_ref.status_code==400
        invalid=client.put(url,headers={"X-RankUp-Request":"1"},
                           json={"tipo":"inventato","riferimento":"Documento di collaudo"})
        assert invalid.status_code==400
        save=client.put(url,headers={"X-RankUp-Request":"1"},
                        json={"tipo":"inglese","riferimento":"Documento di collaudo"})
        assert save.status_code==200,save.text
        assert save.json()["modificato"]
        repeat=client.put(url,headers={"X-RankUp-Request":"1"},
                          json={"tipo":"inglese","riferimento":"Documento di collaudo"})
        assert repeat.status_code==200 and not repeat.json()["modificato"]

        after=client.get("/api/bandi/26-02/incarichi/modelli-nn").json()["incarichi"]
        assert len(after)==2
        assert {x["key"]:x["modello_effettivo"] for x in after}=={
            jobs[0]["key"]:"inglese",jobs[1]["key"]:"ordinario"}
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM incarichi_modelli_scheda_eventi"
                              ).fetchone()[0]==1
            cand, _=server._val_context(db,1)
            assert {c["modello"] for c in cand if c["key"]==jobs[0]["key"]}=={"inglese"}
            assert {c["modello"] for c in cand if c["key"]==jobs[1]["key"]}=={"ordinario"}

        new_zip=client.get("/api/bandi/26-02/schede/zip")
        assert new_zip.status_code==200,new_zip.text
        with zipfile.ZipFile(BytesIO(new_zip.content)) as archive:
            pdfs=[archive.read(n) for n in archive.namelist() if n.endswith(".pdf")]
        english=_pdf_for(pdfs,jobs[0]["key"])
        ordinary=_pdf_for(pdfs,jobs[1]["key"])
        assert (PdfReader(BytesIO(english)).get_fields()["RANKUP-MODEL"].get("/V")
                == "inglese")
        assert "P19" not in (PdfReader(BytesIO(ordinary)).get_fields() or {})
        assert "RANKUP-MODEL" not in (PdfReader(BytesIO(ordinary)).get_fields() or {})

        old=_pdf_for(list(originals.values()),jobs[0]["key"])
        old_preview=_preview(client,[("precedente.pdf",old)])
        assert old_preview["riepilogo"]["da_verificare"]==1
        assert "modello PDF" in old_preview["schede"][0]["motivo"]

        completed=_edit(english,{"P01":"2","TOT":"2",
                                  "STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        preview=_preview(client,[("modello_inglese.pdf",completed)])
        assert preview["riepilogo"]["regolare"]==1,preview["schede"]
        confirmed=_confirm(client,preview["token"])
        assert confirmed["acquisite"]==1
        locked=client.get("/api/bandi/26-02/incarichi/modelli-nn").json()["incarichi"]
        assert next(x for x in locked if x["key"]==jobs[0]["key"])["bloccato"]
        forbidden=client.put(url,headers={"X-RankUp-Request":"1"},
                             json={"tipo":"professionalizzante",
                                   "riferimento":"Nuovo documento di collaudo"})
        assert forbidden.status_code==409
        with server.connect() as db:
            assert db.execute("SELECT modello FROM incarichi_modelli_scheda WHERE incarico_id=?",
                              (jobs[0]["id"],)).fetchone()[0]=="inglese"
            assert db.execute("SELECT COUNT(*) FROM incarichi_modelli_scheda_eventi"
                              ).fetchone()[0]==1
        server.initialize()
        assert client.get("/api/bandi/26-02/incarichi/modelli-nn").json(
            )["incarichi"][0]["modello_effettivo"]=="inglese"


def test_nn_choice_keeps_permanent_ssd_settings_independent(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        with server.connect() as db:
            job=dict(db.execute("SELECT id,key FROM incarichi LIMIT 1").fetchone())
            db.execute("UPDATE incarichi SET ssd='NN' WHERE id=?",(job["id"],))
        choice=client.put("/api/bandi/26-02/incarichi/"+job["key"]+"/modello-scheda",
                          headers={"X-RankUp-Request":"1"},
                          json={"tipo":"professionalizzante",
                                "riferimento":"Scheda del bando collaudo"})
        assert choice.status_code==200,choice.text
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM ssd_classificazioni WHERE ssd='NN'"
                              ).fetchone()[0]==0
            assert db.execute("SELECT COUNT(*) FROM incarichi_modelli_scheda"
                              ).fetchone()[0]==1
