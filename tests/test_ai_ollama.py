"""I test IA locali non richiedono un server Ollama e non aprono connessioni."""
from unittest.mock import patch

import pytest
from ai_ente import AIEmployerError, local_endpoint, status


def test_endpoint_local_only():
    assert local_endpoint("http://192.168.10.20:11434") == "http://192.168.10.20:11434"
    assert local_endpoint("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    for url in (
        "https://api.openai.com:11434", "http://8.8.8.8:11434",
        "https://192.168.10.20:11434", "http://192.168.10.20:8123",
        "http://192.168.10.20:11434/redirect",
        "http://username:secret@192.168.10.20:11434",
        "http://169.254.169.254:11434",
    ):
        with pytest.raises(AIEmployerError):
            local_endpoint(url)


def test_model_status():
    with patch("ai_ente._request_json", return_value={
        "models": [{"name": "qwen2.5:1.5b-instruct-q4_K_M"}]
    }) as call:
        result = status("http://127.0.0.1:11434")
        assert result["modello_installato"] is True
        assert call.call_args.args[1] == "/api/tags"



def test_local_occupation_review_sends_no_identity_or_pdf():
    import json
    from ai_ente import review_occupation
    response={"response":json.dumps({
        "stato":"DA VERIFICARE",
        "motivo":"Rapporto lavorativo non documentato",
        "evidenza":"azienda sanitaria di prova"
    })}
    with patch("ai_ente.status",return_value={"modello_installato":True}), \
         patch("ai_ente._request_json",return_value=response) as send:
        outcome=review_occupation("SSN","impiegato",
                                  "azienda sanitaria di prova",
                                  "tecnico","http://127.0.0.1:11434")
    assert outcome["stato"]=="DA VERIFICARE"
    assert outcome["evidenza"]=="azienda sanitaria di prova"
    assert outcome["modello_locale"] is True
    assert send.call_args.args[1]=="/api/generate"
    data=json.dumps(send.call_args.args[2],ensure_ascii=False)
    assert "azienda sanitaria di prova" in data
    assert "cognome" not in data.lower()
    assert "codice_fiscale" not in data.lower()
    assert "testo pdf" not in data.lower()


def test_local_occupation_cannot_use_fabricated_quote():
    import json
    from ai_ente import review_occupation
    response={"response":json.dumps({
        "stato":"COERENTE (PRELIMINARE)",
        "motivo":"Lavora con ente sanitario",
        "evidenza":"INFORMAZIONE INVENTATA"
    })}
    with patch("ai_ente.status",return_value={"modello_installato":True}), \
         patch("ai_ente._request_json",return_value=response):
        outcome=review_occupation("SSN","collaboratore","ente di prova",
                                  "profilo di prova","http://127.0.0.1:11434")
    assert outcome["stato"]=="DA VERIFICARE"
    assert not outcome["evidenza"]
