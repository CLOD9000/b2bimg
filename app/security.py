"""Sicurezza: hash password, sessioni lato server, rate limiting del login.

Nessun segreto è hardcoded: le chiavi arrivano solo da variabili d'ambiente.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import LoginAttempt, User, UserSession, utcnow

_PBKDF2_ITERATIONS = 390_000
_SESSION_TTL = timedelta()


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 con salt casuale (formato: pbkdf2_sha256$iter$salt$hash)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verifica in tempo costante una password contro l'hash salvato."""
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def sign_token(token: str) -> str:
    """Firma HMAC del token di sessione per rifiutare rapidamente token forgiati."""
    key = get_settings().secret_key.encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()[:32]


def verify_signature(token: str, signature: str) -> bool:
    return hmac.compare_digest(sign_token(token), signature)


@dataclass(frozen=True)
class AuthContext:
    user: User
    session_token: str


def create_session(db: Session, user: User) -> UserSession:
    """Crea una sessione e restituisce il record (il token è in `.token`)."""
    settings = get_settings()
    session = UserSession(
        token=secrets.token_urlsafe(32),
        user_id=user.id,
        expires_at=utcnow() + timedelta(minutes=settings.session_ttl_minutes),
    )
    db.add(session)
    db.commit()
    return session


def get_valid_session(db: Session, token: str) -> UserSession | None:
    """Restituisce la sessione se valida e non scaduta, altrimenti None."""
    if not token:
        return None
    record = db.get(UserSession, token)
    if record is None:
        return None
    if record.expires_at < utcnow():
        db.delete(record)
        db.commit()
        return None
    return record


def touch_session(db: Session, record: UserSession) -> None:
    """Rinnova lo scadenzamento (sliding expiration)."""
    settings = get_settings()
    new_expiry = utcnow() + timedelta(minutes=settings.session_ttl_minutes)
    if (new_expiry - record.expires_at).total_seconds() > 60:
        record.expires_at = new_expiry
        db.commit()


def destroy_session(db: Session, token: str) -> None:
    db.execute(delete(UserSession).where(UserSession.token == token))
    db.commit()


def purge_expired(db: Session) -> int:
    """Rimuove sessioni e tentativi scaduti (chiamato dallo scheduler)."""
    now = utcnow()
    result = db.execute(delete(UserSession).where(UserSession.expires_at < now))
    window = now - timedelta(seconds=get_settings().login_window_seconds)
    db.execute(delete(LoginAttempt).where(LoginAttempt.at < window))
    db.commit()
    return int(result.rowcount or 0)


# --- Rate limiting del login -------------------------------------------------


def login_blocked(db: Session, key: str) -> bool:
    """True se la chiave (username+IP) ha superato il numero di tentativi."""
    settings = get_settings()
    window_start = utcnow() - timedelta(seconds=settings.login_window_seconds)
    count = db.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(LoginAttempt.key == key, LoginAttempt.at >= window_start)
    )
    return bool(count and count >= settings.login_max_attempts)


def record_login_attempt(db: Session, key: str) -> None:
    db.add(LoginAttempt(key=key))
    db.commit()


def clear_login_attempts(db: Session, key: str) -> None:
    db.execute(delete(LoginAttempt).where(LoginAttempt.key == key))
    db.commit()


def ensure_admin(db: Session) -> User:
    """Crea l'utente admin al primo avvio se non esiste."""
    settings = get_settings()
    user = db.scalar(select(User).where(User.username == settings.admin_username))
    if user is not None:
        return user
    password = settings.admin_password
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    user = User(username=settings.admin_username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    if generated:
        import logging

        logging.getLogger("b2bimg").warning(
            "ADMIN_PASSWORD non impostata: creato password temporanea '%s' "
            "per l'utente '%s'. Impostala nel .env e riavvia.",
            password,
            settings.admin_username,
        )
    return user
