"""Rotte di autenticazione: login, logout, nessuna registrazione pubblica."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.deps import COOKIE_NAME, SessionDep, build_session_cookie
from app.models import User
from app.security import (
    clear_login_attempts,
    create_session,
    destroy_session,
    login_blocked,
    record_login_attempt,
    sign_token,
    verify_password,
)
from app.templating import templates

router = APIRouter()


def _client_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "-"
    return f"{(username or '').strip().lower()}|{host}"


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request) -> HTMLResponse:
    if request.cookies.get(COOKIE_NAME):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "login.html", {"error": "", "username": ""}
    )


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
def login_submit(
    request: Request,
    db: SessionDep,
    username: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    key = _client_key(request, username)
    if login_blocked(db, key):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Troppi tentativi. Riprova tra qualche minuto.",
                "username": username,
            },
            status_code=429,
        )

    user = db.scalar(select(User).where(User.username == username.strip()))
    if user is None or not verify_password(password, user.password_hash):
        record_login_attempt(db, key)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Credenziali non valide.", "username": username},
            status_code=401,
        )

    clear_login_attempts(db, key)
    session = create_session(db, user)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        COOKIE_NAME,
        **build_session_cookie(session.token, sign_token(session.token)),  # type: ignore[arg-type]
    )
    return response


@router.post("/logout", include_in_schema=False)
def logout(request: Request, db: SessionDep) -> RedirectResponse:
    raw = request.cookies.get(COOKIE_NAME) or ""
    token = raw.partition("|")[0]
    if token:
        destroy_session(db, token)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
