"""Autenticazione locale RankUp per accesso diretto via rete e Ingress.

La password è letta da /data/options.json, configurata in Home Assistant.
Le sessioni restano in memoria, si annullano al riavvio e non vengono scritte
nel database. Non si registrano credenziali nei log.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import threading
import time

COOKIE_NAME = "rankup_session"
SESSION_SECONDS = 8 * 60 * 60


class Authentication:
    def __init__(self, data_dir: Path):
        self.username = ""
        self.configured = False
        self._salt = secrets.token_bytes(20)
        self._verifier = b""
        self._sessions: dict[str, float] = {}
        self._failures = deque(maxlen=200)
        self._lock = threading.Lock()

        path = data_dir / "options.json"
        try:
            options = json.loads(path.read_text(encoding="utf-8"))
            username = str(options.get("admin_username", "")).strip()
            password = str(options.get("admin_password", ""))
            if 3 <= len(username) <= 50 and len(password) >= 12:
                self.username = username
                self._verifier = self._derive(password)
                self.configured = True
        except (FileNotFoundError, ValueError, TypeError, OSError):
            pass

    def _derive(self, password: str) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), self._salt, 390_000
        )

    def authenticate(self, username: str, password: str) -> str | None:
        if not self.configured:
            return None
        now = time.monotonic()
        with self._lock:
            while self._failures and now - self._failures[0] > 60:
                self._failures.popleft()
            if len(self._failures) >= 8:
                return None
        # Calcolo in tempo costante anche se l'utente non esiste.
        valid_password = hmac.compare_digest(self._derive(password), self._verifier)
        valid_user = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        if not (valid_password and valid_user):
            with self._lock:
                self._failures.append(time.monotonic())
            return None
        token = secrets.token_urlsafe(36)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._lock:
            self._failures.clear()
            self._sessions[digest] = time.time() + SESSION_SECONDS
        return token

    def valid(self, cookie: str | None) -> bool:
        if not self.configured or not cookie or len(cookie) > 256:
            return False
        digest = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
        with self._lock:
            expiry = self._sessions.get(digest)
            if expiry is None:
                return False
            if time.time() >= expiry:
                self._sessions.pop(digest, None)
                return False
            return True

    def role(self, cookie: str | None) -> str | None:
        """Ruolo della sessione autenticata, mai derivato da dati del client.

        RankUp attualmente prevede un unico account configurato nell'add-on:
        tale account è l'amministratore. Eventuali utenti futuri dovranno
        ottenere un ruolo per sessione, non tramite il parametro di una API.
        """
        return "admin" if self.valid(cookie) else None

    def revoke(self, cookie: str | None) -> None:
        if not cookie:
            return
        digest = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
        with self._lock:
            self._sessions.pop(digest, None)


def cookie_path(request) -> str:
    """Limita la sessione al percorso Ingress quando disponibile."""
    ingress_path = request.headers.get("x-ingress-path", "")
    if ingress_path.startswith("/api/hassio_ingress/"):
        return ingress_path.rstrip("/") + "/"
    return "/"


def secure_cookie(request) -> bool:
    # Cloudflare Tunnel termina TLS prima di inoltrare a HTTP locale.
    forwarded = request.headers.get("x-forwarded-proto", "").lower()
    return request.url.scheme == "https" or forwarded == "https"
