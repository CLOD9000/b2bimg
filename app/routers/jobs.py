"""Rotte dei job: filtro, configurazione sorgente/destinazione, avvio, report."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from app.deps import SessionDep, UserDep
from app.models import Job, JobRow, JobStatus
from app.services import filters as filters_service
from app.services import job_engine
from app.services.report import build_report_csv
from app.templating import templates
from plugins import (
    available_destinations,
    available_sources,
    get_destination_class,
    get_source_class,
)

router = APIRouter()

PAGE_SIZE = 50


def _get_job(db, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job inesistente")
    return job


def _job_context(db, job: Job) -> dict:
    counts = job_engine.job_counts(db, job.id)
    return {
        "job": job,
        "counts": counts,
        "headers": job.headers,
        "sources": available_sources(),
        "destinations": available_destinations(),
        "conflict_options": [
            ("rename", "Rinomina (file (1).ext)"),
            ("overwrite", "Sovrascrivi"),
            ("skip", "Salta se esistente"),
        ],
    }


def _run_context(db, job: Job, row_status: str = "", page: int = 1) -> dict:
    """Contesto completo per il pannello di esecuzione (progress + tabella righe)."""
    rows, total = _rows_query(db, job.id, row_status, page)
    payload = job_engine.progress_payload(db, job.id)
    return {
        "job": job,
        "counts": payload["counts"],
        "progress": payload,
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "row_status": row_status,
        "pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        "start_errors": [],
        "force_poll": False,
    }


# --- dettaglio job -----------------------------------------------------------


@router.get("/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
def job_page(
    request: Request,
    db: SessionDep,
    user: UserDep,
    job_id: str,
    row_status: str = Query(""),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    job = _get_job(db, job_id)
    rows, total = _rows_query(db, job_id, row_status, page)
    initial_field = job.filter_field or (job.headers[0] if job.headers else "")
    ctx = _job_context(db, job)
    ctx.update(
        {
            "user": user,
            "row_status": row_status,
            "page": page,
            "rows": rows,
            "total": total,
            "page_size": PAGE_SIZE,
            "pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
            "progress": job_engine.progress_payload(db, job_id),
            "start_errors": [],
            "filter_error": "",
            "selected_field": initial_field,
            "filter_values": (
                filters_service.distinct_values(db, job.id, initial_field, "", 200)
                if initial_field
                else []
            ),
        }
    )
    return templates.TemplateResponse(request, "job_detail.html", ctx)


def _rows_query(db, job_id: str, row_status: str, page: int):
    stmt = select(JobRow).where(JobRow.job_id == job_id)
    total_stmt = select(func.count()).select_from(JobRow).where(JobRow.job_id == job_id)
    if row_status:
        stmt = stmt.where(JobRow.status == row_status)
        total_stmt = total_stmt.where(JobRow.status == row_status)
    total = int(db.scalar(total_stmt) or 0)
    rows = (
        db.scalars(
            stmt.order_by(JobRow.idx).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        ).all()
    )
    return rows, total


@router.get("/jobs/{job_id}/rows", response_class=HTMLResponse, include_in_schema=False)
def job_rows_partial(
    request: Request,
    db: SessionDep,
    user: UserDep,
    job_id: str,
    row_status: str = Query(""),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    job = _get_job(db, job_id)
    rows, total = _rows_query(db, job_id, row_status, page)
    return templates.TemplateResponse(
        request,
        "partials/rows_table.html",
        {
            "job": job,
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "row_status": row_status,
            "pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        },
    )


# --- filtro ------------------------------------------------------------------


@router.get("/jobs/{job_id}/filter/values", response_class=HTMLResponse, include_in_schema=False)
def filter_values(
    request: Request,
    db: SessionDep,
    user: UserDep,
    job_id: str,
    field: str = Query(""),
    search: str = Query(""),
) -> HTMLResponse:
    job = _get_job(db, job_id)
    if not field or field not in job.headers:
        values: list[dict] = []
    else:
        values = filters_service.distinct_values(db, job.id, field, search)
    return templates.TemplateResponse(
        request,
        "partials/filter_values.html",
        {"job": job, "field": field, "values": values, "search": search},
    )


@router.post("/jobs/{job_id}/filter", response_class=HTMLResponse, include_in_schema=False)
def apply_filter(
    request: Request,
    db: SessionDep,
    user: UserDep,
    job_id: str,
    field: str = Form(...),
    values: list[str] = Form(default=[]),
) -> HTMLResponse:
    job = _get_job(db, job_id)
    try:
        selected = filters_service.apply_filter(db, job, field, values)
        error = ""
    except filters_service.FilterError as exc:
        selected = 0
        error = str(exc)
        field = job.filter_field or field
    ctx = _job_context(db, job)
    ctx.update(
        {
            "user": user,
            "filter_error": error,
            "filter_selected": selected,
            "selected_field": field,
            "filter_values": (
                filters_service.distinct_values(db, job.id, field, "", 200) if field else []
            ),
        }
    )
    return templates.TemplateResponse(request, "partials/filter_done.html", ctx)


# --- configurazione ----------------------------------------------------------


@router.get("/jobs/{job_id}/config/form", response_class=HTMLResponse, include_in_schema=False)
def config_form(
    request: Request,
    db: SessionDep,
    user: UserDep,
    job_id: str,
    kind: str = Query("source"),
    value: str = Query(""),
) -> HTMLResponse:
    job = _get_job(db, job_id)
    if kind == "source":
        current = job.source_config if job.source_type == value else {}
        try:
            spec = get_source_class(value) if value else None
        except KeyError:
            spec = None
    else:
        current = job.dest_config if job.dest_type == value else {}
        try:
            spec = get_destination_class(value) if value else None
        except KeyError:
            spec = None
    return templates.TemplateResponse(
        request,
        "partials/config_fields.html",
        {"job": job, "kind": kind, "spec": spec, "current": current},
    )


@router.post("/jobs/{job_id}/config", response_class=HTMLResponse, include_in_schema=False)
async def save_config(
    request: Request,
    db: SessionDep,
    user: UserDep,
    job_id: str,
) -> HTMLResponse:
    job = _get_job(db, job_id)
    if job.status == JobStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail="Job in esecuzione")

    form = await request.form()

    def _text(key: str, default: str = "") -> str:
        value = form.get(key, default)
        return value if isinstance(value, str) else default

    source_type = _text("source_type")
    dest_type = _text("dest_type")
    job.source_type = source_type
    job.dest_type = dest_type
    job.filename_template = _text("filename_template", "{codice}.jpg").strip() or "{codice}.jpg"
    conflict = _text("conflict", "rename")
    job.conflict_strategy = conflict if conflict in {"rename", "overwrite", "skip"} else "rename"
    code_field = _text("code_field")
    if code_field in job.headers:
        job.code_field = code_field
        for row in job.rows:
            row.code = str((row.data or {}).get(code_field, "")).strip()

    job.source_config = {
        k[4:]: str(v)
        for k, v in form.items()
        if k.startswith("src_") and isinstance(v, str)
    }
    job.dest_config = {
        k[4:]: str(v)
        for k, v in form.items()
        if k.startswith("dst_") and isinstance(v, str)
    }

    errors = job_engine.validate_job_config(job)
    job.status = JobStatus.READY.value if not errors else JobStatus.DRAFT.value
    db.commit()

    ctx = _job_context(db, job)
    ctx.update({"user": user, "config_errors": errors, "saved": True})
    return templates.TemplateResponse(request, "partials/config_saved.html", ctx)


# --- avvio / esecuzione ------------------------------------------------------


@router.post("/jobs/{job_id}/start", response_class=HTMLResponse, include_in_schema=False)
def start(
    request: Request,
    db: SessionDep,
    user: UserDep,
    job_id: str,
) -> HTMLResponse:
    job = _get_job(db, job_id)
    if job.status == JobStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail="Job già in esecuzione")
    errors = job_engine.validate_job_config(job)
    if errors:
        ctx = _run_context(db, job, "")
        ctx.update({"user": user, "start_errors": errors})
        return templates.TemplateResponse(request, "partials/run_panel.html", ctx)
    job.status = JobStatus.READY.value
    db.commit()
    try:
        job_engine.start_job(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    job_engine.wait_for_start(job_id)
    db.expire(job)
    ctx = _run_context(db, job, "")
    ctx.update({"user": user, "start_errors": [], "force_poll": True})
    return templates.TemplateResponse(request, "partials/run_panel.html", ctx)


@router.get("/jobs/{job_id}/run", response_class=HTMLResponse, include_in_schema=False)
def run_panel(
    request: Request,
    db: SessionDep,
    user: UserDep,
    job_id: str,
    row_status: str = Query(""),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    job = _get_job(db, job_id)
    ctx = _run_context(db, job, row_status, page)
    ctx.update({"user": user})
    return templates.TemplateResponse(request, "partials/run_panel.html", ctx)


@router.post("/jobs/{job_id}/retry-failed", response_class=HTMLResponse, include_in_schema=False)
def retry_failed(
    request: Request,
    db: SessionDep,
    user: UserDep,
    job_id: str,
) -> HTMLResponse:
    job = _get_job(db, job_id)
    if job.status == JobStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail="Job in esecuzione")
    filters_service.reset_rows_for_rerun(db, job_id, only_failed=True)
    errors = job_engine.validate_job_config(job)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    job.status = JobStatus.READY.value
    db.commit()
    job_engine.start_job(job_id)
    job_engine.wait_for_start(job_id)
    db.expire(job)
    ctx = _run_context(db, job, "")
    ctx.update({"user": user, "start_errors": [], "force_poll": True})
    return templates.TemplateResponse(request, "partials/run_panel.html", ctx)


@router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse, include_in_schema=False)
def cancel_job(
    request: Request,
    db: SessionDep,
    user: UserDep,
    job_id: str,
) -> HTMLResponse:
    job = _get_job(db, job_id)
    job.status = JobStatus.CANCELLED.value
    db.commit()
    job_engine.stop_job(job_id)
    ctx = _run_context(db, job, "")
    ctx.update({"user": user, "start_errors": []})
    return templates.TemplateResponse(request, "partials/run_panel.html", ctx)


# --- report / cancellazione ---------------------------------------------------


@router.get("/jobs/{job_id}/report.csv", include_in_schema=False)
def report(db: SessionDep, user: UserDep, job_id: str) -> Response:
    job = _get_job(db, job_id)
    content = build_report_csv(db, job_id)
    safe_name = (job.name or job_id).replace('"', "").replace("\n", "")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report_{safe_name}.csv"'},
    )


@router.post("/jobs/{job_id}/delete", include_in_schema=False)
def delete_job(db: SessionDep, user: UserDep, job_id: str) -> RedirectResponse:
    job = _get_job(db, job_id)
    if job.status == JobStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail="Impossibile eliminare un job in esecuzione")
    db.delete(job)
    db.commit()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
