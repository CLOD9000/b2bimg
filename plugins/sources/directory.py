"""SourceDirectory — cerca immagini in una directory locale con pattern wildcard."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from app.config import get_settings
from app.pathutil import PathTraversalError, safe_relative
from plugins.base import (
    ConfigError,
    FetchResult,
    FieldSpec,
    ImageSource,
    NotFoundError,
    format_template,
    require,
)
from plugins.registry import register_source

DEFAULT_PATTERNS = "{codice}.jpg, {codice}.png, {codice}_*.jpg, {codice}_*.png, {codice}.*"


@register_source
class SourceDirectory(ImageSource):
    """Sorgente su filesystem locale (o mount Docker/Samba montato nel container)."""

    type: ClassVar[str] = "directory"
    label: ClassVar[str] = "Directory locale / montata"
    description: ClassVar[str] = (
        "Cerca file in una directory seguendo pattern configurabili "
        "(es. {codice}.jpg, {codice}_*.png, wildcard)."
    )
    fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "base_dir",
            "Directory base",
            required=True,
            default="images",
            placeholder="images",
            help="Percorso assoluto, oppure relativo alla DATA_DIR dell'app.",
        ),
        FieldSpec(
            "patterns",
            "Pattern (separati da virgola)",
            type="textarea",
            default=DEFAULT_PATTERNS,
            help="Ogni pattern supporta i placeholder {codice} e le wildcard di glob.",
        ),
        FieldSpec("recursive", "Ricerca ricorsiva", type="checkbox", default=False),
    )

    @property
    def base_dir(self) -> Path:
        raw = require(self.config, "base_dir", "Directory base")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = get_settings().data_dir / path
        return path

    def _patterns(self) -> list[str]:
        raw = str(self.config.get("patterns") or DEFAULT_PATTERNS)
        patterns = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
        if not patterns:
            raise ConfigError("almeno un pattern è obbligatorio")
        return patterns

    def fetch(self, product_code: str) -> FetchResult:
        code = str(product_code or "").strip()
        if not code:
            raise NotFoundError("codice prodotto vuoto")
        base = self.base_dir
        if not base.is_dir():
            raise ConfigError(f"directory base inesistente: {base}")

        recursive = bool(self.config.get("recursive", False))
        candidates: list[Path] = []
        for raw_pattern in self._patterns():
            pattern = format_template(
                raw_pattern, {"codice": code, "code": code}, context="patterns"
            )
            if ".." in Path(pattern).parts or Path(pattern).is_absolute():
                raise ConfigError(f"pattern non consentito: {pattern!r}")
            try:
                if recursive:
                    found = sorted(p for p in base.rglob(Path(pattern).name) if p.is_file())
                else:
                    found = sorted(p for p in base.glob(pattern) if p.is_file())
            except ValueError as exc:  # glob con pattern invalidi
                raise ConfigError(f"pattern non valido: {pattern!r}") from exc
            for match in found:
                try:
                    safe_relative(match, base)  # garantisce assenza di traversal
                except PathTraversalError:
                    continue
                candidates.append(match)
            if candidates:
                break

        if not candidates:
            raise NotFoundError(
                f"nessun file per il codice {code!r} in {base} "
                f"(pattern: {', '.join(self._patterns())})"
            )
        target = candidates[0]
        size = target.stat().st_size
        if size > self.max_bytes:
            from plugins.base import TooLargeError

            raise TooLargeError(f"file oltre {self.max_bytes} byte: {target.name}")
        return FetchResult(
            location=str(target),
            content_type=target.suffix.lstrip(".").lower() or None,
            meta={"size": size},
        )
