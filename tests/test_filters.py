"""Test dei filtri: valori distinti e applicazione della selezione."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Job, JobRow, RowStatus
from app.services import filters as filters_service
from app.services.filters import FilterError


def _job_with_rows(db_session_factory, rows: list[dict[str, str]]) -> str:
    import uuid

    job_id = uuid.uuid4().hex
    headers = list(rows[0].keys())
    with db_session_factory() as db:
        db.add(
            Job(
                id=job_id,
                name="filtro",
                headers=headers,
                code_field=headers[0],
                status="draft",
            )
        )
        db.flush()
        for idx, data in enumerate(rows):
            db.add(
                JobRow(
                    job_id=job_id,
                    idx=idx,
                    data=data,
                    code=data.get(headers[0], ""),
                    status=RowStatus.PENDING.value,
                    selected=True,
                )
            )
        db.commit()
    return job_id


ROWS = [
    {"codice": "A1", "categoria": "monitor"},
    {"codice": "A2", "categoria": "monitor"},
    {"codice": "B1", "categoria": "periferiche"},
]


def test_distinct_values(db_session_factory) -> None:
    job_id = _job_with_rows(db_session_factory, ROWS)
    with db_session_factory() as db:
        job = db.get(Job, job_id)
        values = filters_service.distinct_values(db, job_id, "categoria")
        assert {v["value"] for v in values} == {"monitor", "periferiche"}
        monitor = next(v for v in values if v["value"] == "monitor")
        assert monitor["count"] == 2
        assert job.headers == ["codice", "categoria"]


def test_distinct_values_search(db_session_factory) -> None:
    job_id = _job_with_rows(db_session_factory, ROWS)
    with db_session_factory() as db:
        values = filters_service.distinct_values(db, job_id, "categoria", search="per")
        assert [v["value"] for v in values] == ["periferiche"]


def test_apply_filter_selects_subset(db_session_factory) -> None:
    job_id = _job_with_rows(db_session_factory, ROWS)
    with db_session_factory() as db:
        job = db.get(Job, job_id)
        selected = filters_service.apply_filter(db, job, "categoria", ["monitor"])
        assert selected == 2
        rows = db.scalars(select(JobRow).where(JobRow.job_id == job_id)).all()
        by_code = {r.code: r for r in rows}
        assert by_code["A1"].selected is True
        assert by_code["B1"].selected is False
        assert by_code["B1"].status == RowStatus.SKIPPED.value
        assert job.filter_field == "categoria"


def test_apply_filter_empty_means_all(db_session_factory) -> None:
    job_id = _job_with_rows(db_session_factory, ROWS)
    with db_session_factory() as db:
        job = db.get(Job, job_id)
        filters_service.apply_filter(db, job, "categoria", ["monitor"])
        selected = filters_service.apply_filter(db, job, "categoria", [])
        assert selected == 3
        rows = db.scalars(select(JobRow).where(JobRow.job_id == job_id)).all()
        assert all(r.selected for r in rows)
        assert all(r.status == RowStatus.PENDING.value for r in rows)


def test_apply_filter_unknown_field_raises(db_session_factory) -> None:
    job_id = _job_with_rows(db_session_factory, ROWS)
    with db_session_factory() as db:
        job = db.get(Job, job_id)
        with pytest.raises(FilterError):
            filters_service.apply_filter(db, job, "inesistente", ["x"])


def test_reset_rows_for_rerun(db_session_factory) -> None:
    job_id = _job_with_rows(db_session_factory, ROWS)
    with db_session_factory() as db:
        rows = db.scalars(select(JobRow).where(JobRow.job_id == job_id)).all()
        rows[0].status = RowStatus.ERROR.value
        rows[1].status = RowStatus.DOWNLOADED.value
        db.commit()
        changed = filters_service.reset_rows_for_rerun(db, job_id, only_failed=True)
        assert changed == 1
        rows = db.scalars(select(JobRow).where(JobRow.job_id == job_id)).all()
        assert rows[0].status == RowStatus.PENDING.value
        assert rows[1].status == RowStatus.DOWNLOADED.value
