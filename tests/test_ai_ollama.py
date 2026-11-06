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


