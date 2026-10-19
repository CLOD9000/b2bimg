"""Fixture condivise: env di test, app FastAPI, client autenticato."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

TEST_DATA_DIR = Path(__file__).parent / "_work"

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin-test-pass")
os.environ.setdefault("SOURCE_RATE_LIMIT_PER_MIN", "10000")
os.environ.setdefault("SOURCE_MAX_RETRIES", "2")
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session", autouse=True)
def _prepare_env() -> None:
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
    from app.config import get_settings

    get_settings.cache_clear()
    get_settings()
    from app.db import get_engine, init_db

    get_engine()  # crea l'engine con il DATA_DIR di test
    init_db()  # crea le tabelle senza dipendere dal lifespan


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Mantiene il cache delle impostazioni coerente con l'env del test."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def app():
    from app.main import create_app

    application = create_app()
    yield application
    from app.services import job_engine

    job_engine.set_scheduler(None)


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture()
def auth_client(client):
    """Client già autenticato come admin."""
    response = client.post(
        "/login",
        data={"username": "admin", "password": "admin-test-pass"},
        follow_redirects=False,
    )
    assert response.status_code in (303, 307), response.text
    yield client
    client.post("/logout")


@pytest.fixture(scope="session")
def db_session_factory():
    from app.db import get_sessionmaker

    return get_sessionmaker()


@pytest.fixture()
def csv_bytes() -> bytes:
    return (
        b"AB-1001,Tastiera meccanica,periferiche\n"
        b"AB-1002,Mouse wireless,periferiche\n"
        b"AB-2001,Monitor 27,monitor\n"
    )


@pytest.fixture()
def uploaded_job(auth_client, csv_bytes):
    """Crea un job da CSV e ne restituisce l'id (e il client autenticato)."""
    response = auth_client.post(
        "/jobs/upload",
        files={"file": ("prodotti.csv", csv_bytes, "text/csv")},
        data={"name": "Listino test"},
        follow_redirects=False,
    )
    assert response.status_code in (303, 307), response.text
    job_id = response.headers["location"].rstrip("/").split("/")[-1]
    return job_id
