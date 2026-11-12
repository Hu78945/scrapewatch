"""A local catalogue site whose markup and behaviour can be broken on demand.

Used by the test suite and by `scrapewatch faultbench` to inject each failure class the monitor claims to detect.
Served on 127.0.0.1 only. Shaped like books.toscrape.com so the same kind of config scrapes it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RATINGS = ["One", "Two", "Three", "Four", "Five"]

# fault -> monitor class it must raise. Clean scenarios must raise nothing.
FAULTS = {
    "selector_rename": "selector_breakage",  # item container class renamed -> 0 items
    "field_rename": "selector_breakage",  # required field's class renamed -> 100% null
    "optional_field_missing": "selector_breakage",  # optional field gone -> null-rate jump (warning)
    "price_format": "schema_drift",  # "£12,34": present but no longer parses as float
    "layout_drift": "schema_drift",  # page re-skinned, selectors still match (early warning)
    "rate_limited": "blocked",  # persistent 429
    "forbidden": "blocked",  # 403
    "captcha": "blocked",  # 200 + challenge interstitial
    "empty_body": "blocked",  # 200 + empty document
    "drop_items": "volume_anomaly",  # 8 of 20 items per page
    "truncated_pagination": "volume_anomaly",  # next link disappears after page 2
    "server_error": "fetch_error",  # persistent 503
    "crash_markup": "crash",  # malformed next-page URL makes the scraper raise
}
CLEAN = ["clean", "transient_429", "transient_503", "slight_growth"]


class TestSite:
    __test__ = False  # not a pytest class

    def __init__(self, pages: int = 5, per_page: int = 20):
        self.pages, self.per_page = pages, per_page
        self.fault = "clean"
        self.hits: dict[str, int] = {}
        self.webhooks: list[dict] = []
        self.robots = "User-agent: *\nDisallow: /private/\n"
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                status, body, headers = site.route(self.path)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "text/plain" if self.path.endswith(".txt") else "text/html")
                self.end_headers()
                self.wfile.write(body.encode())

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                site.webhooks.append(json.loads(self.rfile.read(n)))
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def set(self, fault: str) -> None:
        self.fault, self.hits = fault, {}

    # ---- routing -------------------------------------------------------------------------------------------------
    def route(self, path: str) -> tuple[int, str, dict]:
        if path == "/robots.txt":
            return 200, self.robots, {}
        self.hits[path] = self.hits.get(path, 0) + 1
        f = self.fault
        if not path.startswith("/catalog/page-"):
            return 404, "<html><body>not found</body></html>", {}
        n = int(path.removeprefix("/catalog/page-").removesuffix(".html"))
        if f == "rate_limited":
            return 429, "<html><body>Too Many Requests</body></html>", {"Retry-After": "0"}
        if f == "transient_429" and self.hits[path] <= 2:
            return 429, "<html><body>Too Many Requests</body></html>", {"Retry-After": "0"}
        if f == "server_error" or (f == "transient_503" and self.hits[path] <= 1):
            return 503, "<html><body>Service Unavailable</body></html>", {}
        if f == "forbidden":
            return 403, "<html><body><h1>403 Forbidden</h1></body></html>", {}
        if f == "captcha":
            return 200, CAPTCHA, {}
        if f == "empty_body":
            return 200, "", {}
        pages = self.pages + 1 if f == "slight_growth" else self.pages
        if n > pages:
            return 404, "<html><body>not found</body></html>", {}
        return 200, self.render(n, pages), {}

    def render(self, n: int, pages: int) -> str:
        f = self.fault
        count = 8 if f == "drop_items" else self.per_page
        item_cls = "product-card" if f == "selector_rename" else "product_pod"
        price_cls = "price" if f == "field_rename" else "price_color"
        items = []
        for i in range(count):
            pid = (n - 1) * self.per_page + i + 1
            price = f"£{10 + pid * 1.37:.2f}"
            if f == "price_format":
                price = price.replace(".", ",")
            avail = "" if f == "optional_field_missing" else '<p class="instock availability">In stock</p>'
            items.append(
                f'<li class="{"tile" if f == "layout_drift" else "col-xs-6"}"><article class="{item_cls}">'
                f'<p class="star-rating {RATINGS[pid % 5]}"></p>'
                f'<h3><a href="product-{pid}.html" title="Product {pid}">Product {pid}</a></h3>'
                f'<div class="{"cost" if f == "layout_drift" else "product_price"}">'
                f'<p class="{price_cls}">{price}</p>{avail}</div></article></li>'
            )
        nxt = ""
        last = 2 if f == "truncated_pagination" else pages
        if n < last:
            href = "http://[broken/page.html" if f == "crash_markup" else f"page-{n + 1}.html"
            nxt = f'<li class="next"><a href="{href}">next</a></li>'
        if f == "layout_drift":  # re-skinned: new wrapper classes everywhere, item/field selectors unchanged
            shell = ("app-shell", "grid", "topbar", "brand", "crumbs", "listing", "pager", "footer-v2")
        else:
            shell = ("container-fluid", "page_inner", "page-header", "action", "breadcrumb", "row", "pager", "footer")
        c = dict(zip(("wrap", "inner", "head", "brand", "crumbs", "list", "pager", "foot"), shell, strict=True))
        return (
            f'<!DOCTYPE html><html><head><title>Catalogue page {n}</title></head><body>'
            f'<div class="{c["wrap"]}"><div class="{c["inner"]}">'
            f'<header class="{c["head"]}"><a class="{c["brand"]}" href="/">Test Catalogue</a></header>'
            f'<ul class="{c["crumbs"]}"><li class="crumb-home"><a href="/">Home</a></li></ul>'
            f'<section><ol class="{c["list"]}">{"".join(items)}</ol>'
            f'<ul class="{c["pager"]}"><li class="current">Page {n} of {pages}</li>{nxt}</ul></section>'
            f'<footer class="{c["foot"]}"><p class="copyright">test fixture</p></footer>'
            f"</div></div></body></html>"
        )

    def config(self, **overrides) -> dict:
        """A SiteConfig dict for this site (validated by the caller)."""
        cfg = {
            "name": "testsite",
            "start_urls": [f"{self.url}/catalog/page-1.html"],
            "item_css": "article.product_pod",
            "fields": {
                "url": {"css": "h3 a", "attr": "href", "absolute_url": True, "required": True},
                "title": {"css": "h3 a", "attr": "title", "required": True},
                "price_gbp": {"css": "p.price_color", "regex": r"([0-9]+\.[0-9]{2})", "type": "float", "required": True},
                "rating": {"css": "p.star-rating", "attr": "class", "regex": r"star-rating (\w+)", "required": True},
                "availability": {"css": "p.availability"},
            },
            "key_fields": ["url"],
            "pagination": {"next_css": "li.next a", "max_pages": 20},
            "rate_limit": {"requests_per_second": 10},
            "retry": {"max_attempts": 3, "backoff_base_s": 0.0, "backoff_max_s": 0.0, "timeout_s": 5},
        }
        cfg.update(overrides)
        return cfg


CAPTCHA = (
    "<!DOCTYPE html><html><head><title>Attention Required</title></head><body>"
    '<div class="cf-wrapper"><h1>Please verify you are a human</h1>'
    '<div id="challenge-form" class="g-recaptcha" data-sitekey="test"></div>'
    "<p>Our systems have detected unusual traffic from your network.</p></div></body></html>"
)
