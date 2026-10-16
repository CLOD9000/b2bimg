"""Utility HTTP condivise dalle sorgenti web (SourceHTTP, SourceHTML).

Centralizza la gestione di cookie/session reuse, header, timeout e della
validazione delle risposte immagine, così i singoli plugin restano sottili.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from plugins.base import (
    AuthError,
    ConfigError,
    FieldSpec,
    NetworkError,
    NotFoundError,
    TooLargeError,
)

DEFAULT_UA = "b2bimg/1.0 (internal product-image fetcher; +https://localhost)"

# Header di accettazione predefiniti per scaricare immagini.
ACCEPT_IMAGE = "image/*,*/*;q=0.8"
# Header di accettazione per scaricare pagine HTML da analizzare.
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# Campi di configurazione comuni a tutte le sorgenti web.
COOKIE_HEADER_FIELDS: tuple[FieldSpec, ...] = (
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


def parse_header_lines(raw: str) -> dict[str, str]:
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


class HTTPImageMixin:
    """Mixin per le sorgenti basate su ``httpx``.

    La sottoclasse deve impostare ``self._transport`` (per i test) nel proprio
    ``__init__``; il client viene creato pigramente e riusato per la durata del job.
    """

    _client: httpx.Client | None = None
    _transport: httpx.BaseTransport | None = None

    # --- client e header -------------------------------------------------

    @property
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,  # type: ignore[attr-defined]
                follow_redirects=True,
                verify=bool(self.config.get("verify_ssl", True)),  # type: ignore[attr-defined]
                transport=self._transport,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _cookie_header(self) -> str:
        parts: list[str] = []
        env_name = str(self.config.get("cookie_env") or "").strip()  # type: ignore[attr-defined]
        if env_name:
            value = os.environ.get(env_name, "").strip()
            if not value:
                raise ConfigError(f"variabile d'ambiente {env_name} assente o vuota")
            parts.append(value)
        file_name = str(self.config.get("cookie_file") or "").strip()  # type: ignore[attr-defined]
        if file_name:
            path = Path(file_name).expanduser()
            if not path.is_file():
                raise ConfigError(f"file cookie non trovato: {file_name}")
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            value = next((ln.strip() for ln in lines if ln.strip()), "")
            if not value:
                raise ConfigError(f"file cookie vuoto: {file_name}")
            parts.append(value)
        return "; ".join(parts)

    def _headers(self, accept: str = ACCEPT_IMAGE) -> dict[str, str]:
        headers = parse_header_lines(str(self.config.get("headers") or ""))  # type: ignore[attr-defined]
        headers.setdefault(
            "User-Agent", str(self.config.get("user_agent") or DEFAULT_UA)  # type: ignore[attr-defined]
        )
        headers.setdefault("Accept", accept)
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    # --- richieste -------------------------------------------------------

    def _get(self, url: str, *, accept: str = ACCEPT_IMAGE) -> httpx.Response:
        """Esegue una GET mappando gli errori di trasporto in ``NetworkError``.

        Non solleva per gli status HTTP non-2xx: la valutazione dello status è a
        carico del chiamante (le pagine di ricerca possono rispondere 404).
        """
        headers = self._headers(accept)
        try:
            return self._http.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise NetworkError(f"timeout su {url}") from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"errore di rete su {url}: {exc}") from exc

    def _read_image_body(self, response: httpx.Response, url: str) -> tuple[bytes, str | None]:
        """Valida status/content-type e legge il body entro ``max_bytes``."""
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
        if self.config.get("require_image", True) and not content_type.startswith("image/"):  # type: ignore[attr-defined]
            kind = content_type or "mancante"
            raise NotFoundError(f"content-type non immagine ({kind}) su {url}")

        content = bytearray()
        try:
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > self.max_bytes:  # type: ignore[attr-defined]
                    raise TooLargeError(
                        f"immagine oltre {self.max_bytes} byte: {url}"  # type: ignore[attr-defined]
                    )
        except httpx.HTTPError as exc:
            raise NetworkError(f"lettura body fallita su {url}: {exc}") from exc

        return bytes(content), content_type or None
