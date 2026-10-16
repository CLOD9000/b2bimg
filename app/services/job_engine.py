"""Job engine: esecuzione asincrona dei download con retry, rate limit e log per riga."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Job, JobRow, JobStatus, RowStatus, utcnow
from app.pathutil import sanitize_filename
from app.ratelimit import RateLimiter
from plugins import (
    FetchResult,
    NotFoundError,
    PluginError,
    build_destination,
    build_source,
    format_template,
)

logger = logging.getLogger("b2bimg.job")

_LIMITERS: dict[str, RateLimiter] = {}
_SCHEDULER: Any = None  # impostato da app.main (APScheduler)

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
}


def set_scheduler(scheduler: Any) -> None:
    """Collega lo scheduler APScheduler usato per avviare i job in background."""
    global _SCHEDULER
    _SCHEDULER = scheduler


def _limiter_for(source_type: str, rate_limited: bool) -> RateLimiter | None:
    settings = get_settings()
    if not rate_limited or settings.source_rate_limit_per_min <= 0:
        return None
    if source_type not in _LIMITERS:
        _LIMITERS[source_type] = RateLimiter(settings.source_rate_limit_per_min)
    return _LIMITERS[source_type]


# --- utilità -----------------------------------------------------------------


def job_counts(db: Session, job_id: str) -> dict[str, int]:
    """Conteggi per stato senza caricare tutte le righe in memoria."""
    counts = {status.value: 0 for status in RowStatus}
    rows = db.execute(
        select(JobRow.status, func.count())
        .where(JobRow.job_id == job_id)
        .group_by(JobRow.status)
    ).all()
    for status, count in rows:
        counts[status] = int(count)
    return counts


def progress_payload(db: Session, job_id: str) -> dict[str, Any]:
    """Payload JSON per la progress bar e l'aggiornamento HTMX."""
    job = db.get(Job, job_id)
    counts = job_counts(db, job_id)
    total = sum(counts.values())
    done = (
        counts[RowStatus.DOWNLOADED.value]
        + counts[RowStatus.NOT_FOUND.value]
        + counts[RowStatus.ERROR.value]
    )
    skipped = counts[RowStatus.SKIPPED.value]
    processable = max(total - skipped, 0)
    if processable:
        percent = int(round(done * 100 / processable))
    else:
        completed = bool(job and job.status == JobStatus.COMPLETED.value)
        percent = 100 if completed else 0
    return {
        "job_id": job_id,
        "status": job.status if job else "missing",
        "counts": counts,
        "total": total,
        "done": done,
        "processable": processable,
        "percent": percent,
        "error": job.error if job else "",
    }


def validate_job_config(job: Job) -> list[str]:
    """Controlli minimi prima dell'avvio: restituisce la lista degli errori."""
    errors: list[str] = []
    if not job.source_type:
        errors.append("Seleziona una sorgente immagini.")
    if not job.dest_type:
        errors.append("Seleziona una destinazione.")
    if not job.headers:
        errors.append("Il CSV non ha header.")
    elif job.code_field and job.code_field not in job.headers:
        errors.append(f"Il campo codice '{job.code_field}' non è presente negli header.")
    if not job.filename_template.strip():
        errors.append("Il template del nome file è obbligatorio.")
    if not any(r.selected and r.status == RowStatus.PENDING.value for r in job.rows):
        errors.append("Nessuna riga da processare (controlla il filtro).")
    return errors


def render_filename(job: Job, row: JobRow, result: FetchResult) -> str:
    """Costruisce e sanifica il nome file di output dal template."""
    values: dict[str, str] = {
        "codice": row.code,
        "code": row.code,
        "indice": str(row.idx),
    }
    for key, value in (row.data or {}).items():
        values[str(key)] = "" if value is None else str(value)

    raw = format_template(job.filename_template, values, context="nome file")
    name = sanitize_filename(raw, default=f"{row.code or row.idx}")
    suffix = Path(name).suffix.lower()
    if not suffix:
        ext = _extension_for(result)
        name = f"{name}{ext}"
    return name


def _extension_for(result: FetchResult) -> str:
    if result.content_type:
        ext = _EXT_BY_CONTENT_TYPE.get(result.content_type.split(";")[0].strip().lower())
        if ext:
            return ext
    suffix = Path(result.location.split("?")[0]).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def _download(url: str, source: Any, max_bytes: int) -> bytes:
    """Scarica un URL restituito da una sorgente che non ha già il body."""
    source.acquire_slot()
    try:
        with (
            httpx.Client(timeout=source.timeout, follow_redirects=True) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise NotFoundError(f"download fallito ({response.status_code}) da {url}")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    from plugins.base import TooLargeError

                    raise TooLargeError(f"file oltre {max_bytes} byte: {url}")
            return bytes(data)
    except httpx.TimeoutException as exc:
        raise plugins_network_error(f"timeout durante il download da {url}") from exc
    except httpx.TransportError as exc:
        raise plugins_network_error(f"errore di rete durante il download da {url}: {exc}") from exc

def plugins_network_error(message: str) -> PluginError:
    from plugins.base import NetworkError

    return NetworkError(message)


def _read_local(path_str: str, max_bytes: int) -> bytes:
    path = Path(path_str)
    if not path.is_file():
        raise NotFoundError(f"file locale assente: {path}")
    size = path.stat().st_size
    if size > max_bytes:
        from plugins.base import TooLargeError

        raise TooLargeError(f"file oltre {max_bytes} byte: {path}")
    return path.read_bytes()


# --- esecuzione --------------------------------------------------------------


def start_job(job_id: str) -> None:
    """Pianifica l'esecuzione del job in background (APScheduler)."""
    if _SCHEDULER is None:
        raise RuntimeError("scheduler non inizializzato")
    _SCHEDULER.add_job(
        run_job,
        trigger="date",
        id=f"job:{job_id}",
        args=[job_id],
        replace_existing=False,
        coalesce=True,
        misfire_grace_time=30,
        max_instances=1,
    )


def stop_job(job_id: str) -> None:
    if _SCHEDULER is not None:
        with contextlib.suppress(Exception):
            _SCHEDULER.remove_job(f"job:{job_id}")


def run_job(job_id: str) -> None:
    """Worker: processa tutte le righe selezionate e in attesa del job."""
    session_factory = get_sessionmaker()
    db: Session = session_factory()
    source = destination = None
    try:
        job = db.get(Job, job_id)
        if job is None:
            logger.warning("job %s non trovato", job_id)
            return
        if job.status in {JobStatus.CANCELLED.value, JobStatus.RUNNING.value}:
            return
        settings = get_settings()
        job.status = JobStatus.RUNNING.value
        job.started_at = job.started_at or utcnow()
        job.error = ""
        db.commit()

        errors = validate_job_config(job)
        if errors:
            raise RuntimeError("; ".join(errors))

        source = build_source(
            job.source_type,
            job.source_config,
            rate_limiter=_limiter_for(job.source_type, _source_rate_limited(job.source_type)),
            default_timeout=settings.source_default_timeout,
            max_bytes=settings.max_image_mb * 1024 * 1024,
        )
        destination = build_destination(
            job.dest_type,
            job.dest_config,
            default_timeout=settings.source_default_timeout * 2,
            max_bytes=settings.max_image_mb * 1024 * 1024,
        )

        pending = [
            row
            for row in job.rows
            if row.selected and row.status == RowStatus.PENDING.value
        ]
        logger.info("job %s: %d righe da processare", job_id, len(pending))
        for row in pending:
            _process_row(db, job, row, source, destination, settings)
            cancelled = db.get(Job, job_id)
            if cancelled is not None and cancelled.status == JobStatus.CANCELLED.value:
                logger.info("job %s annullato dall'utente", job_id)
                return

        job.status = JobStatus.COMPLETED.value
        job.finished_at = utcnow()
        db.commit()
        counts = job_counts(db, job_id)
        logger.info(
            "job %s completato: %d scaricate, %d non trovate, %d errori",
            job_id,
            counts[RowStatus.DOWNLOADED.value],
            counts[RowStatus.NOT_FOUND.value],
            counts[RowStatus.ERROR.value],
        )
    except Exception as exc:  # noqa: BLE001 - cattura errore a livello di job
        logger.exception("job %s fallito", job_id)
        job = db.get(Job, job_id)
        if job is not None:
            job.status = JobStatus.FAILED.value
            job.error = str(exc)[:2000]
            job.finished_at = utcnow()
            db.commit()
    finally:
        for plugin in (source, destination):
            close = getattr(plugin, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        db.close()


def _source_rate_limited(source_type: str) -> bool:
    from plugins.registry import get_source_class

    try:
        return bool(get_source_class(source_type).rate_limited)
    except KeyError:
        return True


def _process_row(
    db: Session,
    job: Job,
    row: JobRow,
    source: Any,
    destination: Any,
    settings: Any,
) -> None:
    """Scarica e salva una singola riga con retry (max SOURCE_MAX_RETRIES)."""
    code = (row.code or "").strip()
    if not code:
        row.status = RowStatus.ERROR.value
        row.message = "codice prodotto mancante"
        row.attempts = 1
        db.commit()
        return

    started = time.monotonic()
    max_attempts = max(1, int(settings.source_max_retries))
    result: FetchResult | None = None
    last_error: PluginError | None = None

    for attempt in range(1, max_attempts + 1):
        row.attempts = attempt
        try:
            result = source.fetch(code)
            data = result.content
            if data is None:
                location = result.location
                if location.startswith(("http://", "https://")):
                    data = _download(location, source, settings.max_image_mb * 1024 * 1024)
                else:
                    data = _read_local(location, settings.max_image_mb * 1024 * 1024)
            filename = render_filename(job, row, result)
            put = destination.put(filename, data, conflict=job.conflict_strategy)
            elapsed = time.monotonic() - started
            row.status = RowStatus.DOWNLOADED.value
            row.source_used = job.source_type
            row.resolved_location = result.location[:1024]
            row.output_path = put.path[:1024]
            row.file_size = int(put.size or len(data))
            row.message = (
                f"{put.action} in {elapsed:.1f}s ({_human_size(row.file_size)})"
            )
            db.commit()
            logger.info(
                "job=%s idx=%s codice=%s stato=scaricata sorgente=%s out=%s bytes=%s",
                job.id, row.idx, code, job.source_type, put.path, row.file_size,
            )
            return
        except PluginError as exc:
            last_error = exc
            if not exc.retryable or attempt >= max_attempts:
                break
            wait = min(0.5 * (2 ** (attempt - 1)), 5.0)
            logger.debug(
                "job=%s codice=%s tentativo %d/%d fallito (%s): %s",
                job.id, code, attempt, max_attempts, exc.code, exc.message,
            )
            time.sleep(wait)
        except Exception as exc:  # noqa: BLE001 - bug imprevisto nel plugin
            last_error = PluginError(str(exc), code="error")
            if attempt >= max_attempts:
                break
            time.sleep(min(0.5 * (2 ** (attempt - 1)), 5.0))

    error = last_error or PluginError("errore sconosciuto")
    row.status = (
        RowStatus.NOT_FOUND.value
        if isinstance(error, NotFoundError)
        else RowStatus.ERROR.value
    )
    row.source_used = job.source_type
    row.message = f"{error.message} [tentativi: {row.attempts}]"[:2000]
    db.commit()
    logger.warning(
        "job=%s idx=%s codice=%s stato=%s sorgente=%s motivo=%s",
        job.id, row.idx, code, row.status, job.source_type, row.message,
    )


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} kB"
    return f"{size / (1024 * 1024):.1f} MB"


def iter_rows(db: Session, job_id: str, status: str | None = None) -> Iterable[JobRow]:
    """Itera le righe di un job con filtro opzionale per stato."""
    stmt = select(JobRow).where(JobRow.job_id == job_id).order_by(JobRow.idx)
    if status:
        stmt = stmt.where(JobRow.status == status)
    return db.scalars(stmt).all()
