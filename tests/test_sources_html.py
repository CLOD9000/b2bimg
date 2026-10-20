"""Test della sorgente HTML/scraping e dell'estrattore di immagini."""

from __future__ import annotations

import httpx
import pytest

from plugins.base import AuthError, ConfigError, NotFoundError
from plugins.sources._htmlscan import extract_image_urls, extract_links
from plugins.sources.html import SourceHTML

JPEG = b"\xff\xd8\xff\xe0fakejpeg"


def _html_source(handler, **config) -> SourceHTML:
    payload = {"base_url": "https://fornitore.test", "require_image": True}
    payload.update(config)
    return SourceHTML(payload, transport=httpx.MockTransport(handler))


# --- estrattore (unit) -------------------------------------------------------


def test_extract_prefers_og_image() -> None:
    html = (
        '<html><head>'
        '<meta property="og:image" content="/media/AB-1.jpg">'
        "</head><body>"
        '<img src="/thumb/small.png">'
        "</body></html>"
    )
    urls = extract_image_urls(html, "https://fornitore.test/prodotto/AB-1", "AB-1")
    assert urls[0] == "https://fornitore.test/media/AB-1.jpg"


def test_extract_from_jsonld() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Product","image":["https://cdn.test/AB-2-big.jpg"]}'
        "</script>"
    )
    urls = extract_image_urls(html, "https://fornitore.test/p/AB-2", "AB-2")
    assert urls == ["https://cdn.test/AB-2-big.jpg"]


def test_extract_prioritizes_img_containing_code() -> None:
    html = (
        '<img src="/assets/banner.png">'
        '<img src="/assets/AB-9.jpg">'
    )
    urls = extract_image_urls(html, "https://fornitore.test/x", "AB-9")
    assert urls[0] == "https://fornitore.test/assets/AB-9.jpg"


def test_extract_srcset_and_skip_hints() -> None:
    html = (
        '<img src="/sprite.png">'
        '<img srcset="/img/AB-3.jpg 1x, /img/AB-3@2x.jpg 2x">'
    )
    urls = extract_image_urls(html, "https://fornitore.test/x", "AB-3")
    assert urls == ["https://fornitore.test/img/AB-3.jpg"]


def test_extract_links_prioritizes_code() -> None:
    html = (
        '<a href="/categoria/varie">Varie</a>'
        '<a href="/prodotto/AB-5">AB-5</a>'
        '<a href="#top">torna su</a>'
    )
    links = extract_links(html, "https://fornitore.test/ricerca", "AB-5")
    assert links[0] == "https://fornitore.test/prodotto/AB-5"
    assert all("#" not in link for link in links)


# --- SourceHTML: pagina template ---------------------------------------------


def test_html_page_template_og_image() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prodotto/AB-1":
            return httpx.Response(
                200,
                text='<meta property="og:image" content="/media/AB-1.jpg">',
                headers={"content-type": "text/html"},
            )
        if request.url.path == "/media/AB-1.jpg":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    source = _html_source(
        handler, page_template="https://fornitore.test/prodotto/{codice}"
    )
    result = source.fetch("AB-1")
    assert result.content == JPEG
    assert result.location == "https://fornitore.test/media/AB-1.jpg"
    assert result.meta["page"] == "https://fornitore.test/prodotto/AB-1"


def test_html_image_content_type_checked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prodotto/AB-1":
            return httpx.Response(
                200,
                text='<meta property="og:image" content="/not-an-image">',
                headers={"content-type": "text/html"},
            )
        return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})

    source = _html_source(
        handler, page_template="https://fornitore.test/prodotto/{codice}"
    )
    with pytest.raises(NotFoundError):
        source.fetch("AB-1")


def test_html_auth_error_on_403() -> None:
    source = _html_source(
        lambda request: httpx.Response(403),
        page_template="https://fornitore.test/prodotto/{codice}",
    )
    with pytest.raises(AuthError):
        source.fetch("AB-1")


# --- SourceHTML: solo dominio + auto discovery -------------------------------


def test_html_only_domain_follows_search_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = request.url.query.decode()
        if path == "/" and "s=AB-7" in query:
            return httpx.Response(
                200,
                text='<a href="/prodotto/AB-7">Risultato</a>',
                headers={"content-type": "text/html"},
            )
        if path == "/prodotto/AB-7":
            return httpx.Response(
                200,
                text='<img src="/img/AB-7.jpg">',
                headers={"content-type": "text/html"},
            )
        if path == "/img/AB-7.jpg":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    source = _html_source(handler, auto_discovery=True, follow_links=True)
    result = source.fetch("AB-7")
    assert result.content == JPEG
    assert result.location == "https://fornitore.test/img/AB-7.jpg"


def test_html_only_domain_ignores_generic_search_image() -> None:
    """La pagina di ricerca (e le pagine generiche) non devono fornire l'immagine."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in {"/", "/search", "/ricerca"}:
            # Ricerca senza risultati, ma con l'og:image generico del sito e un
            # link a una pagina generica (categoria) che non cita il codice.
            return httpx.Response(
                200,
                text=(
                    '<meta property="og:image" content="/logo.png">'
                    '<a href="/categoria/varie">Varie</a>'
                    "<p>0 risultati</p>"
                ),
                headers={"content-type": "text/html"},
            )
        if path == "/categoria/varie":
            return httpx.Response(
                200,
                text='<meta property="og:image" content="/banner.png">',
                headers={"content-type": "text/html"},
            )
        if path in {"/logo.png", "/banner.png"}:
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    source = _html_source(handler, auto_discovery=True, follow_links=True, max_pages="4")
    with pytest.raises(NotFoundError):
        source.fetch("AB-999")


def test_html_search_page_uses_code_thumbnail() -> None:
    """Dalla pagina di ricerca si usa la miniatura col codice, non il logo."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/" and "s=AB-7" in request.url.query.decode():
            return httpx.Response(
                200,
                text=(
                    '<meta property="og:image" content="/logo.png">'
                    '<img src="/img/AB-7.jpg">'
                ),
                headers={"content-type": "text/html"},
            )
        if path == "/img/AB-7.jpg":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    source = _html_source(handler, auto_discovery=True, follow_links=True)
    result = source.fetch("AB-7")
    assert result.content == JPEG
    assert result.location == "https://fornitore.test/img/AB-7.jpg"


def test_html_page_template_ignores_code_in_url_requirement() -> None:
    """Con page_template la pagina è già quella del prodotto: usa le sue immagini."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/p/12345":
            return httpx.Response(
                200,
                text='<meta property="og:image" content="/media/foto.jpg">',
                headers={"content-type": "text/html"},
            )
        if path == "/media/foto.jpg":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    source = _html_source(handler, page_template="https://fornitore.test/p/{codice}")
    result = source.fetch("12345")
    assert result.content == JPEG
    assert result.location == "https://fornitore.test/media/foto.jpg"


def test_html_search_redirect_to_product_page_is_trusted() -> None:
    """WooCommerce: la ricerca reindirizza alla pagina prodotto (og:type=product)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/" and "post_type=product" in request.url.query.decode():
            return httpx.Response(302, headers={"location": "/prodotti/vernice-600c/"})
        if path == "/prodotti/vernice-600c/":
            return httpx.Response(
                200,
                text=(
                    '<meta property="og:type" content="product">'
                    '<body class="single-product">'
                    '<meta property="og:image" content="/media/306.2085.jpg">'
                ),
                headers={"content-type": "text/html"},
            )
        if path == "/media/306.2085.jpg":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    source = _html_source(handler, auto_discovery=True, follow_links=True)
    result = source.fetch("306.2085.NO")
    assert result.content == JPEG
    assert result.location == "https://fornitore.test/media/306.2085.jpg"


def test_html_matches_code_base_without_variant_suffix() -> None:
    """L'immagine può chiamarsi col codice base (306.2085) senza '.NO'."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/":
            return httpx.Response(
                200,
                text='<img src="/img/306.2085-300x300.jpg">',
                headers={"content-type": "text/html"},
            )
        if path == "/img/306.2085-300x300.jpg":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    source = _html_source(handler, auto_discovery=True, follow_links=True)
    result = source.fetch("306.2085.NO")
    assert result.location == "https://fornitore.test/img/306.2085-300x300.jpg"


def test_html_does_not_follow_self_and_feed_links() -> None:
    """Breadcrumb/feed che contengono il codice non vanno trattati come prodotto."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                '<meta property="og:image" content="/bgn-bubbles-footer.png">'
                '<a href="/?s=AB-3/#breadcrumb">breadcrumb</a>'
                '<a href="/?s=AB-3/feed/rss2/">feed</a>'
            ),
            headers={"content-type": "text/html"},
        )

    source = _html_source(handler, auto_discovery=False, follow_links=True, max_pages="2")
    source.config["base_url"] = "https://fornitore.test"
    source.config["search_template"] = "https://fornitore.test/?s={codice}"
    with pytest.raises(NotFoundError):
        source.fetch("AB-3")


def test_html_no_config_raises_config_error() -> None:
    source = SourceHTML(
        {"base_url": "", "auto_discovery": False},
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    with pytest.raises(ConfigError):
        source.fetch("AB-1")


def test_html_template_requires_placeholder() -> None:
    source = _html_source(
        lambda request: httpx.Response(200),
        page_template="https://fornitore.test/prodotto/fisso",
    )
    with pytest.raises(ConfigError):
        source.fetch("AB-1")


def test_html_respects_max_pages() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            text="<p>nessun risultato</p>",
            headers={"content-type": "text/html"},
        )

    source = _html_source(handler, auto_discovery=True, follow_links=False, max_pages="3")
    with pytest.raises(NotFoundError):
        source.fetch("AB-1")
    assert calls["n"] == 3


def test_html_network_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    source = _html_source(
        handler, page_template="https://fornitore.test/prodotto/{codice}"
    )
    from plugins.base import NetworkError

    with pytest.raises(NetworkError):
        source.fetch("AB-1")
