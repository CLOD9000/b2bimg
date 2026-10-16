"""Dependency FastAPI per l'autenticazione obbligatoria."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User, UserSession
from app.security import get_valid_session, touch_session, verify_signature

COOKIE_NAME = "b2bimg_session"

SessionDep = Annotated[Session, Depends(get_db)]


def _extract_token(request: Request) -> str | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw or "|" not in raw:
        return None
    token, _, signature = raw.partition("|")
    if not token or not verify_signature(token, signature):
        return None
    return token


def current_user(request: Request, db: SessionDep) -> User:
    """Restituisce l'utente loggato oppure interrompe con 401/403.

    Ogni rotta dell'app (tranne `/login`, `/logout`, `/healthz` e `/static`)
    passa da questa dependency: l'accesso è sempre autenticato.
    """
    token = _extract_token(request)
    record: UserSession | None = get_valid_session(db, token or "")
    if record is None:
        if request.headers.get("HX-Request") or request.headers.get("Sec-Fetch-Dest") == "empty":
            # Per le richieste HTMX renviamo 401: htmx intercetta e dirotta.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sessione scaduta",
                headers={"HX-Redirect": "/login"},
            )
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Non autenticato",
            headers={"Location": "/login"},
        )
    touch_session(db, record)
    return record.user


UserDep = Annotated[User, Depends(current_user)]


def build_session_cookie(token: str, signature: str) -> dict[str, object]:
    """Parametri comuni del cookie di sessione (httponly, samesite, scadenza)."""
    settings = get_settings()
    return {
        "value": f"{token}|{signature}",
        "max_age": settings.session_ttl_minutes * 60,
        "httponly": True,
        "samesite": "lax",
        "secure": settings.session_cookie_secure,
        "path": "/",
    }
