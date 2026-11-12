import random

import httpx
import pytest

from scrapewatch.config import USER_AGENT, SiteConfig
from scrapewatch.fetch import Fetcher, RateLimiter, backoff_delay


def _cfg(**kw):
    return SiteConfig.model_validate(
        {
            "name": "t_site",
            "start_urls": ["http://t.test/a"],
            "item_css": "div",
            "fields": {"a": {"css": "a", "required": True}},
            "key_fields": ["a"],
            "respect_robots": False,
            "rate_limit": {"requests_per_second": 10},
            "retry": {"max_attempts": 4, "backoff_base_s": 1, "backoff_max_s": 8},
        }
        | kw
    )


def _fetcher(statuses, cfg=None, headers=None):
    """A Fetcher over a scripted transport; returns (fetcher, seen requests, recorded sleeps)."""
    seen, sleeps = [], []
    script = iter(statuses)

    def handler(request):
        seen.append(request)
        s = next(script)
        if isinstance(s, Exception):
            raise s
        return httpx.Response(s, text=f"<html>{s}</html>", headers=headers or {})

    f = Fetcher(cfg or _cfg(), sleep=sleeps.append, transport=httpx.MockTransport(handler))
    return f, seen, sleeps


def test_backoff_is_exponential_with_full_jitter_and_capped():
    rng = random.Random(0)
    for attempt in range(1, 8):
        for _ in range(50):
            d = backoff_delay(attempt, base=0.5, cap=10, rng=rng)
            assert 0 <= d <= min(10, 0.5 * 2 ** (attempt - 1))
    assert backoff_delay(1, 0.5, 10, retry_after=7, rng=rng) == 7  # Retry-After is a floor
    assert backoff_delay(1, 0.5, 10, retry_after=60, rng=rng) == 10  # ...but still capped


@pytest.mark.parametrize("transient", [429, 500, 502, 503, 504, httpx.ConnectError("refused")])
def test_retries_transient_failures_then_succeeds(transient):
    f, seen, sleeps = _fetcher([transient, transient, 200])
    r = f.fetch("http://t.test/a")
    assert (r.status, r.attempts, r.error) == (200, 3, None)
    assert len(seen) == 3 and len(sleeps) >= 2  # two backoff sleeps (plus rate-limit waits)


@pytest.mark.parametrize("status", [403, 404, 401, 410])
def test_does_not_retry_non_transient_4xx(status):
    f, seen, _ = _fetcher([status])
    r = f.fetch("http://t.test/a")
    assert (r.status, r.attempts) == (status, 1) and len(seen) == 1


def test_gives_up_after_max_attempts():
    f, seen, _ = _fetcher([503] * 4)
    r = f.fetch("http://t.test/a")
    assert (r.status, r.attempts, r.error) == (503, 4, "http_503") and len(seen) == 4


def test_network_error_exhaustion_has_no_status():
    f, _, _ = _fetcher([httpx.ReadTimeout("slow")] * 4)
    r = f.fetch("http://t.test/a")
    assert r.status is None and r.error.startswith("ReadTimeout")


def test_retry_after_header_is_honoured():
    f, _, sleeps = _fetcher([429, 200], headers={"Retry-After": "5"})
    f.fetch("http://t.test/a")
    assert 5 in sleeps


def test_sends_identifying_user_agent():
    f, seen, _ = _fetcher([200])
    f.fetch("http://t.test/a")
    assert seen[0].headers["user-agent"] == USER_AGENT


def test_rate_limiter_spaces_requests_per_host():
    now = [100.0]
    slept = []
    rl = RateLimiter(sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0])
    rl.set_interval("a", 0.5)
    rl.wait("a")
    rl.wait("a")
    rl.wait("b")  # other host: no wait
    assert slept == [0.5]


def test_robots_disallow_is_respected(site):
    cfg = SiteConfig.model_validate(site.config())
    with Fetcher(cfg, sleep=lambda s: None) as f:
        r = f.fetch(f"{site.url}/private/page.html")
        ok = f.fetch(f"{site.url}/catalog/page-1.html")
    assert r.error == "robots_disallowed" and r.status is None
    assert "/private/page.html" not in site.hits  # never requested
    assert ok.status == 200


@pytest.mark.parametrize("robots_status, allowed", [(404, True), (500, False)])
def test_robots_unavailable_follows_rfc9309(robots_status, allowed):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status)
        return httpx.Response(200, text="<html/>")

    f = Fetcher(_cfg(respect_robots=True), sleep=lambda s: None, transport=httpx.MockTransport(handler))
    assert f.allowed("http://t.test/a") is allowed


def test_robots_crawl_delay_slows_the_limiter():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 3\n")
        return httpx.Response(200, text="<html/>")

    sleeps = []
    f = Fetcher(_cfg(respect_robots=True), sleep=sleeps.append, transport=httpx.MockTransport(handler))
    f.fetch("http://t.test/a")
    f.fetch("http://t.test/b")
    assert any(s > 2.5 for s in sleeps)
