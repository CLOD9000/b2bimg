"""SourceHTML — individua l'immagine prodotto analizzando pagine HTML.

Pensato per i fornitori di cui si conosce solo il dominio: se non si conosce il
pattern dell'URL immagine, il plugin costruisce l'URL della pagina (prodotto o
risultati di ricerca), scarica l'HTML ed estrae l'immagine da ``og:image``,
JSON-LD o dai tag ``<img>``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import urldefrag, urlparse

import httpx

from app.ratelimit import RateLimiter
from plugins.base import (
    AuthError,
    ConfigError,
    FetchResult,
    FieldSpec,
    ImageSource,
    NotFoundError,
    format_template,
)
from plugins.registry import register_source
from plugins.sources._htmlscan import (
    extract_image_urls,
    extract_links,
    looks_like_product_page,
)
from plugins.sources._httpbase import (
    ACCEPT_HTML,
    ACCEPT_IMAGE,
    COOKIE_HEADER_FIELDS,
    HTTPImageMixin,
)

# Pattern di ricerca più diffusi, provati quando si conosce solo il dominio.
# Quello WooCommerce (`post_type=product`) è primo: porta direttamente alla
# pagina del singolo prodotto quando la ricerca ha un solo risultato.
COMMON_SEARCH_PATTERNS: tuple[str, ...] = (
    "/?s={codice}&post_type=product",
    "/?s={codice}",
    "/search?q={codice}",
    "/ricerca?q={codice}",
    "/search?search={codice}",
    "/catalogsearch/result/?q={codice}",
    "/?q={codice}",
)

# Segmenti di percorso tipici delle pagine prodotto.
_PRODUCT_PATH_HINTS = ("/prodott", "/product", "/prodotto", "/products")


def _code_variants(code: str) -> list[str]:
    """Varianti del codice usate per confrontare URL/file.

    Molti siti salvano l'immagine col codice base senza il suffisso variante:
    es. ``306.2085.NO`` → ``306.2085``, ``125.0100.10.M`` → ``125.0100.10``.
    """
    low = code.lower()
    variants = {low}
    head, _, tail = low.rpartition(".")
    if head and tail.isalpha():
        variants.add(head)
    return [v for v in variants if v]


def _matches_code(text: str, code: str) -> bool:
    low = text.lower()
    return any(variant in low for variant in _code_variants(code))


def _followable_links(html: str, page_url: str, code: str) -> list[str]:
    """Link a pagine prodotto da seguire, escludendo self-link e feed."""
    page_norm = urldefrag(page_url)[0].rstrip("/")
    out: list[str] = []
    for link in extract_links(html, page_url, code):
        clean = urldefrag(link)[0]
        if not clean.startswith(("http://", "https://")):
            continue
        if clean.rstrip("/") == page_norm:
            continue
        low = clean.lower()
        if "/feed" in low or "wp-json" in low or low.endswith((".xml", ".rss")):
            continue
        path = urlparse(clean).path
        # Le varianti della ricerca (es. /?s=CODICE, /?s=CODICE/feed) non sono
        # pagine prodotto: richiediamo un percorso vero.
        if not path or path == "/":
            continue
        if _matches_code(clean, code) or any(hint in path.lower() for hint in _PRODUCT_PATH_HINTS):
            out.append(clean)
    return out


_SPECIFIC_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "base_url",
        "Dominio del fornitore",
        placeholder="https://esempio.it",
        help="Usato per provare le ricerche più comuni. Ha priorità l'URL "
        "template (pagina prodotto o ricerca) se compilato.",
    ),
    FieldSpec(
        "page_template",
        "URL pagina prodotto",
        placeholder="https://esempio.it/prodotto/{codice}",
        help="Se conosci la pagina del prodotto. Placeholder: {codice}.",
    ),
    FieldSpec(
        "search_template",
        "URL ricerca",
        placeholder="https://esempio.it/?s={codice}",
        help="Pagina di ricerca del sito. Placeholder: {codice}.",
    ),
    FieldSpec(
        "auto_discovery",
        "Prova ricerche comuni",
        type="checkbox",
        default=True,
        help="Se non indichi alcun template, prova i pattern di ricerca diffusi "
        "sul dominio.",
    ),
    FieldSpec(
        "follow_links",
        "Segui i link della pagina",
        type="checkbox",
        default=True,
        help="Dalla pagina di ricerca, segui i link che contengono il codice.",
    ),
    FieldSpec(
        "max_pages",
        "Pagine max per codice",
        type="number",
        default="8",
        help="Include le pagine di ricerca provate e i link seguiti.",
    ),
)


@register_source
class SourceHTML(HTTPImageMixin, ImageSource):
    """Sorgente HTML: risolve l'immagine analizzando la pagina web del prodotto."""

    type: ClassVar[str] = "html"
    label: ClassVar[str] = "HTML / scraping pagina prodotto"
    description: ClassVar[str] = (
        "Trova l'immagine partendo dal dominio o dalla pagina prodotto: scarica "
        "l'HTML ed estrae immagine da og:image, JSON-LD o tag <img>."
    )
    fields: ClassVar[tuple[FieldSpec, ...]] = (*_SPECIFIC_FIELDS, *COOKIE_HEADER_FIELDS)

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

    # --- configurazione --------------------------------------------------

    @property
    def max_pages(self) -> int:
        raw = self.config.get("max_pages")
        try:
            value = int(str(raw)) if raw not in (None, "") else 8
        except (TypeError, ValueError):
            value = 8
        return max(1, min(value, 25))

    def _format(self, template: str, code: str, context: str) -> str:
        if "{codice}" not in template and "{code}" not in template:
            raise ConfigError(f"{context} deve contenere il placeholder {{codice}}")
        url = format_template(template, {"codice": code, "code": code}, context=context)
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"solo URL http/https sono consentiti: {url!r}")
        return url

    def entry_urls(self, code: str) -> list[tuple[str, str]]:
        """Costruisce gli URL di ingresso come coppie ``(url, tipo)``.

        ``tipo`` è ``"product"`` (da cui estrarre le immagini) oppure ``"search"``
        (da cui estrarre solo i link, senza mai scaricare l'immagine generica del
        sito).
        """
        page_template = str(self.config.get("page_template") or "").strip()
        if page_template:
            return [(self._format(page_template, code, "page_template"), "product")]

        search_template = str(self.config.get("search_template") or "").strip()
        if search_template:
            return [(self._format(search_template, code, "search_template"), "search")]

        base_url = str(self.config.get("base_url") or "").strip().rstrip("/")
        if base_url and self.config.get("auto_discovery", True):
            if not base_url.startswith(("http://", "https://")):
                raise ConfigError(f"solo URL http/https sono consentiti: {base_url!r}")
            return [
                (self._format(base_url + pattern, code, "ricerca"), "search")
                for pattern in COMMON_SEARCH_PATTERNS
            ]

        raise ConfigError(
            "servono almeno uno tra 'base_url' (con auto-discovery), "
            "'page_template' o 'search_template'"
        )

    # --- fetch -----------------------------------------------------------

    def fetch(self, product_code: str) -> FetchResult:
        code = str(product_code or "").strip()
        if not code:
            raise NotFoundError("codice prodotto vuoto")

        budget = self.max_pages
        state = {"done": 0, "tried": []}

        def fetch_page(url: str) -> str | None:
            if state["done"] >= budget:
                return None
            self.acquire_slot()
            state["done"] += 1
            response = self._get(url, accept=ACCEPT_HTML)
            status = response.status_code
            if status in (401, 403):
                raise AuthError(
                    f"accesso negato ({status}) su {url}: cookie/sessione non validi"
                )
            if status != 200:
                return None
            return response.text

        def resolve_page(url: str, kind: str) -> tuple[FetchResult | None, list[str]]:
            state["tried"].append(url)
            html = fetch_page(url)
            if html is None:
                return None, []
            # Di una pagina prodotto (template o riconosciuta come tale) ci
            # fidiamo; su ricerca/archivio accettiamo solo immagini col codice.
            trusted = kind == "product" or looks_like_product_page(html)
            images = extract_image_urls(html, url, code)
            if not trusted:
                images = [img for img in images if _matches_code(img, code)]
            for image_url in images:
                result = self._download(image_url, url)
                if result is not None:
                    return result, []
            # Seguiamo i link solo dalle pagine di ricerca e solo verso pagine
            # prodotto reali (mai self-link/breadcrumb/feed).
            if trusted or kind != "search" or not self.config.get("follow_links", True):
                return None, []
            return None, _followable_links(html, url, code)

        queue = list(self.entry_urls(code))
        while queue and state["done"] < budget:
            url, kind = queue.pop(0)
            result, links = resolve_page(url, kind)
            if result is not None:
                return result
            # I link trovati hanno priorità sulle restanti pagine di ricerca.
            queue[0:0] = [(link, "link") for link in links]

        tried = ", ".join(dict.fromkeys(state["tried"])) or "nessuna"
        raise NotFoundError(
            f"immagine non trovata per il codice {code!r} (pagine provate: {tried})"
        )

    def _download(self, image_url: str, page_url: str) -> FetchResult | None:
        """Scarica ``image_url``; torna ``None`` per i candidati non validi."""
        try:
            self.acquire_slot()
            response = self._get(image_url, accept=ACCEPT_IMAGE)
            content, content_type = self._read_image_body(response, image_url)
        except NotFoundError:
            return None
        return FetchResult(
            location=image_url,
            content=content,
            content_type=content_type,
            meta={"page": page_url},
        )


__all__ = ["COMMON_SEARCH_PATTERNS", "SourceHTML"]
