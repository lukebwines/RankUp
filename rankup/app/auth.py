"""Autenticazione locale RankUp per Ingress e accesso diretto.

L'account amministratore resta nelle opzioni di Home Assistant (password
minima preesistente di 12 caratteri). Gli account operativi vengono conservati
nel database persistente /data/rankup.sqlite3, con PBKDF2-HMAC-SHA256,
salt casuale per utente e password minima di 8 caratteri.

I token di sessione non vengono scritti su disco e decadono al riavvio.
Disabilitazione e cambio password revocano anche sessioni già aperte.
"""
from __future__ import annotations

from collections import deque
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time

COOKIE_NAME = "rankup_session"
SESSION_SECONDS = 8 * 60 * 60
HASH_ITERATIONS = 390_000


class Authentication:
    def __init__(self, data_dir: Path):
        self.username = ""
        self.configured = False
        self._db_path = data_dir / "rankup.sqlite3"
        self._salt = secrets.token_bytes(20)
        self._verifier = b""
        # sha256(cookie) -> (scadenza, username, ruolo, id_utente, versione_credenziali)
        self._sessions: dict[str, tuple[float, str, str, int | None, int | None]] = {}
        self._failures = deque(maxlen=500)
        self._lock = threading.Lock()

        path = data_dir / "options.json"
        try:
            options = json.loads(path.read_text(encoding="utf-8"))
            username = str(options.get("admin_username", "")).strip()
            password = str(options.get("admin_password", ""))
            if 3 <= len(username) <= 50 and len(password) >= 12:
                self.username = username
                self._verifier = self._derive(password, self._salt)
                self.configured = True
        except (FileNotFoundError, ValueError, TypeError, OSError):
            pass

    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, HASH_ITERATIONS
        )

    def password_record(self, password: str) -> tuple[str, str]:
        """La password non viene mai salvata in chiaro o restituita al client."""
        if not 8 <= len(password) <= 128 or not password.strip():
            raise ValueError("Password utente: almeno 8 e non oltre 128 caratteri")
        salt = secrets.token_bytes(24)
        return salt.hex(), self._derive(password, salt).hex()

    def _load_user(self, username: str) -> dict | None:
        try:
            with sqlite3.connect(self._db_path, timeout=10) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("""
                    SELECT id,username,password_salt,password_hash,attivo,credenziali_versione
                    FROM rankup_utenti WHERE username=? COLLATE NOCASE
                """, (username,)).fetchone()
                return dict(row) if row else None
        except (sqlite3.Error, OSError):
            return None

    def authenticate(self, username: str, password: str) -> str | None:
        if not self.configured:
            return None
        now = time.monotonic()
        key = username.casefold()
        with self._lock:
            while self._failures and now - self._failures[0][0] > 60:
                self._failures.popleft()
            if sum(1 for _, name in self._failures if name == key) >= 8:
                return None
            if len(self._failures) >= 100:
                return None

        # Un percorso uniforme di confronto, anche per username inesistenti.
        admin_match = hmac.compare_digest(
            username.encode("utf-8"), self.username.encode("utf-8")
        )
        entry = None if admin_match else self._load_user(username)
        if admin_match:
            salt, verifier = self._salt, self._verifier
        elif entry is not None:
            try:
                salt = bytes.fromhex(entry["password_salt"])
                verifier = bytes.fromhex(entry["password_hash"])
            except (ValueError, TypeError):
                return None
        else:
            salt, verifier = self._salt, self._verifier

        try:
            valid = hmac.compare_digest(self._derive(password, salt), verifier)
        except (ValueError, TypeError):
            valid = False
        active = admin_match or bool(entry and entry["attivo"])
        if not (valid and active and (admin_match or entry is not None)):
            with self._lock:
                self._failures.append((time.monotonic(), key))
            return None

        token = secrets.token_urlsafe(36)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        role = "admin" if admin_match else "user"
        identity = self.username if admin_match else entry["username"]
        user_id = None if admin_match else entry["id"]
        version = None if admin_match else entry["credenziali_versione"]
        with self._lock:
            self._failures = deque(
                ((when, name) for when, name in self._failures if name != key),
                maxlen=500
            )
            self._sessions[digest] = (
                time.time() + SESSION_SECONDS, identity, role, user_id, version
            )
        return token

    def principal(self, cookie: str | None) -> dict | None:
        if not self.configured or not cookie or len(cookie) > 256:
            return None
        digest = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
        with self._lock:
            session = self._sessions.get(digest)
            if not session:
                return None
            expiry, username, role, user_id, version = session
            if time.time() >= expiry:
                self._sessions.pop(digest, None)
                return None
        if role == "admin":
            return {"username": self.username, "role": "admin", "id": None}
        if role != "user" or user_id is None:
            return None

        # Rilegge stato/versione dal DB ad ogni richiesta: password cambiata,
        # account disabilitato o eliminato invalidano la sessione subito.
        try:
            with sqlite3.connect(self._db_path, timeout=10) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("""
                    SELECT username,attivo,credenziali_versione
                    FROM rankup_utenti WHERE id=?
                """,(user_id,)).fetchone()
        except (sqlite3.Error,OSError):
            return None
        if not row or not row["attivo"] or row["credenziali_versione"] != version:
            with self._lock:
                self._sessions.pop(digest, None)
            return None
        return {"username": row["username"], "role": "user", "id": user_id}

    def valid(self, cookie: str | None) -> bool:
        return self.principal(cookie) is not None

    def role(self, cookie: str | None) -> str | None:
        p = self.principal(cookie)
        return p["role"] if p else None

    def revoke(self, cookie: str | None) -> None:
        if not cookie:
            return
        digest = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
        with self._lock:
            self._sessions.pop(digest, None)

    def revoke_user(self, user_id: int) -> None:
        with self._lock:
            for digest, entry in list(self._sessions.items()):
                if entry[3] == user_id:
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
