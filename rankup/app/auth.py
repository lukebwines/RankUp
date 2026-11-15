"""Autenticazione locale RankUp per Ingress e accesso diretto.

L'account amministratore viene migrato dalle opzioni Home Assistant a un hash
persistente, con recupero monouso avviato solo dalle opzioni HA. Gli operatori sono
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
from datetime import datetime,timezone,timedelta
from db_lifecycle import AutoClosingConnection
import admin_credentials as admin_store

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
        # Limite per origine, non per username: tentativi contro l'Admin
        # non possono impedire ad altri indirizzi di autenticarsi.
        # Nessun lockout globale: evita denial-of-service per account noti.
        self._failures = deque(maxlen=3000)
        self._lock = threading.Lock()

        settings=admin_store.options(data_dir)
        # Schema gia' aggiornato dal server prima di istanziare Authentication.
        admin_store.bootstrap(self._db_path,settings)
        self.reload_admin()
        request_number=settings.get("admin_recovery_request",0)
        code=admin_store.issue_recovery(self._db_path,request_number)
        if code is not None:
            # Visibile solo nel log privilegiato dell'add-on Home Assistant.
            # Mai restituito da un endpoint pubblico o memorizzato in chiaro.
            print("RANKUP RECUPERO ADMIN (Home Assistant) — codice monouso:",code,
                  "— valido 10 minuti. Non condividere questo codice.",flush=True)

    def reload_admin(self)->None:
        record=admin_store.get_admin(self._db_path)
        self.username=record["username"]
        self.configured=bool(record["password_hash"] and record["password_salt"])
        if self.configured:
            self._salt=bytes.fromhex(record["password_salt"])
            self._verifier=bytes.fromhex(record["password_hash"])
        else:
            self._salt=secrets.token_bytes(20)
            self._verifier=b""

    def revoke_admin(self)->None:
        with self._lock:
            for digest,entry in list(self._sessions.items()):
                if entry[2]=="admin":
                    self._sessions.pop(digest,None)
        self.reload_admin()

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
            with sqlite3.connect(self._db_path, timeout=10, factory=AutoClosingConnection) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("""
                    SELECT id,username,password_salt,password_hash,attivo,credenziali_versione
                    FROM rankup_utenti WHERE username=? COLLATE NOCASE
                """, (username,)).fetchone()
                return dict(row) if row else None
        except (sqlite3.Error, OSError):
            return None

    def verify_operator_password(self,user_id:int,password:str)->bool:
        """Verifica la password corrente dell'operatore, senza creare sessioni."""
        if not isinstance(password,str):
            return False
        try:
            with sqlite3.connect(self._db_path,timeout=10,factory=AutoClosingConnection) as db:
                db.row_factory=sqlite3.Row
                row=db.execute("""SELECT password_salt,password_hash FROM rankup_utenti
                    WHERE id=? AND attivo=1 AND eliminato=0""",(user_id,)).fetchone()
            if not row:
                return False
            calculated=self._derive(password,bytes.fromhex(row["password_salt"]))
            return hmac.compare_digest(calculated,bytes.fromhex(row["password_hash"]))
        except (sqlite3.Error,OSError,ValueError,TypeError):
            return False

    def verify_admin_password(self, password: str) -> bool:
        """Richiede la password Admin senza creare una nuova sessione."""
        if not self.configured or not isinstance(password,str):
            return False
        try:
            calculated=self._derive(password,self._salt)
        except (ValueError,TypeError):
            return False
        return hmac.compare_digest(calculated,self._verifier)

    def rate_limited(self,source:str)->bool:
        origin=str(source or "unknown")[:100]
        now=time.monotonic()
        with self._lock:
            return sum(1 for when,address in self._failures
                       if address==origin and now-when<=60)>=24

    def authenticate(self, username: str, password: str,
                     source: str = "local") -> str | None:
        if not self.configured:
            return None
        now = time.monotonic()
        origin = str(source or "unknown")[:100]
        with self._lock:
            while self._failures and now - self._failures[0][0] > 60:
                self._failures.popleft()
            # Blocco solo dell'origine che ha prodotto troppi errori, non
            # dell'account bersaglio o dell'intera installazione.
            if sum(1 for _, address in self._failures if address == origin) >= 24:
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
                self._failures.append((time.monotonic(), origin))
            return None

        token = secrets.token_urlsafe(36)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        role = "admin" if admin_match else "user"
        identity = self.username if admin_match else entry["username"]
        user_id = None if admin_match else entry["id"]
        version = None if admin_match else entry["credenziali_versione"]
        with self._lock:
            self._failures = deque(
                ((when, address) for when, address in self._failures
                 if address != origin), maxlen=3000
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
            with sqlite3.connect(self._db_path, timeout=10, factory=AutoClosingConnection) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("""
                    SELECT username,attivo,credenziali_versione,
                           password_modificata_il,password_cambio_obbligatorio
                    FROM rankup_utenti WHERE id=?
                """,(user_id,)).fetchone()
                policy=db.execute("""SELECT password_max_giorni FROM rankup_politiche_sicurezza
                                     WHERE id=1""").fetchone()
        except (sqlite3.Error,OSError):
            return None
        if not row or not row["attivo"] or row["credenziali_versione"] != version:
            with self._lock:
                self._sessions.pop(digest, None)
            return None
        change_required=bool(row["password_cambio_obbligatorio"])
        try:
            days=policy[0] if policy else 0
            if days and not change_required:
                modified=row["password_modificata_il"]
                if not modified:
                    change_required=True  # data storica sconosciuta, non inventarla
                else:
                    when=datetime.fromisoformat(modified)
                    if when.tzinfo is None:
                        when=when.replace(tzinfo=timezone.utc)
                    change_required=datetime.now(timezone.utc)>=when+timedelta(days=days)
        except (sqlite3.Error,ValueError,TypeError,OverflowError):
            change_required=True  # incertezza sulla policy: blocca operazioni
        return {"username": row["username"], "role": "user", "id": user_id,
                "password_change_required":change_required}

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
