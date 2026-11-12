import pytest

from scrapewatch.config import FieldSpec, SiteConfig
from scrapewatch.engine import coerce, extract_items, scrape
from scrapewatch.fetch import Fetcher


def test_clean_scrape_follows_pagination_and_extracts_every_item(site, cfg):
    with Fetcher(cfg, sleep=lambda s: None) as f:
        r = scrape(cfg, fetcher=f)
    assert r.crash is None
    assert [p.status for p in r.pages] == [200] * 5
    assert len(r.records) == 100
    first = r.records[0]
    assert first["url"] == f"{site.url}/catalog/product-1.html"  # resolved to absolute
    assert first["title"] == "Product 1" and first["price_gbp"] == "£11.37" and first["rating"] == "star-rating Two"
    assert len({rec["url"] for rec in r.records}) == 100


def test_xpath_fields_and_many():
    cfg = SiteConfig.model_validate(
        {
            "name": "q_site",
            "start_urls": ["http://t.test/"],
            "item_xpath": "//div[@class='quote']",
            "fields": {
                "text": {"xpath": ".//span[@class='text']/text()", "required": True},
                "about": {"xpath": ".//a[contains(@href,'/author/')]/@href", "absolute_url": True},
                "tags": {"css": "a.tag", "many": True},
            },
            "key_fields": ["text"],
        }
    )
    html = (
        "<div class='quote'><span class='text'> “Be  yourself.” </span><a href='/author/x'>about</a>"
        "<a class='tag'>life</a><a class='tag'>truth</a></div><div class='quote'><span class='text'>Hi</span></div>"
    )
    recs = extract_items(cfg, html, "http://t.test/page/1/")
    assert recs[0]["text"] == "“Be yourself.”"  # whitespace normalised
    assert recs[0]["about"] == "http://t.test/author/x"
    assert recs[0]["tags"] == ["life", "truth"] and recs[1]["tags"] == [] and recs[1]["about"] is None


@pytest.mark.parametrize(
    "spec, raw, expected",
    [
        ({"regex": r"([0-9]+\.[0-9]{2})", "type": "float"}, "£51.77", (51.77, True)),
        ({"regex": r"([0-9]+\.[0-9]{2})", "type": "float"}, "£51,77", (None, False)),  # format drift
        ({"type": "int"}, "12", (12, True)),
        ({"type": "int"}, "twelve", (None, False)),
        ({}, None, (None, True)),  # missing is not a type error
        ({"many": True, "type": "int"}, ["1", "2"], ([1, 2], True)),
    ],
)
def test_coerce(spec, raw, expected):
    assert coerce(FieldSpec(css="x", **spec), raw) == expected


@pytest.mark.browser
def test_browser_fetcher_renders_javascript_pages(site):
    """The /js/ pages build their items in JS: httpx sees none, Playwright sees all 100."""
    pytest.importorskip("playwright")
    base = {"start_urls": [f"{site.url}/js/page-1.html"]}
    http = scrape(SiteConfig.model_validate(site.config(**base)))
    browser = scrape(
        SiteConfig.model_validate(site.config(fetcher="browser", wait_for_css="article.product_pod", **base))
    )
    assert browser.crash is None, browser.crash
    assert len(http.records) == 0
    assert len(browser.records) == 100 and len(browser.pages) == 5
