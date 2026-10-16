"""Registrazione di tutte le destinazioni predefinite."""

from __future__ import annotations

from plugins.destinations import local, s3, sftp  # noqa: F401  (autoregistrazione)

__all__ = ["local", "s3", "sftp"]
