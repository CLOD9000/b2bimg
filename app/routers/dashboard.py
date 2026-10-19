"""Dashboard: elenco job e creazione di un nuovo job da CSV."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select

from app.config import get_settings
from app.deps import SessionDep, UserDep
from app.models import Job, JobRow, JobStatus, RowStatus
from app.pathutil import sanitize_filename
from app.services.csv_service import CSVValidationError, parse_file
from app.templating import templates

router = APIRouter()

_CODE_HINTS = ("codice", "code", "sku", "articolo", "article", "ean", "barcode", "riferimento")


def _guess_code_field(headers: list[str]) -> str:
    lowered = {h.lower(): h for h in headers}
    for hint in _CODE_HINTS:
        for key, original in lowered.items():
            if hint in key:
                return original
    return headers[0] if headers else ""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, db: SessionDep, user: UserDep) -> HTMLResponse:
    jobs = db.scalars(select(Job).order_by(desc(Job.created_at))).all()
    rows_stats = dict(
        db.execute(
            select(JobRow.job_id, func.count())
            .where(JobRow.status == RowStatus.DOWNLOADED.value)
            .group_by(JobRow.job_id)
        ).all()
    )
    items = [
        {
            "job": job,
            "downloaded": rows_stats.get(job.id, 0),
        }
        for job in jobs
    ]
    return templates.TemplateResponse(
        request, "dashboard.html", {"user": user, "items": items}
    )


@router.get("/jobs/new", response_class=HTMLResponse, include_in_schema=False)
def new_job_page(request: Request, db: SessionDep, user: UserDep) -> HTMLResponse:
    return templates.TemplateResponse(request, "job_new.html", {"user": user, "error": ""})


@router.post("/jobs/upload", response_class=HTMLResponse, include_in_schema=False)
async def upload_csv(
    request: Request,
    db: SessionDep,
    user: UserDep,
    file: UploadFile = File(...),
    name: str = Form(""),
) -> HTMLResponse:
    settings = get_settings()
    limit = settings.max_upload_mb * 1024 * 1024
    raw = bytearray()
    while chunk := await file.read(64 * 1024):
        raw.extend(chunk)
        if len(raw) > limit:
            return templates.TemplateResponse(
                request,
                "job_new.html",
                {"user": user, "error": f"File oltre {settings.max_upload_mb} MB."},
                status_code=413,
            )
    try:
        parsed = parse_file(file.filename or "prodotti.csv", bytes(raw))
    except CSVValidationError as exc:
        return templates.TemplateResponse(
            request,
            "job_new.html",
            {"user": user, "error": str(exc)},
            status_code=400,
        )

    job_id = uuid.uuid4().hex
    original = sanitize_filename(file.filename or "prodotti.csv", default="prodotti.csv")
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    (settings.uploads_dir / f"{job_id}.csv").write_bytes(bytes(raw))

    job = Job(
        id=job_id,
        name=(name or "").strip() or Path(original).stem,
        source_filename=original,
        encoding=parsed.encoding,
        delimiter=parsed.delimiter,
        headers=parsed.headers,
        code_field=_guess_code_field(parsed.headers),
        status=JobStatus.DRAFT.value,
    )
    db.add(job)
    db.flush()
    for idx, data in enumerate(parsed.rows):
        db.add(
            JobRow(
                job_id=job_id,
                idx=idx,
                data=data,
                code=str(data.get(job.code_field, "")).strip(),
                status=RowStatus.PENDING.value,
                selected=True,
            )
        )
    db.commit()
    return RedirectResponse(f"/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)
