"""Polite fetching: robots.txt, per-host rate limiting, retries with backoff + jitter.

Retries happen on 429, 5xx and network errors only. 403/404 and other 4xx are returned as-is:
retrying a block just digs the hole deeper, and the monitor needs to see it.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from scrapewatch.config import USER_AGENT, SiteConfig

RETRYABLE = {429, 500, 502, 503, 504}


@dataclass
class Response:
    url: str
    status: int | None  # None = never got an HTTP response (network error / robots)
    text: str
    attempts: int
    elapsed_s: float
    error: str | None = None


def backoff_delay(attempt: int, base: float, cap: float, retry_after: float | None = None, rng=random) -> float:
    """Full-jitter exponential backoff (attempt is 1-based); a server's Retry-After is a floor, capped at `cap`."""
    delay = rng.uniform(0, min(cap, base * 2 ** (attempt - 1)))
    if retry_after is not None:
        delay = max(delay, retry_after)
    return min(delay, cap)


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:  # HTTP-date form: ignore, jittered backoff still applies
        return None


class RateLimiter:
    """Minimum interval between requests to the same host (the engine is sequential, so no locking)."""

    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self._last: dict[str, float] = {}
        self._interval: dict[str, float] = {}
        self.sleep, self.clock = sleep, clock

    def set_interval(self, host: str, seconds: float) -> None:
        self._interval[host] = max(self._interval.get(host, 0.0), seconds)

    def wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            remaining = self._interval.get(host, 0.0) - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self._last[host] = self.clock()


class Fetcher:
    """HTTP fetcher (httpx). BrowserFetcher swaps only `_get`."""

    def __init__(self, cfg: SiteConfig, sleep=time.sleep, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.sleep = sleep
        self.limiter = RateLimiter(sleep=sleep)
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=cfg.retry.timeout_s,
            follow_redirects=True,
            transport=transport,
        )
        self._robots: dict[str, RobotFileParser] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self.client.close()

    # robots.txt (RFC 9309: 4xx -> allow all, 5xx / unreachable -> disallow all)
    def allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        parts = urlparse(url)
        host = parts.netloc
        if host not in self._robots:
            rp = RobotFileParser()
            self.limiter.set_interval(host, 1 / self.cfg.rate_limit.requests_per_second)
            self.limiter.wait(host)
            try:
                r = self.client.get(f"{parts.scheme}://{host}/robots.txt")
                if r.status_code >= 500:
                    rp.disallow_all = True
                elif r.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp.disallow_all = True
            delay = rp.crawl_delay(USER_AGENT)
            if delay:
                self.limiter.set_interval(host, float(delay))
            self._robots[host] = rp
        return self._robots[host].can_fetch(USER_AGENT, url)

    def _get(self, url: str) -> tuple[int, str, str, float | None]:
        r = self.client.get(url)
        return r.status_code, r.text, str(r.url), _retry_after(r.headers.get("retry-after"))

    def fetch(self, url: str) -> Response:
        start = time.monotonic()
        if not self.allowed(url):
            return Response(url, None, "", 0, 0.0, error="robots_disallowed")
        host = urlparse(url).netloc
        self.limiter.set_interval(host, 1 / self.cfg.rate_limit.requests_per_second)
        r = self.cfg.retry
        status, text, error, final_url = None, "", None, url
        for attempt in range(1, r.max_attempts + 1):
            self.limiter.wait(host)
            retry_after = None
            try:
                status, text, final_url, retry_after = self._get(url)
                error = None
                if status not in RETRYABLE:
                    break
                error = f"http_{status}"
            except Exception as e:  # httpx.TransportError, playwright Error: both mean "no usable response"
                if not _is_network_error(e):
                    raise
                status, text, error = None, "", f"{type(e).__name__}: {e}"[:300]
            if attempt < r.max_attempts:
                self.sleep(backoff_delay(attempt, r.backoff_base_s, r.backoff_max_s, retry_after))
        return Response(final_url, status, text, attempt, time.monotonic() - start, error)


def _is_network_error(e: Exception) -> bool:
    if isinstance(e, httpx.TransportError):
        return True
    return type(e).__module__.startswith("playwright")


class BrowserFetcher(Fetcher):
    """Playwright Chromium for JS-rendered pages. robots.txt and rate limiting still go through Fetcher."""

    def __init__(self, cfg: SiteConfig, sleep=time.sleep):
        super().__init__(cfg, sleep=sleep)
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._page = self._browser.new_page(user_agent=USER_AGENT)

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()
        super().close()

    def _get(self, url: str) -> tuple[int, str, str, float | None]:
        resp = self._page.goto(url, timeout=self.cfg.retry.timeout_s * 1000, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        if self.cfg.wait_for_css and status < 400:
            try:
                self._page.wait_for_selector(self.cfg.wait_for_css, timeout=self.cfg.retry.timeout_s * 1000)
            except Exception:  # noqa: S110 - selector never rendered: return what we have, the monitor judges it
                pass
        retry_after = _retry_after(resp.headers.get("retry-after")) if resp else None
        return status, self._page.content(), self._page.url, retry_after


def make_fetcher(cfg: SiteConfig, **kw) -> Fetcher:
    return BrowserFetcher(cfg, **kw) if cfg.fetcher == "browser" else Fetcher(cfg, **kw)


def absolute(base: str, href: str) -> str:
    return urljoin(base, href)
