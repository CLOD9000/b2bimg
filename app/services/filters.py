"""Filtri sulle righe importate: valori distinti e applicazione della selezione."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Job, JobRow, RowStatus


class FilterError(ValueError):
    """Errore di filtro (mostrato all'utente)."""


def _json_path(field: str) -> str:
    """Costruisce un path JSON sicuro per un nome campo (già validato sui header)."""
    escaped = field.replace('"', '""')
    return f'$."{escaped}"'


def validate_field(job: Job, field: str) -> str:
    """Verifica che il campo esista tra gli header del CSV del job."""
    if field not in (job.headers or []):
        raise FilterError(f"Campo non presente nei header del CSV: {field!r}")
    return field


def distinct_values(
    db: Session,
    job_id: str,
    field: str,
    search: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Valori distinti di `field` con conteggio righe, per l'autocomplete."""
    path = _json_path(field)
    expr = func.json_extract(JobRow.data, path)
    stmt = (
        select(expr, func.count())
        .where(JobRow.job_id == job_id)
        .group_by(expr)
        .order_by(func.count().desc(), expr)
    )
    rows = db.execute(stmt).all()
    needle = (search or "").strip().lower()
    out: list[dict[str, Any]] = []
    for value, count in rows:
        text = "" if value is None else str(value)
        if needle and needle not in text.lower():
            continue
        out.append({"value": text, "count": int(count)})
        if len(out) >= limit:
            break
    return out


def apply_filter(
    db: Session,
    job: Job,
    field: str,
    values: Iterable[str],
) -> int:
    """Imposta `selected` sulle righe del job in base ai valori scelti.

    Selezione vuota = nessun filtro (tutte le righe selezionate).
    Restituisce il numero di righe selezionate.
    """
    validate_field(job, field)
    if job.status == "running":
        raise FilterError("Impossibile filtrare mentre il job è in esecuzione.")

    selected_values = {str(v) for v in values if str(v).strip()}
    count = 0
    if not selected_values:
        for row in job.rows:
            row.selected = True
            if row.status == RowStatus.SKIPPED.value:
                row.status = RowStatus.PENDING.value
            count += 1
    else:
        for row in job.rows:
            value = "" if row.data.get(field) is None else str(row.data.get(field))
            row.selected = value in selected_values
            if not row.selected:
                row.status = RowStatus.SKIPPED.value
            elif row.status == RowStatus.SKIPPED.value:
                row.status = RowStatus.PENDING.value
            if row.selected:
                count += 1

    job.filter_field = field
    job.filter_values = sorted(selected_values)
    db.commit()
    return count


def selected_count(db: Session, job_id: str) -> int:
    """Numero di righe attualmente selezionate per il job."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(JobRow)
            .where(JobRow.job_id == job_id, JobRow.selected.is_(True))
        )
        or 0
    )


def reset_rows_for_rerun(db: Session, job_id: str, only_failed: bool = True) -> int:
    """Rimette in attesa le righe fallite (o tutte quelle selezionate)."""
    if only_failed:
        stmt = (
            update(JobRow)
            .where(
                JobRow.job_id == job_id,
                JobRow.status.in_([RowStatus.ERROR.value, RowStatus.NOT_FOUND.value]),
                JobRow.selected.is_(True),
            )
            .values(status=RowStatus.PENDING.value, attempts=0, message="")
        )
    else:
        stmt = (
            update(JobRow)
            .where(JobRow.job_id == job_id, JobRow.selected.is_(True))
            .values(status=RowStatus.PENDING.value, attempts=0, message="")
        )
    result = db.execute(stmt)
    db.commit()
    return int(result.rowcount or 0)
