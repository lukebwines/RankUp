"""Diagnosi HTTP 500 in rianalisi mirata di due candidature (sintetiche).

Nessuna richiesta reale GroqCloud, nessun dato candidato effettivo.
"""
import json

from test_ai_queue_v109 import _prepare
from test_pdf_import import _post

def test_two_selected_reanalysis_after_previous_results(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        # Primo passaggio: tutti i candidati con risultati AI archiviati.
        first=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert first.status_code==200,first.text
        calls=[]
        def answer(key,model,payload,guidance=None):
            calls.append(payload)
            return {"indizio":"DATI INSUFFICIENTI",
                    "motivo":"Motivazione sintetica",
                    "verifiche":"Richiedere attestazione",
                    "origine":"groqcloud","token_usati":80}
        for _ in range(6):
            server.ai_process_one(server.connect,server._ai_worker_candidates,
                                  answer,server.GROQ.key,server.GROQ.limits)
        before=client.get("/api/admin/ai/lavori",params={"code":"26-02"}).json()
        assert before["lavoro"]["stato"]=="completata",before
        cases=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()["casi"]
        ids=[r["id"] for r in cases[:2]]
        assert len(ids)==2
        selected=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True,
            "ids_candidature":ids,"forza":True})
        assert selected.status_code==200,selected.text
        assert selected.json()["totale_candidature"]==2
        assert selected.json()["selezione_mirata"] is True
        for _ in range(6):
            server.ai_process_one(server.connect,server._ai_worker_candidates,
                                  answer,server.GROQ.key,server.GROQ.limits)
        done=client.get("/api/admin/ai/lavori",params={"code":"26-02"}).json()
        assert done["lavoro"]["stato"]=="completata",done


def test_two_distinct_institutional_contexts_still_generate_targeted_job(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        ids=[r["id"] for r in
             client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()["casi"][:2]]
        with server.connect() as db:
            originals=[
                {"azienda_ente":"",
                 "ateneo":"Universita di fantasia",
                 "occupazione":"",
                 "profilo_professionale":"professore associato"},
                {"azienda_ente":"",
                 "ateneo":"",
                 "occupazione":"Incarico di ricerca presso Universita di Verona",
                 "profilo_professionale":"Ricercatore"},
            ]
            for cid,values in zip(ids,originals):
                obj=json.loads(db.execute("SELECT source_json FROM candidature WHERE id=?",
                            (cid,)).fetchone()["source_json"])
                obj.update(values)
                db.execute("UPDATE candidature SET source_json=? WHERE id=?",
                           (json.dumps(obj),cid))
            db.commit()
        view=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()
        for cid in ids:
            row=next(x for x in view["casi"] if x["id"]==cid)
            assert row["richiede_ai"],row
        res=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True,
            "ids_candidature":ids,"forza":True})
        assert res.status_code==200,res.text
        assert res.json()["totale_candidature"]==2


def test_two_selected_while_other_queue_active_returns_explicit_409(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        old=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert old.status_code==200,old.text
        ids=[r["id"] for r in
             client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()["casi"][:2]]
        selected=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True,
            "ids_candidature":ids,"forza":True})
        assert selected.status_code==409,selected.text
        assert "coda" in selected.text.lower()
