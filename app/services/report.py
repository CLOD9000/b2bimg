"""Generazione del report CSV con gli esiti del job."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.models import Job, RowStatus

STATUS_LABELS = {
    RowStatus.PENDING.value: "in attesa",
    RowStatus.SELECTED.value: "selezionata",
    RowStatus.SKIPPED.value: "esclusa (filtro)",
    RowStatus.DOWNLOADED.value: "scaricata",
    RowStatus.NOT_FOUND.value: "non trovata",
    RowStatus.ERROR.value: "errore",
}
STATUS_ICONS = {
    RowStatus.PENDING.value: "...",
    RowStatus.SELECTED.value: "...",
    RowStatus.SKIPPED.value: "-",
    RowStatus.DOWNLOADED.value: "OK",
    RowStatus.NOT_FOUND.value: "NOT_FOUND",
    RowStatus.ERROR.value: "ERROR",
}


def report_rows(job: Job) -> Iterator[list[str]]:
    """Header + righe del report (colonne tecniche + colonne originali del CSV)."""
    technical = [
        "indice",
        "codice",
        "stato",
        "esito",
        "sorgente",
        "risorsa",
        "output",
        "dimensione",
        "tentativi",
        "messaggio",
    ]
    yield technical + list(job.headers)
    for row in sorted(job.rows, key=lambda r: r.idx):
        status = row.status
        yield [
            str(row.idx),
            row.code,
            STATUS_LABELS.get(status, status),
            STATUS_ICONS.get(status, status),
            row.source_used,
            row.resolved_location,
            row.output_path,
            str(row.file_size),
            str(row.attempts),
            row.message,
        ] + [str((row.data or {}).get(h, "")) for h in job.headers]


def build_report_csv(db: Session, job_id: str) -> str:
    """Serializza l'intero report come stringa CSV (UTF-8 con BOM per Excel)."""
    job = db.get(Job, job_id)
    if job is None:
        raise KeyError(f"job {job_id} inesistente")
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for line in report_rows(job):
        writer.writerow(line)
    return "﻿" + buffer.getvalue()
