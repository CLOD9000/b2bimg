"""Test del job engine: esecuzione end-to-end, retry, conteggi e validazione."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import Job, JobRow, JobStatus, RowStatus
from app.services import job_engine
from plugins import FetchResult, ImageSource, NetworkError, register_source

JPEG = b"\xff\xd8\xff\xe0fakejpeg"


def _make_job(db_session_factory, rows, **kwargs) -> str:
    job_id = uuid.uuid4().hex
    headers = ["codice", "categoria"]
    payload = {
        "id": job_id,
        "name": "engine test",
        "headers": headers,
        "code_field": "codice",
        "status": JobStatus.READY.value,
        "source_type": "",
        "source_config": {},
        "dest_type": "",
        "dest_config": {},
        "filename_template": "{codice}.jpg",
        "conflict_strategy": "rename",
    }
    payload.update(kwargs)
    with db_session_factory() as db:
        db.add(Job(**payload))
        db.flush()
        for idx, (code, category) in enumerate(rows):
            db.add(
                JobRow(
                    job_id=job_id,
                    idx=idx,
                    data={"codice": code, "categoria": category},
                    code=code,
                    status=RowStatus.PENDING.value,
                    selected=True,
                )
            )
        db.commit()
    return job_id


def _rows(db_session_factory, job_id: str) -> dict[str, JobRow]:
    with db_session_factory() as db:
        rows = db.scalars(select(JobRow).where(JobRow.job_id == job_id)).all()
    return {r.code: r for r in rows}


def test_engine_end_to_end(db_session_factory, tmp_path) -> None:
    (tmp_path / "AB-1001.jpg").write_bytes(JPEG)
    job_id = _make_job(
        db_session_factory,
        [("AB-1001", "monitor"), ("ASSENTE", "monitor")],
        source_type="directory",
        source_config={"base_dir": str(tmp_path), "patterns": "{codice}.jpg"},
        dest_type="local",
        dest_config={"base_dir": str(tmp_path / "out")},
    )

    job_engine.run_job(job_id)

    rows = _rows(db_session_factory, job_id)
    assert rows["AB-1001"].status == RowStatus.DOWNLOADED.value
    assert rows["AB-1001"].file_size == len(JPEG)
    assert rows["AB-1001"].source_used == "directory"
    assert rows["ASSENTE"].status == RowStatus.NOT_FOUND.value
    assert (tmp_path / "out" / "AB-1001.jpg").read_bytes() == JPEG

    with db_session_factory() as db:
        job = db.get(Job, job_id)
        assert job.status == JobStatus.COMPLETED.value
        assert job.finished_at is not None
        payload = job_engine.progress_payload(db, job_id)
    assert payload["counts"]["downloaded"] == 1
    assert payload["counts"]["not_found"] == 1
    assert payload["percent"] == 100


def test_engine_marks_config_error(db_session_factory, tmp_path) -> None:
    job_id = _make_job(
        db_session_factory,
        [("AB-1001", "monitor")],
        source_type="directory",
        source_config={"base_dir": str(tmp_path / "assente"), "patterns": "{codice}.jpg"},
        dest_type="local",
        dest_config={"base_dir": str(tmp_path / "out")},
    )
    job_engine.run_job(job_id)
    rows = _rows(db_session_factory, job_id)
    assert rows["AB-1001"].status == RowStatus.ERROR.value
    assert "directory base inesistente" in rows["AB-1001"].message


def test_engine_fails_when_not_configured(db_session_factory) -> None:
    job_id = _make_job(db_session_factory, [("A1", "x")])
    job_engine.run_job(job_id)
    with db_session_factory() as db:
        job = db.get(Job, job_id)
    assert job.status == JobStatus.FAILED.value
    assert "sorgente" in job.error.lower() or "destinazione" in job.error.lower()


def test_engine_skips_unselected_rows(db_session_factory, tmp_path) -> None:
    (tmp_path / "A1.jpg").write_bytes(JPEG)
    (tmp_path / "B1.jpg").write_bytes(JPEG)
    job_id = _make_job(
        db_session_factory,
        [("A1", "x"), ("B1", "y")],
        source_type="directory",
        source_config={"base_dir": str(tmp_path), "patterns": "{codice}.jpg"},
        dest_type="local",
        dest_config={"base_dir": str(tmp_path / "out")},
    )
    from app.services import filters as filters_service

    with db_session_factory() as db:
        job = db.get(Job, job_id)
        filters_service.apply_filter(db, job, "categoria", ["x"])

    job_engine.run_job(job_id)
    rows = _rows(db_session_factory, job_id)
    assert rows["A1"].status == RowStatus.DOWNLOADED.value
    assert rows["B1"].status == RowStatus.SKIPPED.value


def test_engine_filename_template_uses_extra_placeholders(db_session_factory, tmp_path) -> None:
    (tmp_path / "A1.jpg").write_bytes(JPEG)
    job_id = _make_job(
        db_session_factory,
        [("A1", "monitor")],
        source_type="directory",
        source_config={"base_dir": str(tmp_path), "patterns": "{codice}.jpg"},
        dest_type="local",
        dest_config={"base_dir": str(tmp_path / "out")},
        filename_template="monitor/{codice}_{categoria}.jpg",
    )
    job_engine.run_job(job_id)
    rows = _rows(db_session_factory, job_id)
    assert rows["A1"].status == RowStatus.DOWNLOADED.value
    # i separatori di path vengono neutralizzati dalla sanitizzazione
    assert rows["A1"].output_path.endswith("monitor_A1_monitor.jpg")
    assert (tmp_path / "out" / "monitor_A1_monitor.jpg").exists()


def test_engine_filename_template_unknown_placeholder(db_session_factory, tmp_path) -> None:
    (tmp_path / "A1.jpg").write_bytes(JPEG)
    job_id = _make_job(
        db_session_factory,
        [("A1", "monitor")],
        source_type="directory",
        source_config={"base_dir": str(tmp_path), "patterns": "{codice}.jpg"},
        dest_type="local",
        dest_config={"base_dir": str(tmp_path / "out")},
        filename_template="{inesistente}.jpg",
    )
    job_engine.run_job(job_id)
    rows = _rows(db_session_factory, job_id)
    assert rows["A1"].status == RowStatus.ERROR.value
    assert "inesistente" in rows["A1"].message


# --- plugin dimostrativo registrato in test ----------------------------------


@register_source
class FlakySource(ImageSource):
    """Sorgente di test: fallisce con errore di rete fino a `fail_times` chiamate."""

    type = "flaky_test"
    label = "Flaky (test)"
    fields = ()
    calls: list[str] = []
    fail_times = 1

    def fetch(self, product_code: str) -> FetchResult:
        type(self).calls.append(product_code)
        if len(type(self).calls) <= type(self).fail_times:
            raise NetworkError("errore transitorio simulato")
        return FetchResult(location=f"https://fake.test/{product_code}.jpg", content=JPEG)


def test_engine_retries_transient_errors(db_session_factory, tmp_path) -> None:
    FlakySource.calls = []
    FlakySource.fail_times = 1
    job_id = _make_job(
        db_session_factory,
        [("A1", "x")],
        source_type="flaky_test",
        source_config={},
        dest_type="local",
        dest_config={"base_dir": str(tmp_path / "out")},
    )
    job_engine.run_job(job_id)
    rows = _rows(db_session_factory, job_id)
    assert rows["A1"].status == RowStatus.DOWNLOADED.value
    assert rows["A1"].attempts == 2
    assert len(FlakySource.calls) == 2


def test_engine_gives_up_after_max_retries(db_session_factory, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_MAX_RETRIES", "3")
    from app.config import get_settings

    get_settings.cache_clear()
    FlakySource.calls = []
    FlakySource.fail_times = 99
    job_id = _make_job(
        db_session_factory,
        [("A1", "x")],
        source_type="flaky_test",
        source_config={},
        dest_type="local",
        dest_config={"base_dir": str(tmp_path / "out")},
    )
    job_engine.run_job(job_id)
    rows = _rows(db_session_factory, job_id)
    assert rows["A1"].status == RowStatus.ERROR.value
    assert rows["A1"].attempts == 3
    assert len(FlakySource.calls) == 3


def test_retry_failed_resets_rows(db_session_factory, tmp_path) -> None:
    (tmp_path / "A1.jpg").write_bytes(JPEG)
    job_id = _make_job(
        db_session_factory,
        [("A1", "x"), ("A2", "x")],
        source_type="directory",
        source_config={"base_dir": str(tmp_path), "patterns": "{codice}.jpg"},
        dest_type="local",
        dest_config={"base_dir": str(tmp_path / "out")},
    )
    job_engine.run_job(job_id)
    assert _rows(db_session_factory, job_id)["A2"].status == RowStatus.NOT_FOUND.value

    with db_session_factory() as db:
        changed = job_engine.job_counts(db, job_id)
        assert changed["not_found"] == 1

    # crea il file mancante e rilancia solo i falliti
    (tmp_path / "A2.jpg").write_bytes(JPEG)
    from app.services import filters as filters_service

    with db_session_factory() as db:
        reset = filters_service.reset_rows_for_rerun(db, job_id, only_failed=True)
        assert reset == 1
    job_engine.run_job(job_id)
    rows = _rows(db_session_factory, job_id)
    assert rows["A2"].status == RowStatus.DOWNLOADED.value
    assert rows["A1"].status == RowStatus.DOWNLOADED.value
