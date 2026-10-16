"""SourceHTTP — recupera immagini da un URL template con verifica del content-type.

Supporta la riutilizzazione di una sessione di login esterna tramite cookie
letti da variabile d'ambiente o da file (aggiornabili senza riavvio).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import httpx

from app.ratelimit import RateLimiter
from plugins.base import (
    AuthError,
    ConfigError,
    FetchResult,
    FieldSpec,
    ImageSource,
    NetworkError,
    NotFoundError,
    TooLargeError,
    format_template,
    require,
)
from plugins.registry import register_source

DEFAULT_UA = "b2bimg/1.0 (internal product-image fetcher; +https://localhost)"


def _parse_header_lines(raw: str) -> dict[str, str]:
    """Converte righe ``Nome: valore`` in una mappa di header HTTP."""
    headers: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ConfigError(f"header non valido (manca ':'): {line!r}")
        name, _, value = line.partition(":")
        headers[name.strip()] = value.strip()
    return headers


@register_source
class SourceHTTP(ImageSource):
    """Sorgente HTTP(S): ``GET`` su un URL template, verifica ``200`` + immagine."""

    type: ClassVar[str] = "http"
    label: ClassVar[str] = "HTTP / URL template"
    description: ClassVar[str] = (
        "Chiama un URL tipo https://esempio.it/img/{codice}.jpg e verifica che "
        "la risorsa esista (status 200 + content-type immagine)."
    )
    fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "url_template",
            "URL template",
            required=True,
            placeholder="https://esempio.it/img/{codice}.jpg",
            help="Placeholders disponibili: {codice}.",
        ),
        FieldSpec(
            "cookie_env",
            "Cookie (variabile d'ambiente)",
            placeholder="SORGENTE_HTTP_COOKIE",
            help="Nome della variabile d'ambiente che contiene l'header Cookie "
            "di una sessione già autenticata (session reuse).",
        ),
        FieldSpec(
            "cookie_file",
            "Cookie (file)",
            placeholder="data/cookies/esempio.txt",
            help="File di testo contenente nella prima riga l'header Cookie. "
            "Riletto a ogni richiesta: puoi aggiornarlo senza riavviare.",
        ),
        FieldSpec("headers", "Header extra", type="textarea", placeholder="X-Tenant: acme"),
        FieldSpec("user_agent", "User-Agent", default=DEFAULT_UA),
        FieldSpec("timeout", "Timeout (secondi)", type="number", default="15"),
        FieldSpec("verify_ssl", "Verifica SSL", type="checkbox", default=True),
        FieldSpec(
            "require_image",
            "Verifica content-type immagine",
            type="checkbox",
            default=True,
            help="Se spuntato, un content-type non `image/*` viene trattato come non trovato.",
        ),
    )

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

    # --- utilità ---------------------------------------------------------

    def _cookie_header(self) -> str:
        parts: list[str] = []
        env_name = str(self.config.get("cookie_env") or "").strip()
        if env_name:
            import os

            value = os.environ.get(env_name, "").strip()
            if not value:
                raise ConfigError(f"variabile d'ambiente {env_name} assente o vuota")
            parts.append(value)
        file_name = str(self.config.get("cookie_file") or "").strip()
        if file_name:
            path = Path(file_name).expanduser()
            if not path.is_file():
                raise ConfigError(f"file cookie non trovato: {file_name}")
            first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()
            value = next((ln.strip() for ln in first_line if ln.strip()), "")
            if not value:
                raise ConfigError(f"file cookie vuoto: {file_name}")
            parts.append(value)
        return "; ".join(parts)

    def _headers(self) -> dict[str, str]:
        headers = _parse_header_lines(str(self.config.get("headers") or ""))
        headers.setdefault("User-Agent", str(self.config.get("user_agent") or DEFAULT_UA))
        headers.setdefault("Accept", "image/*,*/*;q=0.8")
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        return headers

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

    @property
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                verify=bool(self.config.get("verify_ssl", True)),
                transport=self._transport,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # --- fetch -----------------------------------------------------------

    def fetch(self, product_code: str) -> FetchResult:
        if not str(product_code or "").strip():
            raise NotFoundError("codice prodotto vuoto")
        url = self.build_url(str(product_code).strip())
        self.acquire_slot()
        try:
            response = self._http.get(url, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise NetworkError(f"timeout su {url}") from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"errore di rete su {url}: {exc}") from exc

        status = response.status_code
        if status in (401, 403):
            raise AuthError(
                f"accesso negato ({status}) su {url}: cookie/sessione non validi"
            )
        if status == 429:
            raise NetworkError(f"rate limit remoto (429) su {url}")
        if status >= 500:
            raise NetworkError(f"errore server ({status}) su {url}")
        if status != 200:
            raise NotFoundError(f"risorsa assente ({status}) su {url}")

        content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        if self.config.get("require_image", True) and not content_type.startswith("image/"):
            kind = content_type or "mancante"
            raise NotFoundError(f"content-type non immagine ({kind}) su {url}")

        content = bytearray()
        try:
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > self.max_bytes:
                    raise TooLargeError(f"immagine oltre {self.max_bytes} byte: {url}")
        except httpx.HTTPError as exc:
            raise NetworkError(f"lettura body fallita su {url}: {exc}") from exc

        return FetchResult(
            location=url, content=bytes(content), content_type=content_type or None
        )
