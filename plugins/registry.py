"""Registry dei plugin: nuove sorgenti/destinazioni si autoregistano qui."""

from __future__ import annotations

from plugins.base import Destination, ImageSource

_SOURCES: dict[str, type[ImageSource]] = {}
_DESTINATIONS: dict[str, type[Destination]] = {}


def register_source(cls: type[ImageSource]) -> type[ImageSource]:
    """Decoratoro: registra una classe sorgente basandosi sul suo attributo ``type``."""
    if not cls.type:
        raise ValueError(f"{cls.__name__} deve definire un attributo 'type'")
    _SOURCES[cls.type] = cls
    return cls


def register_destination(cls: type[Destination]) -> type[Destination]:
    """Decoratoro: registra una classe destinazione."""
    if not cls.type:
        raise ValueError(f"{cls.__name__} deve definire un attributo 'type'")
    _DESTINATIONS[cls.type] = cls
    return cls


def get_source_class(source_type: str) -> type[ImageSource]:
    try:
        return _SOURCES[source_type]
    except KeyError:
        raise KeyError(f"sorgente sconosciuta: {source_type!r}") from None


def get_destination_class(dest_type: str) -> type[Destination]:
    try:
        return _DESTINATIONS[dest_type]
    except KeyError:
        raise KeyError(f"destinazione sconosciuta: {dest_type!r}") from None


def available_sources() -> list[type[ImageSource]]:
    return sorted(_SOURCES.values(), key=lambda c: c.label)


def available_destinations() -> list[type[Destination]]:
    return sorted(_DESTINATIONS.values(), key=lambda c: c.label)


def build_source(source_type: str, config: dict, **kwargs) -> ImageSource:
    return get_source_class(source_type)(config, **kwargs)


def build_destination(dest_type: str, config: dict, **kwargs) -> Destination:
    return get_destination_class(dest_type)(config, **kwargs)


def clear_registry() -> None:
    """Solo per i test."""
    _SOURCES.clear()
    _DESTINATIONS.clear()


__all__ = [
    "register_source",
    "register_destination",
    "get_source_class",
    "get_destination_class",
    "available_sources",
    "available_destinations",
    "build_source",
    "build_destination",
    "clear_registry",
]
