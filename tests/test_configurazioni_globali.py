"""Commissioni e SSD persistono all'azzeramento: dati interamente sintetici."""
from test_pdf_import import _setup, _post
from test_import import pica_fixture


def test_reset_preserves_global_ssd_and_commissions_but_requires_explicit_reuse(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        base="/api/bandi/26-02"
        before_catalog=client.get("/api/admin/commissioni-globali")
        assert before_catalog.status_code==200,before_catalog.text
        catalog=before_catalog.json()["commissioni"]
        assert catalog
        original=client.get(base+"/commissioni").json()["commissioni"]
        assert original
        saved_ssd=client.get("/api/admin/ssd/classificazioni").json()["ssd"]
        assert any(x["ssd"]=="MEDS-24/C" and x["tipo"]=="professionalizzante" for x in saved_ssd)
        before=client.get("/api/admin/azzeramento").json()
        assert before["classificazioni_ssd_conservate"]>=1
        assert before["commissioni_conservate"]>=len(original)
        response=_post(client,"/api/admin/azzeramento",
                       json={"conferma":"AZZERA TUTTI I DATI RANKUP",
                             "password_admin":"PasswordFittiziaLunga12345!"})
        assert response.status_code==200,response.text
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM bandi").fetchone()[0]==0
            assert db.execute("SELECT COUNT(*) FROM commissioni").fetchone()[0]==0
            assert db.execute("SELECT COUNT(*) FROM commissioni_catalogo").fetchone()[0]==len(catalog)
            assert db.execute("SELECT tipo FROM ssd_classificazioni WHERE ssd='MEDS-24/C'").fetchone()[0]=="professionalizzante"
        assert client.get("/api/admin/commissioni-globali").json()["commissioni"]==catalog
        current=client.get("/api/admin/ssd/classificazioni").json()["ssd"]
        assert any(x["ssd"]=="MEDS-24/C" and x["tipo"]=="professionalizzante" for x in current)
        preview=_post(client,"/api/pica/preview",files={"file":(
            "nuova_procedura.xlsx",pica_fixture(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert preview.status_code==200,preview.text
        imported=_post(client,"/api/pica/commit",json={"token":preview.json()["token"]})
        assert imported.status_code==200,imported.text
        assert client.get(base+"/commissioni").json()["commissioni"]==[]
        assert client.get(base+"/commissioni/associazioni").json()["associate"]==0
        assert _post(client,base+"/commissioni/recupera-globali",
            json={"ids":[catalog[0]["id"]],"conferma_verifica":False}).status_code==400
        # Sono riutilizzabili solo le composizioni corrispondenti agli incarichi PICA.
        matched=next(x for x in catalog if any(
            x["cds"]==c["cds"] and x["sede"]==c["sede"] for c in original))
        restored=_post(client,base+"/commissioni/recupera-globali",
                       json={"ids":[matched["id"]],"conferma_verifica":True})
        assert restored.status_code==200,restored.text
        assert restored.json()["aggiunte"]==1
        assert _post(client,base+"/commissioni/recupera-globali",
            json={"ids":[matched["id"]],"conferma_verifica":True}).status_code==409
        assert len(client.get(base+"/commissioni").json()["commissioni"])==1


def test_global_ssd_works_without_bando_and_admin_guards(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        reset=_post(client,"/api/admin/azzeramento",
            json={"conferma":"AZZERA TUTTI I DATI RANKUP",
                  "password_admin":"PasswordFittiziaLunga12345!"})
        assert reset.status_code==200,reset.text
        change=client.put("/api/ssd/TEST-99%2FA/classificazione",
            headers={"X-RankUp-Request":"1"},
            json={"tipo":"inglese","riferimento":"Documento ufficiale sintetico di collaudo"})
        assert change.status_code==200,change.text
        no_reference=client.put("/api/ssd/TEST-99%2FA/classificazione",
            headers={"X-RankUp-Request":"1"},json={"tipo":"ordinario"})
        assert no_reference.status_code==200,no_reference.text
        assert no_reference.json()["riferimento"]=="Documento ufficiale sintetico di collaudo"
        assert any(x["ssd"]=="TEST-99/A" and x["tipo"]=="ordinario" for x in
                   client.get("/api/admin/ssd/classificazioni").json()["ssd"])
