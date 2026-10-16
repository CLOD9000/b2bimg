"""Interfacce comuni dei plugin: sorgenti immagini e destinazioni."""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

_ALLOWED_VAR = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def format_template(
    template: str, values: Mapping[str, Any], *, context: str = "template"
) -> str:
    """Sostituisce i placeholder ``{nome}`` in modo sicuro (solo chiavi note).

    A differenza di ``str.format`` non accede ad attributi/metodi degli oggetti,
    quindi un valore utente (codice prodotto) non può eseguire codice.
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ConfigError(f"placeholder {{{key}}} sconosciuto in {context}")
        return str(values[key])

    return _PLACEHOLDER.sub(_replace, template)


class PluginError(Exception):
    """Errore di plugin con codice stabile usato dal job engine."""

    code: str = "error"
    retryable: bool = True

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code

    @property
    def message(self) -> str:
        return str(self)


class ConfigError(PluginError):
    """Configurazione non valida: non ha senso riprovare."""

    code = "config"
    retryable = False


class NotFoundError(PluginError):
    """Risorsa assente per il codice prodotto richiesto."""

    code = "not_found"
    retryable = False


class NetworkError(PluginError):
    """Timeout, refused, 5xx: ha senso riprovare."""

    code = "network"
    retryable = True


class TooLargeError(PluginError):
    """La risorsa supera la dimensione massima consentita."""

    code = "too_large"
    retryable = False


class AuthError(PluginError):
    """Autenticazione verso la sorgente/destinazione fallita."""

    code = "auth"
    retryable = False


@dataclass(frozen=True)
class FetchResult:
    """Esito di ``ImageSource.fetch``.

    ``location`` è l'URL o il path trovato (sempre valorizzato, serve per il log);
    ``content`` è opzionale: quando la sorgente ha già scaricato il body
    (es. SourceHTTP) il job engine evita una seconda richiesta.
    """

    location: str
    content: bytes | None = None
    content_type: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PutResult:
    """Esito di ``Destination.put``."""

    path: str
    action: str  # created | overwritten | renamed | skipped
    size: int = 0


@dataclass(frozen=True)
class FieldSpec:
    """Descrive un campo di configurazione: il form viene generato dinamicamente."""

    name: str
    label: str
    type: str = "text"  # text | password | number | checkbox | textarea | select
    required: bool = False
    default: Any = ""
    placeholder: str = ""
    help: str = ""
    options: tuple[tuple[str, str], ...] = ()
    env_hint: str = ""  # es. "${SFTP_PASSWORD}" documentato nel form

    @property
    def is_secret(self) -> bool:
        return self.type == "password"


def resolve_env_value(value: Any) -> Any:
    """Risolve riferimenti a variabili d'ambiente in un valore di configurazione.

    Solo il pattern completo ``${VAR}`` viene sostituito: in questo modo le
    credenziali restano esclusivamente nel `.env` e mai nel codice né nel DB.
    """
    if isinstance(value, str):
        match = _ALLOWED_VAR.match(value.strip())
        if match:
            return os.environ.get(match.group(1), "")
        return value
    if isinstance(value, Mapping):
        return {k: resolve_env_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env_value(v) for v in value]
    return value


def resolve_config(config: Mapping[str, Any], spec: Iterable[FieldSpec]) -> dict[str, Any]:
    """Applica default + risoluzione ``${ENV}`` a una mappa di configurazione."""
    out: dict[str, Any] = {f.name: f.default for f in spec}
    out.update({k: v for k, v in (config or {}).items() if v is not None})
    out = resolve_env_value(out)
    return out


def require(config: Mapping[str, Any], name: str, label: str | None = None) -> str:
    """Legge un campo obbligatorio restituendo una stringa non vuota."""
    value = str(config.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{label or name} è obbligatorio")
    return value


class ImageSource(ABC):
    """Interfaccia comune di tutte le sorgenti immagini."""

    type: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    fields: ClassVar[tuple[FieldSpec, ...]] = ()
    rate_limited: ClassVar[bool] = True

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        rate_limiter: Any | None = None,
        default_timeout: float = 15.0,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self.config = resolve_config(config, self.fields)
        self.rate_limiter = rate_limiter
        self.default_timeout = default_timeout
        self.max_bytes = int(max_bytes)

    @abstractmethod
    def fetch(self, product_code: str) -> FetchResult:
        """Restituisce l'immagine per ``product_code``.

        Deve sollevare :class:`NotFoundError` se assente e
        :class:`NetworkError` per errori transitivi.
        """

    @property
    def timeout(self) -> float:
        raw = self.config.get("timeout")
        try:
            return float(raw) if raw not in (None, "") else self.default_timeout
        except (TypeError, ValueError):
            return self.default_timeout

    def acquire_slot(self, timeout: float = 120.0) -> None:
        """Applica il rate limiting condiviso prima di ogni richiesta esterna."""
        if self.rate_limiter is not None and not self.rate_limiter.acquire(timeout):
            raise NetworkError("rate limit sorgente raggiunto, riprovare più tardi")


class Destination(ABC):
    """Interfaccia comune di tutte le destinazioni."""

    type: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    fields: ClassVar[tuple[FieldSpec, ...]] = ()

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        default_timeout: float = 30.0,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self.config = resolve_config(config, self.fields)
        self.default_timeout = default_timeout
        self.max_bytes = int(max_bytes)

    @abstractmethod
    def put(self, filename: str, data: bytes, conflict: str = "rename") -> PutResult:
        """Salva ``data`` come ``filename``.

        ``conflict`` ∈ {overwrite, rename, skip}.
        """

    @property
    def timeout(self) -> float:
        raw = self.config.get("timeout")
        try:
            return float(raw) if raw not in (None, "") else self.default_timeout
        except (TypeError, ValueError):
            return self.default_timeout
