"""Rendering dei template Jinja2."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.version import build_label

templates = Jinja2Templates(directory="templates")


def _status_icon(status: str) -> str:
    return {
        "pending": "…",
        "selected": "…",
        "skipped": "–",
        "downloaded": "✓",
        "not_found": "⚠",
        "error": "✗",
    }.get(status, "?")


def _status_label(status: str) -> str:
    return {
        "pending": "in attesa",
        "selected": "selezionata",
        "skipped": "esclusa",
        "downloaded": "scaricata",
        "not_found": "non trovata",
        "error": "errore",
    }.get(status, status)


templates.env.globals["status_icon"] = _status_icon
templates.env.globals["status_label"] = _status_label
templates.env.globals["build"] = build_label()
