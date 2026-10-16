"""Test delle sorgenti immagini (HTTP mockato, directory locale, SFTP mockato)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.ratelimit import RateLimiter
from plugins.base import (
    AuthError,
    ConfigError,
    NetworkError,
    NotFoundError,
    TooLargeError,
)
from plugins.sources.directory import SourceDirectory
from plugins.sources.http import SourceHTTP

JPEG = b"\xff\xd8\xff\xe0fakejpeg"


def _http(handler, **config) -> SourceHTTP:
    payload = {
        "url_template": "https://esempio.test/img/{codice}.jpg",
        "require_image": True,
    }
    payload.update(config)
    return SourceHTTP(payload, transport=httpx.MockTransport(handler))


# --- SourceHTTP --------------------------------------------------------------


def test_http_ok_returns_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=JPEG, headers={"content-type": "image/jpeg"}
        )

    source = _http(handler)
    result = source.fetch("AB-1001")
    assert result.location == "https://esempio.test/img/AB-1001.jpg"
    assert result.content == JPEG
    assert result.content_type == "image/jpeg"


def test_http_404_not_found() -> None:
    source = _http(lambda request: httpx.Response(404))
    with pytest.raises(NotFoundError):
        source.fetch("ASSENTE")


def test_http_wrong_content_type_not_found() -> None:
    source = _http(
        lambda request: httpx.Response(
            200, content=b"<html>login</html>", headers={"content-type": "text/html"}
        )
    )
    with pytest.raises(NotFoundError):
        source.fetch("AB-1001")


def test_http_403_auth_error() -> None:
    source = _http(lambda request: httpx.Response(403))
    with pytest.raises(AuthError):
        source.fetch("AB-1001")


def test_http_500_network_error_retryable() -> None:
    source = _http(lambda request: httpx.Response(500))
    with pytest.raises(NetworkError) as exc:
        source.fetch("AB-1001")
    assert exc.value.retryable is True


def test_http_timeout_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    source = _http(handler)
    with pytest.raises(NetworkError):
        source.fetch("AB-1001")


def test_http_cookie_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SESSION_COOKIE", "sessionid=abc123; token=xyz")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie", "")
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    source = _http(handler, cookie_env="TEST_SESSION_COOKIE")
    source.fetch("AB-1")
    assert seen["cookie"] == "sessionid=abc123; token=xyz"


def test_http_cookie_from_file(tmp_path) -> None:
    cookie_file = tmp_path / "cookie.txt"
    cookie_file.write_text("sessionid=file-cookie\n", encoding="utf-8")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie", "")
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    source = _http(handler, cookie_file=str(cookie_file))
    source.fetch("AB-1")
    assert seen["cookie"] == "sessionid=file-cookie"


def test_http_missing_cookie_env_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUALSIASI_COOKIE", raising=False)
    source = _http(lambda request: httpx.Response(200), cookie_env="QUALSIASI_COOKIE")
    with pytest.raises(ConfigError):
        source.fetch("AB-1")


def test_http_template_requires_placeholder() -> None:
    source = _http(lambda request: httpx.Response(200), url_template="https://x.test/a.jpg")
    with pytest.raises(ConfigError):
        source.fetch("AB-1")


def test_http_rejects_non_http_scheme() -> None:
    source = _http(
        lambda request: httpx.Response(200), url_template="file:///etc/{codice}"
    )
    with pytest.raises(ConfigError):
        source.fetch("AB-1")


def test_http_max_bytes(tmp_path) -> None:
    source = _http(
        lambda request: httpx.Response(200, content=b"x" * 100),
        url_template="https://esempio.test/{codice}",
        require_image=False,
    )
    source.max_bytes = 10
    with pytest.raises(TooLargeError):
        source.fetch("AB-1")


def test_http_header_lines_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-tenant"] == "acme"
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    source = _http(handler, headers="X-Tenant: acme\n# commento\nAltro: valore")
    source.fetch("AB-1")


# --- SourceDirectory ---------------------------------------------------------


@pytest.fixture()
def images_dir(tmp_path):
    (tmp_path / "AB-1001.jpg").write_bytes(JPEG)
    (tmp_path / "AB-1002_fronte.png").write_bytes(b"\x89PNG")
    (tmp_path / "readme.txt").write_text("no", encoding="utf-8")
    return tmp_path


def test_directory_finds_exact(images_dir) -> None:
    source = SourceDirectory(
        {"base_dir": str(images_dir), "patterns": "{codice}.jpg, {codice}.png"}
    )
    result = source.fetch("AB-1001")
    assert result.location.endswith("AB-1001.jpg")


def test_directory_finds_wildcard(images_dir) -> None:
    source = SourceDirectory(
        {"base_dir": str(images_dir), "patterns": "{codice}.jpg, {codice}_*.png"}
    )
    result = source.fetch("AB-1002")
    assert result.location.endswith("AB-1002_fronte.png")


def test_directory_not_found(images_dir) -> None:
    source = SourceDirectory({"base_dir": str(images_dir), "patterns": "{codice}.jpg"})
    with pytest.raises(NotFoundError):
        source.fetch("MAI-QUI")


def test_directory_blocks_traversal_pattern(images_dir) -> None:
    source = SourceDirectory({"base_dir": str(images_dir), "patterns": "../{codice}.jpg"})
    with pytest.raises(ConfigError):
        source.fetch("qualcosa")


def test_directory_code_with_traversal(images_dir) -> None:
    source = SourceDirectory({"base_dir": str(images_dir), "patterns": "{codice}.jpg"})
    with pytest.raises(ConfigError):
        source.fetch("../../etc/passwd")


def test_directory_missing_base_dir(tmp_path) -> None:
    source = SourceDirectory({"base_dir": str(tmp_path / "assente")})
    with pytest.raises(ConfigError):
        source.fetch("AB-1")


def test_directory_relative_to_data_dir() -> None:
    from app.config import get_settings

    source = SourceDirectory({"base_dir": "images", "patterns": "{codice}.jpg"})
    assert str(get_settings().data_dir) in str(source.base_dir)


# --- SourceSFTP --------------------------------------------------------------


def _sftp_source(**config):
    from plugins.sources.sftp import SourceSFTP

    payload = {
        "host": "nas.test",
        "username": "utente",
        "password": "segreto",
        "remote_dir": "/images",
        "patterns": "{codice}.jpg",
    }
    payload.update(config)
    return SourceSFTP(payload)


def test_sftp_fetch_reads_file() -> None:
    source = _sftp_source()
    with patch("plugins.sources.sftp.paramiko.SSHClient") as ssh_cls:
        client = MagicMock()
        sftp = MagicMock()
        ssh_cls.return_value = client
        client.open_sftp.return_value = sftp
        sftp.listdir.return_value = ["AB-1.jpg", "altro.png"]
        sftp.stat.return_value = MagicMock(st_size=len(JPEG))
        handle = MagicMock()
        handle.__enter__.return_value.read.return_value = JPEG
        sftp.open.return_value = handle

        result = source.fetch("AB-1")
        assert result.content == JPEG
        assert result.location == "/images/AB-1.jpg"
        sftp.open.assert_called_with("/images/AB-1.jpg", "rb")


def test_sftp_not_found() -> None:
    source = _sftp_source()
    with patch("plugins.sources.sftp.paramiko.SSHClient") as ssh_cls:
        client = MagicMock()
        sftp = MagicMock()
        ssh_cls.return_value = client
        client.open_sftp.return_value = sftp
        sftp.listdir.return_value = ["altro.png"]
        with pytest.raises(NotFoundError):
            source.fetch("AB-1")


def test_sftp_requires_credentials() -> None:
    from plugins.base import ConfigError as _ConfigError

    source = _sftp_source(password="", key_file="")
    with patch("plugins.sources.sftp.paramiko.SSHClient"), pytest.raises(_ConfigError):
        source.fetch("AB-1")


# --- Rate limiter -------------------------------------------------------------


def test_rate_limiter_limits_calls() -> None:
    limiter = RateLimiter(max_calls=2, period=60)
    assert limiter.acquire(timeout=0.1) is True
    assert limiter.acquire(timeout=0.1) is True
    assert limiter.acquire(timeout=0.05) is False


def test_rate_limiter_window_expires() -> None:
    limiter = RateLimiter(max_calls=1, period=0.05)
    assert limiter.acquire(timeout=0.1) is True
    assert limiter.acquire(timeout=1.0) is True  # attende la finestra
