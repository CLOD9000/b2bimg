"""SourceHTTP — recupera immagini da un URL template con verifica del content-type.

Supporta la riutilizzazione di una sessione di login esterna tramite cookie
letti da variabile d'ambiente o da file (aggiornabili senza riavvio).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from app.ratelimit import RateLimiter
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
from plugins.sources._httpbase import (
    ACCEPT_IMAGE,
    COOKIE_HEADER_FIELDS,
    DEFAULT_UA,
    HTTPImageMixin,
)

__all__ = ["DEFAULT_UA", "SourceHTTP"]

_URL_TEMPLATE_FIELD = FieldSpec(
    "url_template",
    "URL template",
    required=True,
    placeholder="https://esempio.it/img/{codice}.jpg",
    help="Placeholders disponibili: {codice}.",
)


@register_source
class SourceHTTP(HTTPImageMixin, ImageSource):
    """Sorgente HTTP(S): ``GET`` su un URL template, verifica ``200`` + immagine."""

    type: ClassVar[str] = "http"
    label: ClassVar[str] = "HTTP / URL template"
    description: ClassVar[str] = (
        "Chiama un URL tipo https://esempio.it/img/{codice}.jpg e verifica che "
        "la risorsa esista (status 200 + content-type immagine)."
    )
    fields: ClassVar[tuple[FieldSpec, ...]] = (_URL_TEMPLATE_FIELD, *COOKIE_HEADER_FIELDS)

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        rate_limiter: RateLimiter | None = None,
        default_timeout: float = 15.0,
        max_bytes: int = 25 * 1024 * 1024,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            config,
            rate_limiter=rate_limiter,
            default_timeout=default_timeout,
            max_bytes=max_bytes,
        )
        self._transport = transport
        self._client: httpx.Client | None = None

    def build_url(self, product_code: str) -> str:
        template = require(self.config, "url_template", "URL template")
        if "{codice}" not in template and "{code}" not in template:
            raise ConfigError("l'URL template deve contenere il placeholder {codice}")
        url = format_template(
            template, {"codice": product_code, "code": product_code}, context="url_template"
        )
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"solo URL http/https sono consentiti: {url!r}")
        return url

    def fetch(self, product_code: str) -> FetchResult:
        if not str(product_code or "").strip():
            raise NotFoundError("codice prodotto vuoto")
        url = self.build_url(str(product_code).strip())
        self.acquire_slot()
        response = self._get(url, accept=ACCEPT_IMAGE)
        content, content_type = self._read_image_body(response, url)
        return FetchResult(location=url, content=content, content_type=content_type)
