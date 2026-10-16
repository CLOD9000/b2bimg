"""Protezione path traversal e sanitizzazione dei nomi file."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class PathTraversalError(ValueError):
    """Sollevato quando un path esce dalla directory consentita."""


def sanitize_filename(name: str, default: str = "file") -> str:
    """Rimuove separatori, componenti di path e caratteri pericolosi da un nome file."""
    name = (name or "").strip().replace("\x00", "")
    name = name.replace("/", "_").replace("\\", "_")
    name = _UNSAFE.sub("_", name)
    name = name.strip(" .")
    stem = name.split(".")[0].lower()
    if stem in _RESERVED:
        name = f"_{name}"
    if not name or name in {".", ".."}:
        return default
    return name[:200]


def safe_join(base: Path | str, *parts: str) -> Path:
    """Unisce `parts` a `base` verificando che il risultato resti dentro `base`.

    Solleva :class:`PathTraversalError` in caso di path traversal (assoluto,
    `..`, symlink fuori base, ecc.).
    """
    base_path = Path(base).expanduser().resolve()
    candidate = base_path
    for part in parts:
        if not part:
            continue
        pure = PurePosixPath(str(part).replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts:
            raise PathTraversalError(f"path non consentito: {part!r}")
        candidate = candidate.joinpath(*pure.parts)
    resolved = candidate.resolve()
    if resolved != base_path and base_path not in resolved.parents:
        raise PathTraversalError(f"path fuori dalla base consentita: {candidate}")
    return resolved


def safe_relative(path: Path | str, base: Path | str) -> str:
    """Restituisce il percorso relativo a `base`, altrimenti solleva errore."""
    resolved = Path(path).resolve()
    base_path = Path(base).expanduser().resolve()
    try:
        return str(resolved.relative_to(base_path))
    except ValueError as exc:
        raise PathTraversalError(f"{path} non è sotto {base}") from exc


def expand_placeholders(template: str, values: dict[str, str]) -> str:
    """Espande `{chiave}` in `template` con `values`, senza introdurre path traversal.

    Le chiavi note sono l'unico posto in cui è possibile inserire testo dinamico:
    il risultato finale passa comunque da :func:`sanitize_filename`.
    """
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out
