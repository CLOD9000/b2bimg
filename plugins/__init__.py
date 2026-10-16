"""Plugin sorgenti/destinazioni: import dei moduli per attivare l'autoregistrazione."""

from __future__ import annotations

from plugins import destinations as _destinations  # noqa: F401
from plugins import sources as _sources  # noqa: F401
from plugins.base import (
    AuthError,
    ConfigError,
    Destination,
    FetchResult,
    ImageSource,
    NetworkError,
    NotFoundError,
    PluginError,
    PutResult,
    format_template,
    resolve_config,
)
from plugins.registry import (
    available_destinations,
    available_sources,
    build_destination,
    build_source,
    get_destination_class,
    get_source_class,
    register_destination,
    register_source,
)

__all__ = [
    "AuthError",
    "ConfigError",
    "Destination",
    "FetchResult",
    "ImageSource",
    "NetworkError",
    "NotFoundError",
    "PluginError",
    "PutResult",
    "available_destinations",
    "available_sources",
    "build_destination",
    "build_source",
    "format_template",
    "get_destination_class",
    "get_source_class",
    "register_destination",
    "register_source",
    "resolve_config",
]
