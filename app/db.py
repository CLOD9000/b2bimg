"""Connessioni al database (SQLite di default, PostgreSQL via `DATABASE_URL`)."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Base per tutti i modelli ORM."""


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _sqlite_on_connect(dbapi_connection, _record) -> None:  # pragma: no cover
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def get_engine() -> Engine:
    """Crea (una volta sola) l'engine SQLAlchemy."""
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        url = settings.sqlalchemy_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        if url.startswith("sqlite:///"):
            Path(url.replace("sqlite:///", "", 1)).parent.mkdir(
                parents=True, exist_ok=True
            )
        _engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _sqlite_on_connect)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """Dependency FastAPI che fornisce una sessione ORM per richiesta."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """Crea le tabelle se non esistono."""
    from app import models  # noqa: F401  (registrazione modelli)

    Base.metadata.create_all(get_engine())
