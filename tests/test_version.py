"""Test dell'etichetta di build mostrata nell'interfaccia."""

from __future__ import annotations

from pathlib import Path

from app import version


def test_build_label_uses_env(monkeypatch) -> None:
    version.build_label.cache_clear()
    monkeypatch.setenv("APP_BUILD", "abc123")
    assert version.build_label() == "abc123"
    version.build_label.cache_clear()


def test_build_label_falls_back_to_version(monkeypatch) -> None:
    version.build_label.cache_clear()
    monkeypatch.delenv("APP_BUILD", raising=False)
    monkeypatch.setattr(version, "_BUILD_FILE", Path("/nonexistent/BUILD"))
    assert version.build_label() == f"v{version.__version__}"
    version.build_label.cache_clear()
