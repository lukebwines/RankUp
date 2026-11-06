"""RankUP: suggerimenti locali tramite Ollama, senza credenziali cloud.

Non analizza più il PDF del bando. È mantenuto il solo controllo di\nraggiungibilità del modello locale, per eventuali controlli occupazionali.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


VALID = {"SSN", "AOUI", "ASUIT", "UNITN", "SSP", "ASUIT O UNITN", "NESSUNA RISERVA"}
DEFAULT_MODEL = "qwen2.5:1.5b-instruct-q4_K_M"


class AIEmployerError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AIEmployerError("Reindirizzamento Ollama non consentito")


_opener = build_opener(ProxyHandler({}), _NoRedirect)


def local_endpoint(url: str) -> str:
    """Rifiuta host pubblici, porta inattesa e URL che possono uscire dalla LAN."""
    parsed = urlsplit((url or "").strip())
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise AIEmployerError("Specificare un endpoint Ollama HTTP interno, senza credenziali")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise AIEmployerError("L'endpoint Ollama deve contenere soltanto host e porta")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AIEmployerError("Porta Ollama non valida") from exc
    if port != 11434:
        raise AIEmployerError("È ammessa soltanto la porta locale Ollama 11434")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
        if not (address.is_private or address.is_loopback) or address.is_link_local or address.is_unspecified or address.is_multicast:
            raise AIEmployerError("Il servizio Ollama deve trovarsi su una rete privata")
    except ValueError:
        if not (
            host == "localhost" or host.endswith(".local")
            or re.fullmatch(r"[a-z0-9-]+(?:_)?rankup[-_]ollama", host)
            or host.endswith("-ollama")
        ):
            raise AIEmployerError("Usare un hostname di add-on Ollama oppure un IP privato")
        try:
            resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise AIEmployerError("Hostname Ollama non risolvibile dalla rete dell'add-on") from exc
        if not resolved:
            raise AIEmployerError("Hostname Ollama senza indirizzi IP")
        for item in resolved:
            ip = ipaddress.ip_address(item[4][0])
            if not (ip.is_private or ip.is_loopback) or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                raise AIEmployerError("Endpoint Ollama risolto su rete non privata")
    return (url or "").strip().rstrip("/")


def _request_json(endpoint: str, path: str, data: dict | None = None,
                  timeout: int = 240) -> dict:
    method = "POST" if data is not None else "GET"
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    request = Request(endpoint + path, data=body, method=method,
                      headers={"Content-Type": "application/json"})
    try:
        with _opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise AIEmployerError("Il servizio Ollama non ha risposto correttamente")
            payload = response.read(1_500_001)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise AIEmployerError(
            "Ollama locale non raggiungibile o elaborazione scaduta. Verificare l'add-on e il modello."
        ) from exc
    if len(payload) > 1_500_000:
        raise AIEmployerError("Risposta Ollama oltre il limite consentito")
    try:
        result = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise AIEmployerError("Risposta Ollama non valida") from exc
    if not isinstance(result, dict):
        raise AIEmployerError("Risposta Ollama inattesa")
    return result


def status(url: str, model: str = DEFAULT_MODEL) -> dict:
    endpoint = local_endpoint(url)
    result = _request_json(endpoint, "/api/tags", timeout=8)
    models = [m.get("name") or m.get("model") or ""
              for m in result.get("models", []) if isinstance(m, dict)]
    return {"raggiungibile": True, "modello": model,
            "modello_installato": model in models, "modelli_presenti": models}


