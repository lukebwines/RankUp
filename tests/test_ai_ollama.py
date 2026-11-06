"""I test IA locali non richiedono un server Ollama e non aprono connessioni."""
import json
from unittest.mock import patch

import pytest
from ai_ente import AIEmployerError, local_endpoint, propose, status


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


def test_proposals_validated_from_same_pdf_excerpt():
    text = "ENTE DI APPARTENENZA RICHIESTO\nMODULO PROFESSIONALE - AOUI Verona\n"
    assignment = [{"key": "abcd1234", "cds": "CDS DI PROVA", "sede": "SEDE PROVA",
                   "modulo": "MODULO PROFESSIONALE", "insegnamento": "INSEGNAMENTO DI PROVA"}]
    response = {"response": json.dumps({"proposte": [
        {"key": "abcd1234", "ente": "AOUI", "citazione": "MODULO PROFESSIONALE - AOUI Verona"},
        {"key": "inesistente", "ente": "SSN", "citazione": "MODULO PROFESSIONALE - AOUI Verona"},
        {"key": "abcd1234", "ente": "SSN", "citazione": "Citazione non presente"},
    ]})}
    with patch("ai_ente.status", return_value={"modello_installato": True}), \
         patch("ai_ente._request_json", return_value=response) as send:
        result = propose(text, assignment, "http://127.0.0.1:11434")
    assert len(result) == 1
    assert result[0]["ente_proposto"] == "AOUI"
    request_body = send.call_args.args[2]
    assert request_body["model"].startswith("qwen2.5:1.5b")
    assert request_body["stream"] is False
    assert request_body["keep_alive"] == "0"
    assert "cognome" not in json.dumps(request_body).lower()


def test_no_reservation_requires_literal_evidence():
    text = "MODULO DEMO - SSN"
    item = [{"key": "xyz", "cds": "CDS", "sede": "SEDE",
             "modulo": "MODULO DEMO", "insegnamento": "ESEMPIO"}]
    response = {"response": json.dumps({"proposte": [
        {"key": "xyz", "ente": "NESSUNA RISERVA", "citazione": "MODULO DEMO - SSN"}
    ]})}
    with patch("ai_ente.status", return_value={"modello_installato": True}), \
         patch("ai_ente._request_json", return_value=response):
        assert propose(text, item, "http://127.0.0.1:11434") == []


def test_many_assignments_requires_course_filter():
    assignments = [{"key": str(i), "modulo": str(i)} for i in range(13)]
    with pytest.raises(AIEmployerError, match="12"):
        propose("TESTO", assignments, "http://127.0.0.1:11434")
