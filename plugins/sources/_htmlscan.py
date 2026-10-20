"""Estrazione di URL immagine e link da pagine HTML (solo stdlib, nessuna dipendenza).

Usato da :class:`SourceHTML` per individuare l'immagine prodotto partendo da una
pagina: preferisce i metadati social (``og:image``), poi JSON-LD, poi ``<img>``.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# Estensioni tipiche delle immagini (per dare priorità ai candidati credibili).
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tiff")
# Sottostringhe che indicano risorse non utili (icone, placeholder, tracking).
SKIP_HINTS = (
    "sprite",
    "placeholder",
    "logo",
    "icon",
    "spinner",
    "loading",
    "/blank.",
    "pixel.",
    "transparent.",
)

_META_IMAGE_KEYS = (
    "og:image",
    "og:image:secure_url",
    "twitter:image",
    "twitter:image:src",
)


class HTMLScan(HTMLParser):
    """Raccoglie metadati immagine, ``<img>``, link e JSON-LD da una pagina."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[tuple[str, str]] = []
        self.link_image_src: list[str] = []
        self.images: list[dict[str, str]] = []
        self.anchors: list[str] = []
        self.base_href: str | None = None
        self.jsonld: list[str] = []
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").strip().lower()
            content = a.get("content", "").strip()
            if key and content:
                self.meta.append((key, content))
        elif tag == "link":
            if "image_src" in a.get("rel", "").lower() and a.get("href"):
                self.link_image_src.append(a["href"])
        elif tag == "base":
            if a.get("href"):
                self.base_href = a["href"]
        elif tag == "img":
            self.images.append(
                {
                    "src": a.get("src") or a.get("data-src") or a.get("data-original") or "",
                    "srcset": a.get("srcset") or a.get("data-srcset") or "",
                    "width": a.get("width", ""),
                    "height": a.get("height", ""),
                    "alt": a.get("alt", ""),
                }
            )
        elif tag == "source" and a.get("srcset"):
            self.images.append(
                {"src": "", "srcset": a["srcset"], "width": "", "height": "", "alt": ""}
            )
        elif tag == "a" and a.get("href", "").strip():
            self.anchors.append(a["href"].strip())
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._jsonld_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            blob = "".join(self._jsonld_buf).strip()
            if blob:
                self.jsonld.append(blob)


def _parse(html: str) -> HTMLScan:
    scan = HTMLScan()
    scan.feed(html or "")
    return scan


def _pick_from_srcset(srcset: str) -> str:
    """Torna il primo URL di un ``srcset`` (``url 2x, url2 640w``)."""
    first = srcset.split(",", 1)[0].strip()
    return first.split(" ", 1)[0].strip()


def _as_urls(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        url = value.get("url") or value.get("contentUrl")
        return [url] if isinstance(url, str) else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_as_urls(item))
        return out
    return []


def _jsonld_images(blob: str) -> list[str]:
    try:
        data = json.loads(blob)
    except ValueError:
        return []
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() == "image":
                    found.extend(_as_urls(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def is_image_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(IMAGE_EXTENSIONS)


def looks_like_product_page(html: str) -> bool:
    """Riconosce una pagina dettaglio prodotto (WooCommerce/OpenGraph).

    Serve a distinguere la pagina del singolo prodotto (di cui ci si può fidare)
    da una pagina di ricerca/archivio (dove le immagini sono spesso generiche).
    """
    low = (html or "").lower()
    if 'content="product"' in low and ("og:type" in low or "property='og:type'" in low):
        return True
    return "single-product" in low or "product-template" in low


def extract_image_urls(html: str, page_url: str, code: str = "") -> list[str]:
    """Restituisce gli URL immagine trovati, in ordine di attendibilità.

    L'ordine è: social meta → ``link[rel=image_src]`` → JSON-LD → ``<img>``
    (con priorità a quelli il cui filename contiene il codice prodotto).
    """
    scan = _parse(html)
    base = urljoin(page_url, scan.base_href) if scan.base_href else page_url
    urls: list[str] = []

    def add(raw: str) -> None:
        candidate = (raw or "").strip()
        if not candidate or candidate.startswith(("data:", "javascript:", "#")):
            return
        absolute = urljoin(base, candidate)
        if absolute not in urls:
            urls.append(absolute)

    for key, content in scan.meta:
        if key in _META_IMAGE_KEYS:
            add(content)
    for href in scan.link_image_src:
        add(href)
    for blob in scan.jsonld:
        for url in _jsonld_images(blob):
            add(url)

    code_lower = code.lower()
    scored: list[tuple[int, str]] = []
    for image in scan.images:
        raw = image["src"] or _pick_from_srcset(image["srcset"])
        if not raw:
            continue
        absolute = urljoin(base, raw.strip())
        low = absolute.lower()
        if any(hint in low for hint in SKIP_HINTS):
            continue
        try:
            width = int(image["width"]) if image["width"] else None
            height = int(image["height"]) if image["height"] else None
        except ValueError:
            width = height = None
        if (width is not None and width <= 2) or (height is not None and height <= 2):
            continue
        score = 0
        if code_lower and code_lower in low:
            score += 10
        if is_image_url(absolute):
            score += 5
        scored.append((-score, absolute))
    scored.sort()
    for _, url in scored:
        add(url)

    return urls


def extract_links(html: str, page_url: str, code: str = "") -> list[str]:
    """Restituisce i link della pagina, prioritizzando quelli che contengono il codice."""
    scan = _parse(html)
    base = urljoin(page_url, scan.base_href) if scan.base_href else page_url
    code_lower = code.lower()
    preferred: list[str] = []
    others: list[str] = []
    seen: set[str] = set()
    for href in scan.anchors:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base, href)
        if not absolute.startswith(("http://", "https://")) or absolute in seen:
            continue
        seen.add(absolute)
        if code_lower and code_lower in absolute.lower():
            preferred.append(absolute)
        else:
            others.append(absolute)
    return preferred + others
