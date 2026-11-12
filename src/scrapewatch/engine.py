"""Generic engine: runs any SiteConfig. Extracts raw strings (bronze); typing happens in silver via `coerce`."""

from __future__ import annotations

import re
import time
import traceback
from dataclasses import dataclass, field

from parsel import Selector

from scrapewatch.config import FieldSpec, SiteConfig
from scrapewatch.fetch import Fetcher, absolute, make_fetcher

# Markers of bot-challenge / interstitial pages. Only checked on pages that yielded too few items,
# so a quote that happens to contain "access denied" cannot raise a false alarm.
CHALLENGE_MARKERS = re.compile(
    r"captcha|cf-challenge|challenge-platform|verify you are (a )?human|are you a robot|unusual traffic|"
    r"access denied|attention required|request blocked|bot detection",
    re.I,
)
EMPTY_BODY_BYTES = 200


@dataclass
class PageResult:
    url: str
    status: int | None
    attempts: int
    elapsed_s: float
    error: str | None
    html: str
    n_items: int = 0
    block_reason: str | None = None  # http_403 | http_429 | challenge | empty_body


@dataclass
class ScrapeResult:
    site: str
    started_at: float
    finished_at: float = 0.0
    pages: list[PageResult] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)  # raw: {field: str | list[str] | None, "_page_url": str}
    fingerprint: set[str] = field(default_factory=set)  # DOM tag.class tokens across unblocked 2xx pages
    crash: str | None = None


def _values(node: Selector, spec: FieldSpec, page_url: str) -> list[str]:
    found = node.css(spec.css) if spec.css else node.xpath(spec.xpath)
    out = []
    for el in found:
        if spec.attr:
            v = el.attrib.get(spec.attr)
        elif isinstance(el.root, str):  # xpath selected an attribute / text node
            v = el.root
        else:
            v = " ".join(t.strip() for t in el.css("::text").getall() if t.strip())
        if v is None:
            continue
        v = " ".join(v.split())
        if v:
            out.append(absolute(page_url, v) if spec.absolute_url else v)
    return out


def extract_items(cfg: SiteConfig, html: str, page_url: str) -> list[dict]:
    sel = Selector(text=html)
    items = sel.css(cfg.item_css) if cfg.item_css else sel.xpath(cfg.item_xpath)
    records = []
    for item in items:
        rec: dict = {"_page_url": page_url}
        for name, spec in cfg.fields.items():
            vals = _values(item, spec, page_url)
            rec[name] = vals if spec.many else (vals[0] if vals else None)
        records.append(rec)
    return records


def fingerprint(html: str) -> set[str]:
    root = Selector(text=html).root
    tokens = set()
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tokens.add(el.tag)
        for cls in (el.get("class") or "").split():
            tokens.add(f"{el.tag}.{cls}")
    return tokens


def _next_url(cfg: SiteConfig, html: str, page_url: str) -> str | None:
    p = cfg.pagination
    if not (p.next_css or p.next_xpath):
        return None
    sel = Selector(text=html)
    found = sel.css(p.next_css) if p.next_css else sel.xpath(p.next_xpath)
    href = found.attrib.get("href") if found else None
    if href is None and found and isinstance(found[0].root, str):
        href = found[0].root
    return absolute(page_url, href) if href else None


def block_reason(cfg: SiteConfig, status: int | None, html: str, n_items: int) -> str | None:
    if status in (403, 429):
        return f"http_{status}"
    if status is not None and 200 <= status < 300 and n_items < cfg.monitor.min_items_per_page:
        if CHALLENGE_MARKERS.search(html):
            return "challenge"
        if len(html.strip()) < EMPTY_BODY_BYTES or not Selector(text=html).xpath("//body//text()[normalize-space()]"):
            return "empty_body"
    return None


def scrape(cfg: SiteConfig, fetcher: Fetcher | None = None) -> ScrapeResult:
    """Never raises for site problems: a crash is captured in result.crash so the monitor can classify it."""
    result = ScrapeResult(site=cfg.name, started_at=time.time())
    own = fetcher is None
    try:
        fetcher = fetcher or make_fetcher(cfg)
        seen: set[str] = set()
        for start in cfg.start_urls:
            url: str | None = str(start)
            for _ in range(cfg.pagination.max_pages):
                if url is None or url in seen:
                    break
                seen.add(url)
                resp = fetcher.fetch(url)
                page = PageResult(resp.url, resp.status, resp.attempts, resp.elapsed_s, resp.error, resp.text)
                result.pages.append(page)
                if resp.status is None or not 200 <= resp.status < 300:
                    page.block_reason = block_reason(cfg, resp.status, resp.text, 0)
                    break  # no page -> no next link
                records = extract_items(cfg, resp.text, resp.url)
                page.n_items = len(records)
                page.block_reason = block_reason(cfg, resp.status, resp.text, len(records))
                result.records.extend(records)
                if not page.block_reason:  # a challenge page's DOM says nothing about the site's layout
                    result.fingerprint |= fingerprint(resp.text)
                url = _next_url(cfg, resp.text, resp.url)
    except Exception:
        result.crash = traceback.format_exc(limit=5)
    finally:
        if own and fetcher is not None:
            fetcher.close()
        result.finished_at = time.time()
    return result


def coerce(spec: FieldSpec, raw):
    """Raw string(s) -> typed value. Returns (value, ok). ok=False means present but unparseable (format drift)."""
    if raw is None or raw == []:
        return (None, True)
    if spec.many:
        vals = [coerce(spec.model_copy(update={"many": False}), v) for v in raw]
        return ([v for v, _ in vals], all(ok for _, ok in vals))
    s = raw
    if spec.regex:
        m = re.search(spec.regex, s)
        if not m:
            return (None, False)
        s = m.group(1) if m.groups() else m.group(0)
    try:
        return ({"str": str, "int": int, "float": float}[spec.type](s), True)
    except ValueError:
        return (None, False)
