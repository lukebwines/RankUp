"""Regressioni GroqCloud: denominazione ente minimizzata, privacy, senza rete."""
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfReader
from test_pdf_import import _setup, _post
from ai_groq import prepare_minimal_payload, GroqLimit

HEADERS = {"X-RankUp-Request": "1"}


def test_only_institutional_categories_are_sent_to_groq():
    raw={
        "id_pica":"12345",
        "cognome":"NOME SINTETICO",
        "codice_fiscale":"RSSMRA80A01H501U",
        "email":"fittizio@example.test",
        "occupazione":"Dipendente, nome sintetico, numero 3331234567",
        "azienda_ente":"AOUI Verona",
        "profilo_professionale":"Dirigente",
        "ente_richiesto":"AOUI",
    }
    minimal=prepare_minimal_payload(raw)
    assert set(minimal)=={
        "requisito_ente","denominazione_ente_dichiarato",
    }
    assert minimal["requisito_ente"]=="AOUI"
    assert minimal["denominazione_ente_dichiarato"]=="AOUI VERONA"
    assert "12345" not in str(minimal)
    assert "RSSMRA" not in str(minimal)
    assert "fittizio@" not in str(minimal)
    assert "NOME SINTETICO" not in str(minimal)
    # Denominazioni con possibili dati personali restano nel server.
    unknown=prepare_minimal_payload({**raw,"azienda_ente":"Studio privato fittizio Rossi"})
    assert unknown["denominazione_ente_dichiarato"]=="DENOMINAZIONE NON TRASMETTIBILE"
    assert "Rossi" not in str(unknown)


def test_admin_only_and_private_key(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        public=client.get("/api/admin/ai/config")
        assert public.status_code==200,public.text
        assert public.json()["chiave_configurata"] is False
        response=client.put("/api/admin/ai/config",headers=HEADERS,
            json={"model":"llama-3.3-70b-versatile","api_key":"groq_test_secret_1234567890"})
        assert response.status_code==200,response.text
        assert response.json()["chiave_configurata"] is True
        assert "groq_test_secret" not in response.text
        assert "groq_test_secret" not in client.get("/api/admin/ai/config").text
        keyfile=tmp_path/".groqcloud_key"
        assert keyfile.read_text()=="groq_test_secret_1234567890"
        assert (keyfile.stat().st_mode & 0o077)==0
        created=_post(client,"/api/utenti",json={
            "username":"operatore_test","password":"PasswordTest1234!"})
        assert created.status_code==200,created.text
        with TestClient(server.app) as user:
            assert _post(user,"/api/auth/login",json={
                "username":"operatore_test","password":"PasswordTest1234!"
            }).status_code==200
            assert user.get("/api/admin/ai/config").status_code==403
            assert user.get("/api/admin/ai/modelli").status_code==403
            assert user.get("/api/admin/ai/risultati?code=26-02").status_code==403
            assert user.get("/api/admin/ai/report.pdf?code=26-02").status_code==403
            assert _post(user,"/api/admin/ai/analizza-passo",
                json={"code":"26-02"}).status_code==403


def test_analysis_runs_on_current_PICA_occupations_without_evaluation_sheets(tmp_path,monkeypatch):
    server,admin,sheets=_setup(tmp_path,monkeypatch)
    with admin:
        config=admin.put("/api/admin/ai/config",headers=HEADERS,json={
            "api_key":"groq_fake_key_1234567890","model":"llama-3.3-70b-versatile"})
        assert config.status_code==200,config.text
        sent=[]
        def fake_analyze(key,model,payload):
            assert key=="groq_fake_key_1234567890"
            assert model=="llama-3.3-70b-versatile"
            assert set(payload)=={
                "requisito_ente","denominazione_ente_dichiarato"}
            assert "candidato" not in str(payload).lower()
            sent.append(dict(payload))
            return {"indizio":"DATI INSUFFICIENTI",
                    "motivo":"Informazioni insufficienti per confermare il rapporto.",
                    "verifiche":"Verificare la posizione dichiarata presso l'ente."}
        monkeypatch.setattr(server,"groq_analyze",fake_analyze)
        # Le valutazioni non sono state importate, ma PICA è già presente:
        # l'analisi dell'occupazione deve essere disponibile indipendentemente.
        with server.connect() as db:
            start=db.execute("SELECT COUNT(*) FROM valutazioni_pdf WHERE stato='Acquisita'").fetchone()[0]
        assert start==0
        first=_post(admin,"/api/admin/ai/analizza-passo",json={"code":"26-02"})
        assert first.status_code==200,first.text
        assert first.json()["nuovi"]==3
        assert len(sent)==3
        result=admin.get("/api/admin/ai/risultati?code=26-02")
        assert result.status_code==200,result.text
        body=result.json()
        assert body["completati"]==3
        assert body["da_analizzare"]==0
        assert all(row["id_pica"] for row in body["risultati"])
        # Le denominazioni minimizzate sono trasmesse; il rapporto con PICA è interno.
        assert len(sent)<=3
        again=_post(admin,"/api/admin/ai/analizza-passo",json={"code":"26-02"})
        assert again.status_code==200 and again.json()["nuovi"]==0
        report=admin.get("/api/admin/ai/report.pdf?code=26-02")
        assert report.status_code==200,report.text
        output=PdfReader(BytesIO(report.content))
        assert len(output.pages)>=1
        text="\n".join(page.extract_text() or "" for page in output.pages)
        assert "ID PICA" in text
        assert "RANKUP" in text.upper()
        assert "ID PICA" in text
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM ai_ente_verifiche").fetchone()[0]==3
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf WHERE stato='Acquisita'").fetchone()[0]==start
            assert db.execute("SELECT COUNT(*) FROM graduatorie_cds").fetchone()[0]==0


def test_rate_limit_preserves_previous_results(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        response=client.put("/api/admin/ai/config",headers=HEADERS,json={
            "model":"llama-3.3-70b-versatile",
            "api_key":"groq_fake_key_1234567890"})
        assert response.status_code==200,response.text
        def noquota(*_):
            raise GroqLimit(70)
        monkeypatch.setattr(server,"groq_analyze",noquota)
        result=_post(client,"/api/admin/ai/analizza-passo",json={"code":"26-02"})
        assert result.status_code==200,result.text
        assert result.json()["limite_raggiunto"] is True
        assert result.json()["retry_after"]==70
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM ai_ente_verifiche").fetchone()[0]==0



def test_models_endpoint_reports_groq_error_and_quota(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        assert client.put("/api/admin/ai/config",headers=HEADERS,
            json={"model":"openai/gpt-oss-120b","api_key":"groq_fake_key_1234567890"}).status_code==200
        def limit(*_):
            raise GroqLimit(45)
        monkeypatch.setattr(server,"groq_models",limit)
        result=client.get("/api/admin/ai/modelli")
        assert result.status_code==429,result.text
        assert result.headers.get("retry-after")=="45"
        assert "limite richieste" in result.json()["detail"]
        def not_allowed(*_):
            raise ValueError("GroqCloud HTTP 401: chiave API mancante, revocata o non valida")
        monkeypatch.setattr(server,"groq_models",not_allowed)
        result=client.get("/api/admin/ai/modelli")
        assert result.status_code==424,result.text
        assert result.headers.get("X-RankUp-Response-Origin")=="rankup"
        assert "HTTP 401" in result.json()["detail"]
        assert "groq_fake_key" not in result.text


def test_live_model_probe_is_synthetic_and_does_not_touch_pica(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        assert client.put("/api/admin/ai/config",headers=HEADERS,
            json={"model":"openai/gpt-oss-120b","api_key":"groq_fake_key_1234567890"}).status_code==200
        observed=[]
        def fake_analyze(key,model,payload):
            assert key=="groq_fake_key_1234567890"
            assert model=="openai/gpt-oss-120b"
            observed.append(payload)
            return {"indizio":"INDIZI COERENTI",
                    "motivo":"Caso esclusivamente sintetico",
                    "verifiche":"Verifica documentale",
                    "origine":"groqcloud","token_usati":42}
        monkeypatch.setattr(server,"groq_analyze",fake_analyze)
        response=_post(client,"/api/admin/ai/test")
        assert response.status_code==200,response.text
        assert response.json()["token_usati"]==42
        assert observed==[{
            "requisito_ente":"AOUI",
            "denominazione_ente_dichiarato":"AOUI VERONA",
        }]
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM ai_ente_verifiche").fetchone()[0]==0


def test_prepares_json_mode_and_reads_usage_without_pica_identifiers(monkeypatch):
    import ai_groq
    payload={
        "requisito_ente":"AOUI",
        "denominazione_ente_dichiarato":"AOUI",
    }
    outbound=[]
    def fake_json(url,key,body):
        outbound.append(body)
        return {
            "choices":[{"message":{"content":
                '{"indizio":"INDIZI COERENTI","motivo":"Categoria compatibile",'
                '"verifiche":"Verificare rapporto attivo"}'}}],
            "usage":{"total_tokens":130},
        }
    monkeypatch.setattr(ai_groq,"_fetch_json",fake_json)
    answer=ai_groq.analyze("groq_fake","openai/gpt-oss-120b",payload)
    assert answer["origine"]=="groqcloud"
    assert answer["token_usati"]==130
    assert len(outbound)==1
    assert outbound[0]["response_format"]=={"type":"json_object"}
    assert outbound[0]["reasoning_effort"]=="low"
    assert outbound[0]["include_reasoning"] is False
    assert "id_pica" not in str(outbound)
    assert "cognome" not in str(outbound)



def test_groq_temporary_502_is_retried_once_without_logging_key(monkeypatch):
    import ai_groq
    from urllib import error
    from io import BytesIO
    calls=[]
    secret="groq_synthetic_secret_1234567890"
    class Reply:
        def __init__(self,body):
            self.body=body
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def read(self,limit):
            return self.body[:limit]
    class MockOpener:
        def open(self,req,timeout=None):
            calls.append((req.full_url,timeout))
            if len(calls)==1:
                raise error.HTTPError(req.full_url,502,"Bad Gateway",{},BytesIO(b'{}'))
            return Reply(b'{"data":[{"id":"openai/gpt-oss-120b"}]}')
    monkeypatch.setattr(ai_groq.request,"build_opener",lambda *args:MockOpener())
    monkeypatch.setattr(ai_groq.time,"sleep",lambda seconds:None)
    assert ai_groq.models(secret)==["openai/gpt-oss-120b"]
    assert len(calls)==2
    assert all("api.groq.com" in url for url,timeout in calls)


def test_provider_502_returns_424_with_rankup_header(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        res=client.put("/api/admin/ai/config",headers=HEADERS,
            json={"model":"openai/gpt-oss-120b",
                  "api_key":"groq_synthetic_secret_1234567890"})
        assert res.status_code==200
        def fail_models(*_):
            raise ValueError("GroqCloud HTTP 502: servizio temporaneamente non disponibile")
        monkeypatch.setattr(server,"groq_models",fail_models)
        models=client.get("/api/admin/ai/modelli")
        assert models.status_code==424,models.text
        assert "GroqCloud HTTP 502" in models.json()["detail"]
        assert models.headers.get("X-RankUp-Response-Origin")=="rankup"
        def fail_probe(*_):
            raise ValueError("GroqCloud HTTP 502: servizio temporaneamente non disponibile")
        monkeypatch.setattr(server,"groq_analyze",fail_probe)
        probe=_post(client,"/api/admin/ai/test")
        assert probe.status_code==424,probe.text
        assert "GroqCloud HTTP 502" in probe.json()["detail"]
        assert probe.headers.get("X-RankUp-Response-Origin")=="rankup"
        assert "groq_synthetic_secret_" not in (probe.text+models.text)



def test_groq_requests_use_rankup_user_agent_for_models_and_completions(monkeypatch):
    """L'header vale per GET modelli e POST completions, senza chiavi nel risultato."""
    import ai_groq
    import json

    sent=[]
    class Reply:
        def __init__(self,data):
            self.data=data
        def __enter__(self):
            return self
        def __exit__(self,*args):
            return False
        def read(self,limit):
            return self.data[:limit]

    class Opener:
        def open(self,req,timeout=None):
            sent.append({
                "url":req.full_url,
                "method":req.get_method(),
                "agent":req.get_header("User-agent"),
                "authorization":req.get_header("Authorization"),
            })
            if req.full_url==ai_groq.API_MODELS:
                return Reply(b'{"data":[{"id":"openai/gpt-oss-120b"}]}')
            return Reply(json.dumps({
                "choices":[{"message":{"content":json.dumps({
                    "indizio":"INDIZI COERENTI",
                    "motivo":"Categoria compatibile",
                    "verifiche":"Controllo documentale",
                })}}],
                "usage":{"total_tokens":42}
            }).encode("utf-8"))

    monkeypatch.setattr(ai_groq.request,"build_opener",lambda *_:Opener())
    secret="groq_test_secret_123456789012345"
    assert ai_groq.models(secret)==["openai/gpt-oss-120b"]
    answer=ai_groq.analyze(secret,"openai/gpt-oss-120b",{
        "requisito_ente":"AOUI",
        "denominazione_ente_dichiarato":"AOUI",
    })
    assert answer["origine"]=="groqcloud"
    assert [r["method"] for r in sent]==["GET","POST"]
    assert all(r["agent"]==ai_groq.GROQ_USER_AGENT for r in sent)
    assert all(r["agent"]!="Python-urllib/3.12" for r in sent)
    assert all(r["authorization"]=="Bearer "+secret for r in sent)
    assert secret not in str(answer)


def test_cloudflare_1010_is_distinct_from_real_groq_403(monkeypatch):
    """Il messaggio individua il blocco Cloudflare senza suggerire nuova chiave."""
    import ai_groq
    from urllib import error
    from io import BytesIO

    class Block:
        def __init__(self,body):
            self.body=body
        def open(self,req,timeout=None):
            raise error.HTTPError(req.full_url,403,"Forbidden",
                                  {"Content-Type":"text/html"},
                                  BytesIO(self.body))
    current=[]
    def opener(*args):
        return Block(current[0])
    monkeypatch.setattr(ai_groq.request,"build_opener",opener)
    current[:]=[b'<html><title>Access denied</title><p>Error code: 1010</p>'
                b'<footer>Cloudflare</footer></html>']
    import pytest
    with pytest.raises(ValueError,match="Cloudflare 1010") as ex:
        ai_groq.models("sintetica_chiave_non_reale")
    assert "chiave API" in str(ex.value)
    assert "sintetica_chiave_non_reale" not in str(ex.value)

    current[:]=[b'{"error":{"message":"You do not have permission","type":"forbidden"}}']
    with pytest.raises(ValueError,match="GroqCloud HTTP 403") as ex:
        ai_groq.models("sintetica_chiave_non_reale")
    assert "Cloudflare 1010:" not in str(ex.value)
    assert "sintetica_chiave_non_reale" not in str(ex.value)
