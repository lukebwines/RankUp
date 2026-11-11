"""Collaudo v1.0.13 con soli dati sintetici: nessun accesso a GroqCloud."""
import json
import pytest

from ai_groq import (
    GroqInvalidResponse, prepare_minimal_payload, safe_institution_name, analyze,
)
from test_ai_queue_v109 import _prepare
from test_pdf_import import _post


@pytest.mark.parametrize("name,expected",[
    ("AULSS 9 Scaligera", "AULSS 9 SCALIGERA"),
    ("ULSS 9 Scaligera", "ULSS 9 SCALIGERA"),
    ("AOUI Verona", "AOUI VERONA"),
    ("Azienda Zero", "AZIENDA ZERO"),
    ("ASST Mantova", "ASST MANTOVA"),
    ("Studio privato dott. Mario Rossi", "DENOMINAZIONE NON TRASMETTIBILE"),
    ("AULSS 9, tel. 3331234567", "DENOMINAZIONE NON TRASMETTIBILE"),
    ("ENTE\nnome persona", "DENOMINAZIONE NON TRASMETTIBILE"),
    ("MARIO ROSSI", "DENOMINAZIONE NON TRASMETTIBILE"),
    ("", "NON DICHIARATO"),
])
def test_literal_institution_name_and_privacy(name,expected):
    assert safe_institution_name(name)==expected


def test_only_name_and_requirement_leave_the_server():
    raw={
        "nome":"NomeFittizio","cognome":"CognomeFittizio",
        "email":"fittizio@example.test","id_pica":"TEST-0001",
        "occupazione":"Dipendente numero 3331234567",
        "profilo_professionale":"Dirigente",
        "ente_richiesto":"SSN","azienda_ente":"AULSS 9 SCALIGERA"
    }
    result=prepare_minimal_payload(raw)
    assert result=={
        "requisito_ente":"SSN",
        "denominazione_ente_dichiarato":"AULSS 9 SCALIGERA",
    }
    assert "3331234567" not in json.dumps(result)
    assert "NomeFittizio" not in json.dumps(result)
    assert "CognomeFittizio" not in json.dumps(result)
    different={**raw,"occupazione":"Libero professionista","profilo_professionale":"Autonomo"}
    assert prepare_minimal_payload(different)==result
    raw["azienda_ente"]="AOUI Verona NomeFittizio"
    assert prepare_minimal_payload(raw)["denominazione_ente_dichiarato"]==(
        "DENOMINAZIONE NON TRASMETTIBILE")


def test_diagnostic_invalid_groq_reply_has_no_raw_text(monkeypatch):
    import ai_groq
    def fake(*_):
        return {
            "choices":[{"finish_reason":"length",
                        "message":{"content":"NOME PRIVATO E TESTO INCOMPLETO"}}],
            "usage":{"total_tokens":44},
        }
    monkeypatch.setattr(ai_groq,"_fetch_json",fake)
    with pytest.raises(GroqInvalidResponse,match="troncato") as err:
        analyze("chiave_sintetica","openai/gpt-oss-120b",{
            "requisito_ente":"SSN",
            "denominazione_ente_dichiarato":"AULSS 9 SCALIGERA",
        })
    assert err.value.tokens==44
    assert "NOME PRIVATO" not in str(err.value)


def test_invalid_replies_are_retried_and_skipped_without_ai_conclusions(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        start=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert start.status_code==200,start.text
        calls=[]
        def invalid(*args,**kwargs):
            calls.append(1)
            raise GroqInvalidResponse("JSON malformato o incompleto",23)
        for _ in range(6):
            assert server.ai_process_one(server.connect,server._ai_worker_candidates,
                    invalid,server.GROQ.key,server.GROQ.limits()) is True
        assert server.ai_process_one(server.connect,server._ai_worker_candidates,
                invalid,server.GROQ.key,server.GROQ.limits()) is False
        status=client.get("/api/admin/ai/lavori",params={"code":"26-02"}).json()["lavoro"]
        assert status["stato"]=="completata"
        assert status["completati"]==3
        assert status["da_esaminare"]==0
        assert status["errori_tecnici"]==3
        assert status["chiamate"]==3
        assert status["token"]==3*23
        assert len(calls)==3
        results=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()["casi"]
        assert len(results)==3
        assert all(row["ai"] is None for row in results)
        assert all(row.get("errore_tecnico") for row in results)
        assert all(row["esito"]=="DA APPROFONDIRE" for row in results)
        # Gli esiti tecnici non entrano nella cache istituzionale.
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM ai_ente_ai_esiti").fetchone()[0]==0
        retry=_post(client,
            "/api/admin/ai/lavori/"+str(start.json()["lavoro"]["id"])+"/riprova_errori")
        assert retry.status_code==200,retry.text
        assert retry.json()["lavoro"]["da_esaminare"]==3
        assert retry.json()["lavoro"]["errori_tecnici"]==0
