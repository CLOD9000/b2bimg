"""Registrazione di tutte le sorgenti predefinite."""

from __future__ import annotations

from plugins.sources import directory, http, sftp  # noqa: F401  (autoregistrazione)

__all__ = ["directory", "http", "sftp"]
