"""Configurazione dell'applicazione letta da variabili d'ambiente / `.env`."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Impostazioni runtime (tutte le chiavi sono documentate in `.env.example`)."""

    # --- Server ---
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"
    secret_key: str = ""
    trusted_hosts: tuple[str, ...] = field(default_factory=lambda: ("*",))

    # --- Percorsi ---
    data_dir: Path = Path("data")

    # --- Auth ---
    admin_username: str = "admin"
    admin_password: str = ""
    session_ttl_minutes: int = 480
    session_cookie_secure: bool = False
    login_max_attempts: int = 5
    login_window_seconds: int = 300

    # --- Upload / limiti ---
    max_upload_mb: int = 20
    max_image_mb: int = 25

    # --- Sorgenti ---
    source_rate_limit_per_min: int = 30
    source_default_timeout: float = 15.0
    source_max_retries: int = 3

    # --- Job ---
    job_row_batch: int = 100

    # --- Elasticità futura ---
    database_url: str = ""

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "b2bimg.sqlite3"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.db_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Restituisce le impostazioni (singleton con cache)."""
    secret = os.environ.get("SECRET_KEY", "").strip()
    if not secret:
        secret = secrets.token_urlsafe(32)
    hosts = tuple(
        h.strip() for h in os.environ.get("TRUSTED_HOST", "*").split(",") if h.strip()
    )
    return Settings(
        app_host=os.environ.get("APP_HOST", "127.0.0.1").strip(),
        app_port=_int("APP_PORT", 8000),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        secret_key=secret,
        trusted_hosts=hosts or ("*",),
        data_dir=Path(os.environ.get("DATA_DIR", "data")).expanduser(),
        admin_username=os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin",
        admin_password=os.environ.get("ADMIN_PASSWORD", "").strip(),
        session_ttl_minutes=_int("SESSION_TTL_MINUTES", 480),
        session_cookie_secure=_bool("SESSION_COOKIE_SECURE", False),
        login_max_attempts=_int("LOGIN_MAX_ATTEMPTS", 5),
        login_window_seconds=_int("LOGIN_WINDOW_SECONDS", 300),
        max_upload_mb=_int("MAX_UPLOAD_MB", 20),
        max_image_mb=_int("MAX_IMAGE_MB", 25),
        source_rate_limit_per_min=_int("SOURCE_RATE_LIMIT_PER_MIN", 30),
        source_default_timeout=_float("SOURCE_DEFAULT_TIMEOUT", 15.0),
        source_max_retries=_int("SOURCE_MAX_RETRIES", 3),
        job_row_batch=_int("JOB_ROW_BATCH", 100),
        database_url=os.environ.get("DATABASE_URL", "").strip(),
    )
