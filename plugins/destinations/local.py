"""DestinationLocal — salva le immagini in una directory locale (o volume Docker)."""

from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.pathutil import safe_join, sanitize_filename
from plugins.base import ConfigError, Destination, FieldSpec, PutResult, require
from plugins.registry import register_destination


@register_destination
class DestinationLocal(Destination):
    """Scrittura su filesystem locale con gestione dei conflitti."""

    type: str = "local"
    label = "Directory locale"
    description = "Scrive i file in una directory del container/host (volume consigliato)."
    fields = (
        FieldSpec(
            "base_dir",
            "Directory di destinazione",
            required=True,
            default="output",
            help="Percorso assoluto, oppure relativo alla DATA_DIR dell'app.",
        ),
    )

    @property
    def base_dir(self) -> Path:
        raw = require(self.config, "base_dir", "Directory di destinazione")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = get_settings().data_dir / path
        return path

    def put(self, filename: str, data: bytes, conflict: str = "rename") -> PutResult:
        name = sanitize_filename(filename, default="immagine.jpg")
        base = self.base_dir
        try:
            base.mkdir(parents=True, exist_ok=True)
            target = safe_join(base, name)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"directory di destinazione non valida: {exc}") from exc

        if target.exists():
            if conflict == "skip":
                return PutResult(path=str(target), action="skipped", size=target.stat().st_size)
            if conflict == "rename":
                target = self._next_free(target)
            # overwrite: si prosegue scrivendo sopra

        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        action = "overwritten" if conflict == "overwrite" else "created"
        return PutResult(path=str(target), action=action, size=len(data))

    @staticmethod
    def _next_free(target: Path) -> Path:
        stem, suffix = target.stem, target.suffix
        counter = 1
        while True:
            candidate = target.with_name(f"{stem} ({counter}){suffix}")
            if not candidate.exists():
                return candidate
            counter += 1
