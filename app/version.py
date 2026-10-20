"""Versione e identificativo di build dell'applicazione."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

__version__ = "1.0.0"

_BUILD_FILE = Path(__file__).resolve().parents[1] / "BUILD"


@lru_cache(maxsize=1)
def build_label() -> str:
    """Etichetta di build mostrata nell'interfaccia.

    Ordine di priorità: variabile d'ambiente ``APP_BUILD`` (aggiornabile in
    ``stack.env`` senza rebuild), file ``BUILD`` generato dall'immagine Docker
    (data/ora di build), infine la versione del pacchetto.
    """
    explicit = os.environ.get("APP_BUILD", "").strip()
    if explicit:
        return explicit
    try:
        baked = _BUILD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        baked = ""
    if baked:
        return f"v{__version__} ({baked})"
    return f"v{__version__}"
