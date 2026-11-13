"""RankUP 1.0.17: cinque campi PICA visibili, quota per modello e riserva.

Tutti i dati sono sintetici. Nessuna chiamata di rete verso GroqCloud.
"""
import json
import time

from ai_groq import GroqSecrets, GroqLimit, DEFAULT_QUOTA
from test_ai_queue_v109 import _prepare, HEADERS
from test_pdf_import import _post


def test_legacy_quotas_remain_on_original_model_when_switching(tmp_path):
    groq=GroqSecrets(tmp_path)
    groq.save("groq_fake_key_123456789","openai/gpt-oss-120b")
    old={**DEFAULT_QUOTA,"margine":90,"stima_token":1300}
    (tmp_path/"groqcloud_quota.json").write_text(json.dumps(old))
    assert groq.limits("openai/gpt-oss-120b")==old
    groq.save(None,"openai/gpt-oss-20b")
    assert groq.limits("openai/gpt-oss-120b")==old
    assert groq.limits("openai/gpt-oss-20b")==DEFAULT_QUOTA
    new={**DEFAULT_QUOTA,"margine":85,"stima_token":1100}
    groq.save_limits(new,"openai/gpt-oss-20b")
    assert groq.limits("openai/gpt-oss-120b")==old
    assert groq.limits("openai/gpt-oss-20b")==new
    assert set(groq.configured_models())=={"openai/gpt-oss-120b","openai/gpt-oss-20b"}


def test_fallback_requires_distinct_model_with_saved_quota(tmp_path):
    groq=GroqSecrets(tmp_path)
    groq.save("groq_fake_key_123456789","openai/gpt-oss-120b")
    try:
        groq.save(None,"openai/gpt-oss-120b","openai/gpt-oss-20b",True)
    except ValueError as exc:
        assert "limiti" in str(exc)
    else:
        assert False,"La riserva non configurata deve essere respinta"
    groq.save_limits(dict(DEFAULT_QUOTA),"openai/gpt-oss-20b")
    groq.save(None,"openai/gpt-oss-120b","openai/gpt-oss-20b",True)
    assert groq.failover()=={"fallback_model":"openai/gpt-oss-20b",
                             "fallback_enabled":True}
    try:
        groq.save(None,"openai/gpt-oss-120b","openai/gpt-oss-120b",True)
    except ValueError as exc:
        assert "diverso" in str(exc)
    else:
        assert False,"La riserva deve essere distinta dal principale"


def _configure_reserve(client):
    response=client.put("/api/admin/ai/limiti?model=openai/gpt-oss-20b",
        headers=HEADERS,json=DEFAULT_QUOTA)
    assert response.status_code==200,response.text
    response=client.put("/api/admin/ai/config",headers=HEADERS,json={
        "model":"openai/gpt-oss-120b","fallback_model":"openai/gpt-oss-20b",
        "fallback_enabled":True})
    assert response.status_code==200,response.text
    assert response.json()["fallback_enabled"] is True


def test_admin_can_read_five_actual_pica_fields_with_privacy_status(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        with server.connect() as db:
            item=db.execute("SELECT id,source_json FROM candidature LIMIT 1").fetchone()
            data=json.loads(item["source_json"])
            data.update({"azienda_ente":"","ateneo":"Universita' di fantasia",
                         "occupazione":"Ricercatore presso Universita' di fantasia",
                         "profilo_professionale":"Docente di ricerca",
                         "qualifica":"Professore associato"})
            db.execute("UPDATE candidature SET source_json=? WHERE id=?",
                       (json.dumps(data),item["id"]))
            db.commit()
        response=client.get("/api/admin/ai/verifiche",params={"code":"26-02"})
        assert response.status_code==200,response.text
        row=next(r for r in response.json()["casi"] if r["id"]==item["id"])
        original=row["campi_pica_originali"]
        payload=row["campi_pica_ai"]
        assert set(original)=={"Azienda/ente","Ateneo","Occupazione",
                               "Profilo professionale","Qualifica"}
        assert original["Azienda/ente"]==""
        assert original["Ateneo"]=="Universita' di fantasia"
        assert original["Qualifica"]=="Professore associato"
        assert payload["Ateneo"]=="UNIVERSITA' DI FANTASIA"
        assert payload["Azienda/ente"]=="NON DICHIARATO"


def test_automatic_fallback_on_local_tpm_preserves_results_and_model(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        _configure_reserve(client)
        created=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert created.status_code==200,created.text
        with server.connect() as db:
            job=db.execute("SELECT * FROM ai_ente_lavori ORDER BY id DESC LIMIT 1").fetchone()
            assert job["modello"]=="openai/gpt-oss-120b"
            assert job["modello_riserva"]=="openai/gpt-oss-20b"
            assert job["failover_attivo"]==1
            snapshot=json.loads(job["limiti_modelli_json"])
            assert snapshot["openai/gpt-oss-20b"]["tpd"]==200000
            db.execute("INSERT INTO ai_ente_quota_eventi(eseguito_il,modello,token) VALUES(?,?,?)",
                       (time.time(),"openai/gpt-oss-120b",8000))
            db.commit()
        observed=[]
        def send(key,model,payload,guidance=None):
            observed.append(model)
            assert model=="openai/gpt-oss-20b"
            return {"indizio":"INDIZI COERENTI",
                    "motivo":"Ente istituzionalmente compatibile (sintetico)",
                    "verifiche":"Verificare separatamente il rapporto dichiarato",
                    "origine":"groqcloud","token_usati":110}
        for _ in range(5):
            server.ai_process_one(server.connect,server._ai_worker_candidates,
                                  send,server.GROQ.key,server.GROQ.limits)
        assert observed==["openai/gpt-oss-20b"]
        status=client.get("/api/admin/ai/lavori",params={"code":"26-02"}).json()["lavoro"]
        assert status["stato"]=="completata"
        assert status["modello"]=="openai/gpt-oss-120b"
        assert status["modello_attivo"]=="openai/gpt-oss-20b"
        rows=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()["casi"]
        assert len(rows)==3
        assert all(row["ai"]["indizio"]=="INDIZI COERENTI" for row in rows)
        assert all(row["ai"]["modello"]=="openai/gpt-oss-20b" for row in rows)
        with server.connect() as db:
            models={r["modello"] for r in db.execute("SELECT modello FROM ai_ente_ai_esiti")}
        assert models=={"openai/gpt-oss-20b"}


def test_429_switches_once_then_waits_if_fallback_is_limited(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        _configure_reserve(client)
        created=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert created.status_code==200,created.text
        seen=[]
        def always_limited(key,model,payload,guidance=None):
            seen.append(model)
            raise GroqLimit(110)
        for _ in range(2):
            assert server.ai_process_one(server.connect,server._ai_worker_candidates,
                always_limited,server.GROQ.key,server.GROQ.limits)
        assert seen==["openai/gpt-oss-120b","openai/gpt-oss-20b"]
        job=client.get("/api/admin/ai/lavori",params={"code":"26-02"}).json()["lavoro"]
        assert job["stato"]=="attesa_quota"
        assert job["modello_attivo"]=="openai/gpt-oss-20b"
        assert job["da_esaminare"]==3
        assert job["completati"]==0


def test_admin_ui_uses_real_select_and_pica_card():
    from pathlib import Path
    html=(Path(__file__).parents[1]/"rankup/app/static/index.html").read_text()
    css=(Path(__file__).parents[1]/"rankup/app/static/workspace.css").read_text()
    assert '<select id="ai-model"' in html
    assert '<select id="ai-backup-model"' in html
    assert '<datalist id="ai-models"' not in html
    assert '["BB","Azienda/ente"]' in html
    assert '["BD","Ateneo"]' in html
    assert '["BM","Occupazione"]' in html
    assert '["BA","Profilo professionale"]' in html
    assert '["BO","Qualifica"]' in html
    assert "ru-ai-pica-card" in css
    assert "ru-ai-results-table" in css
