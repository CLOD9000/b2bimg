"""Test web: auth obbligatoria, upload, filtro, config, avvio job e report."""

from __future__ import annotations

import time

import pytest

CSV = (
    b"codice,descrizione,categoria\n"
    b"AB-1001,Tastiera meccanica,periferiche\n"
    b"AB-1002,Mouse wireless,periferiche\n"
    b"AB-2001,Monitor 27,monitor\n"
)


def test_login_required_for_dashboard(client) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_required_for_job_routes(client) -> None:
    for path in ("/jobs/new", "/jobs/qualsiasi", "/jobs/qualsiasi/report.csv"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code in (303, 401), path


def test_healthz_open(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_login_page_renders(client) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "Accedi" in response.text


def test_login_wrong_password(client) -> None:
    response = client.post(
        "/login", data={"username": "admin", "password": "sbagliata"}
    )
    assert response.status_code == 401
    assert "Credenziali non valide" in response.text


def test_login_and_logout(client) -> None:
    response = client.post(
        "/login", data={"username": "admin", "password": "admin-test-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    home = client.get("/")
    assert home.status_code == 200
    assert "Job" in home.text

    client.post("/logout")
    after = client.get("/", follow_redirects=False)
    assert after.status_code == 303


def test_upload_csv_and_preview(auth_client, uploaded_job) -> None:
    page = auth_client.get(f"/jobs/{uploaded_job}")
    assert page.status_code == 200
    assert "AB-1001" in page.text          # anteprima righe
    assert "categoria" in page.text        # header nel dropdown filtro
    assert "codice" in page.text


def test_upload_invalid_csv_rejected(auth_client) -> None:
    response = auth_client.post(
        "/jobs/upload",
        files={"file": ("vuoto.csv", b"", "text/csv")},
        data={"name": "vuoto"},
    )
    assert response.status_code == 400
    assert "vuoto" in response.text.lower() or "file" in response.text.lower()


def test_filter_values_endpoint(auth_client, uploaded_job) -> None:
    response = auth_client.get(
        f"/jobs/{uploaded_job}/filter/values",
        params={"field": "categoria", "search": "per"},
    )
    assert response.status_code == 200
    assert "periferiche" in response.text


def test_filter_apply(auth_client, uploaded_job) -> None:
    response = auth_client.post(
        f"/jobs/{uploaded_job}/filter",
        data={"field": "categoria", "values": ["monitor"]},
    )
    assert response.status_code == 200
    assert "1 righe selezionate" in response.text or "1 righe" in response.text


def test_config_form_partial(auth_client, uploaded_job) -> None:
    response = auth_client.get(
        f"/jobs/{uploaded_job}/config/form", params={"kind": "source", "value": "http"}
    )
    assert response.status_code == 200
    assert "url_template" in response.text
    assert "cookie_env" in response.text


@pytest.mark.parametrize("kind,expected", [("source", "url_template"), ("dest", "bucket")])
def test_config_forms_for_all_kinds(auth_client, uploaded_job, kind, expected) -> None:
    value = "http" if kind == "source" else "s3"
    response = auth_client.get(
        f"/jobs/{uploaded_job}/config/form", params={"kind": kind, "value": value}
    )
    assert response.status_code == 200
    assert expected in response.text


def test_full_job_flow(auth_client, uploaded_job, tmp_path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    (images / "AB-1001.jpg").write_bytes(b"\xff\xd8\xff")
    (images / "AB-1002.jpg").write_bytes(b"\xff\xd8\xff")
    out = tmp_path / "out"

    # 1. filtro sulle periferiche (2 righe)
    filtered = auth_client.post(
        f"/jobs/{uploaded_job}/filter",
        data={"field": "categoria", "values": ["periferiche"]},
    )
    assert filtered.status_code == 200

    # 2. configurazione sorgente/destinazione
    config = auth_client.post(
        f"/jobs/{uploaded_job}/config",
        data={
            "source_type": "directory",
            "dest_type": "local",
            "filename_template": "{codice}.jpg",
            "conflict": "rename",
            "code_field": "codice",
            "src_base_dir": str(images),
            "src_patterns": "{codice}.jpg",
            "src_recursive": "",
            "dst_base_dir": str(out),
        },
    )
    assert config.status_code == 200
    assert "Configurazione salvata" in config.text

    # 3. avvio
    started = auth_client.post(f"/jobs/{uploaded_job}/start")
    assert started.status_code == 200

    # 4. attesa completamento (polling come fa htmx)
    deadline = time.time() + 30
    while time.time() < deadline:
        panel = auth_client.get(f"/jobs/{uploaded_job}/run")
        if "badge-completed" in panel.text:
            break
        time.sleep(0.2)
    else:  # pragma: no cover
        pytest.fail("job non completato entro 30s")

    assert (out / "AB-1001.jpg").exists()
    assert (out / "AB-1002.jpg").exists()

    # 5. righe: 2 scaricate, 1 esclusa dal filtro
    detail = auth_client.get(f"/jobs/{uploaded_job}")
    assert "scaricata" in detail.text
    assert "esclusa" in detail.text

    # 6. report CSV
    report = auth_client.get(f"/jobs/{uploaded_job}/report.csv")
    assert report.status_code == 200
    assert report.headers["content-type"].startswith("text/csv")
    body = report.text
    assert "AB-1001" in body
    assert "scaricata" in body
    assert "esclusa (filtro)" in body


def test_report_unknown_job_404(auth_client) -> None:
    response = auth_client.get("/jobs/inesistente/report.csv")
    assert response.status_code == 404


def test_delete_job(auth_client, uploaded_job) -> None:
    response = auth_client.post(f"/jobs/{uploaded_job}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert auth_client.get(f"/jobs/{uploaded_job}").status_code == 404
