"""Punto d'ingresso FastAPI: middleware, rotte, scheduler e healthcheck."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.db import get_sessionmaker, init_db
from app.routers import auth, dashboard, jobs
from app.security import ensure_admin, purge_expired
from app.services.job_engine import set_scheduler

logger = logging.getLogger("b2bimg")


def _purge_session_job() -> None:
    """Task periodico: rimuove sessioni e tentativi di login scaduti."""
    try:
        with get_sessionmaker()() as db:
            removed = purge_expired(db)
        if removed:
            logger.debug("sessioni/tentativi scaduti rimossi: %d", removed)
    except Exception:  # noqa: BLE001
        logger.exception("purge sessioni fallito")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    init_db()
    with get_sessionmaker()() as db:
        ensure_admin(db)

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _purge_session_job,
        trigger="interval",
        minutes=10,
        id="purge-sessions",
        replace_existing=True,
        coalesce=True,
    )
    scheduler.start()
    set_scheduler(scheduler)
    logger.info("b2bimg avviato su %s:%s", settings.app_host, settings.app_port)

    yield

    set_scheduler(None)
    scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    """Crea l'istanza FastAPI (usata anche dai test)."""
    settings = get_settings()
    app = FastAPI(
        title="b2bimg",
        description="Portale interno per download automatizzato immagini prodotti.",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.trusted_hosts != ("*",):
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if "text/html" in response.headers.get("content-type", ""):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(jobs.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        """Healthcheck per Docker/round balancer: verifica DB e scheduler."""
        try:
            with get_sessionmaker()() as db:
                db.execute(text("SELECT 1"))
            return JSONResponse({"status": "ok"})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)

    return app


app = create_app()


def main() -> None:  # pragma: no cover - avvio da riga di comando
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
