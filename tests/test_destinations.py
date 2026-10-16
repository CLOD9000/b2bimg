"""Test delle destinazioni: locale (conflitti + traversal), S3 e SFTP mockati."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.pathutil import PathTraversalError, safe_join, sanitize_filename
from plugins.destinations.local import DestinationLocal

# --- pathutil ---------------------------------------------------------------


def test_safe_join_ok(tmp_path) -> None:
    assert safe_join(tmp_path, "a", "b.jpg") == (tmp_path / "a" / "b.jpg").resolve()


def test_safe_join_blocks_parent(tmp_path) -> None:
    with pytest.raises(PathTraversalError):
        safe_join(tmp_path, "../escape.jpg")


def test_safe_join_blocks_absolute(tmp_path) -> None:
    with pytest.raises(PathTraversalError):
        safe_join(tmp_path, "/etc/passwd")


def test_sanitize_filename_removes_separators() -> None:
    assert "/" not in sanitize_filename("../../etc/passwd")
    assert "\\" not in sanitize_filename("..\\..\\win.ini")
    assert sanitize_filename("") == "file"
    assert sanitize_filename("...") == "file"


# --- DestinationLocal --------------------------------------------------------


def test_local_put_created(tmp_path) -> None:
    dest = DestinationLocal({"base_dir": str(tmp_path)})
    result = dest.put("AB-1.jpg", b"data", conflict="rename")
    assert result.action == "created"
    assert (tmp_path / "AB-1.jpg").read_bytes() == b"data"


def test_local_put_rename_on_conflict(tmp_path) -> None:
    dest = DestinationLocal({"base_dir": str(tmp_path)})
    dest.put("AB-1.jpg", b"uno", conflict="rename")
    result = dest.put("AB-1.jpg", b"due", conflict="rename")
    assert result.action == "created"
    assert (tmp_path / "AB-1.jpg").read_bytes() == b"uno"
    assert (tmp_path / "AB-1 (1).jpg").read_bytes() == b"due"


def test_local_put_overwrite(tmp_path) -> None:
    dest = DestinationLocal({"base_dir": str(tmp_path)})
    dest.put("AB-1.jpg", b"uno", conflict="overwrite")
    result = dest.put("AB-1.jpg", b"due", conflict="overwrite")
    assert result.action == "overwritten"
    assert (tmp_path / "AB-1.jpg").read_bytes() == b"due"


def test_local_put_skip(tmp_path) -> None:
    dest = DestinationLocal({"base_dir": str(tmp_path)})
    dest.put("AB-1.jpg", b"uno", conflict="skip")
    result = dest.put("AB-1.jpg", b"due", conflict="skip")
    assert result.action == "skipped"
    assert (tmp_path / "AB-1.jpg").read_bytes() == b"uno"


def test_local_put_blocks_traversal(tmp_path) -> None:
    dest = DestinationLocal({"base_dir": str(tmp_path)})
    result = dest.put("../evil.jpg", b"x", conflict="rename")
    assert result.action == "created"
    assert not (tmp_path.parent / "evil.jpg").exists()
    assert (tmp_path / "_evil.jpg").exists() or (tmp_path / "evil.jpg").exists()


# --- DestinationS3 -----------------------------------------------------------


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "HeadObject")


def _s3_dest(**config):
    from plugins.destinations.s3 import DestinationS3

    payload = {
        "bucket": "prodotti",
        "endpoint_url": "http://minio.test:9000",
        "access_key": "chiave",
        "secret_key": "segreto",
    }
    payload.update(config)
    return DestinationS3(payload)


def test_s3_put_new_object() -> None:
    dest = _s3_dest()
    client = MagicMock()
    client.head_object.side_effect = _client_error("404")
    session = MagicMock()
    session.client.return_value = client

    with patch("plugins.destinations.s3.boto3.session.Session", return_value=session):
        result = dest.put("AB-1.jpg", b"data", conflict="rename")

    assert result.action == "created"
    assert result.path == "s3://prodotti/AB-1.jpg"
    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "prodotti"
    assert kwargs["Key"] == "AB-1.jpg"
    assert kwargs["Body"] == b"data"


def test_s3_put_rename_on_conflict() -> None:
    dest = _s3_dest()
    client = MagicMock()

    def head(Bucket: str, Key: str):  # noqa: N803 - firma boto3
        if Key != "AB-1.jpg":
            raise _client_error("404")
        return {}

    client.head_object.side_effect = head
    session = MagicMock()
    session.client.return_value = client

    with patch("plugins.destinations.s3.boto3.session.Session", return_value=session):
        result = dest.put("AB-1.jpg", b"data", conflict="rename")

    assert result.path == "s3://prodotti/AB-1 (1).jpg"


def test_s3_put_skip() -> None:
    dest = _s3_dest()
    client = MagicMock()  # head_object ok => oggetto esistente
    session = MagicMock()
    session.client.return_value = client

    with patch("plugins.destinations.s3.boto3.session.Session", return_value=session):
        result = dest.put("AB-1.jpg", b"data", conflict="skip")

    assert result.action == "skipped"
    client.put_object.assert_not_called()


def test_s3_prefix_applied() -> None:
    dest = _s3_dest(prefix="listino/")
    client = MagicMock()
    client.head_object.side_effect = _client_error("404")
    session = MagicMock()
    session.client.return_value = client

    with patch("plugins.destinations.s3.boto3.session.Session", return_value=session):
        dest.put("AB-1.jpg", b"data")

    assert client.put_object.call_args.kwargs["Key"] == "listino/AB-1.jpg"


# --- DestinationSFTP ---------------------------------------------------------


def test_sftp_put_uploads_file() -> None:
    from plugins.destinations.sftp import DestinationSFTP

    dest = DestinationSFTP(
        {"host": "nas.test", "username": "u", "password": "p", "remote_dir": "/out"}
    )
    with patch("plugins.destinations.sftp.paramiko.SSHClient") as ssh_cls:
        client = MagicMock()
        sftp = MagicMock()
        ssh_cls.return_value = client
        client.open_sftp.return_value = sftp
        sftp.stat.side_effect = OSError("assente")
        handle = MagicMock()
        sftp.open.return_value = handle

        result = dest.put("AB-1.jpg", b"data", conflict="rename")

    assert result.action == "created"
    assert result.path == "/out/AB-1.jpg"
    handle.__enter__.return_value.write.assert_called_once_with(b"data")


def test_sftp_put_skip_existing() -> None:
    from plugins.destinations.sftp import DestinationSFTP

    dest = DestinationSFTP(
        {"host": "nas.test", "username": "u", "password": "p", "remote_dir": "/out"}
    )
    with patch("plugins.destinations.sftp.paramiko.SSHClient") as ssh_cls:
        client = MagicMock()
        sftp = MagicMock()
        ssh_cls.return_value = client
        client.open_sftp.return_value = sftp
        sftp.stat.return_value = MagicMock()  # esiste

        result = dest.put("AB-1.jpg", b"data", conflict="skip")

    assert result.action == "skipped"
    sftp.open.assert_not_called()
