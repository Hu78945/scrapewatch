import pytest
from pydantic import ValidationError

from scrapewatch.config import SiteConfig, load_all

BASE = {
    "name": "x_site",
    "start_urls": ["https://quotes.toscrape.com/"],
    "item_css": "div.quote",
    "fields": {"text": {"css": "span.text", "required": True}},
    "key_fields": ["text"],
}


def test_shipped_configs_validate_and_match_file_names():
    cfgs = load_all()
    assert set(cfgs) == {"books_toscrape", "quotes_toscrape", "quotes_toscrape_js"}
    assert cfgs["quotes_toscrape_js"].fetcher == "browser"
    assert all(str(u).split("/")[2].endswith("toscrape.com") for c in cfgs.values() for u in c.start_urls)


@pytest.mark.parametrize(
    "patch, match",
    [
        ({"fields": {"text": {"css": "a", "xpath": "//a", "required": True}}}, "exactly one of css / xpath"),
        ({"fields": {"text": {"required": True}}}, "exactly one of css / xpath"),
        ({"item_xpath": "//div"}, "exactly one of item_css / item_xpath"),
        ({"key_fields": ["nope"]}, "key_fields not defined"),
        ({"fields": {"text": {"css": "span.text"}}}, "at least one field must be required"),
        ({"wait_for_css": "div"}, "only applies to fetcher: browser"),
        ({"name": "Bad Name"}, "String should match pattern"),
        ({"start_urls": ["ftp://example.com/"]}, "URL scheme should be"),
        ({"rate_limit": {"requests_per_second": 50}}, "less than or equal to 10"),
        ({"selectors": {}}, "Extra inputs are not permitted"),
        ({"fields": {"text": {"css": "a", "type": "date", "required": True}}}, "Input should be"),
    ],
)
def test_invalid_configs_are_rejected(patch, match):
    with pytest.raises(ValidationError, match=match):
        SiteConfig.model_validate(BASE | patch)


def test_defaults_are_polite():
    c = SiteConfig.model_validate(BASE)
    assert c.respect_robots and c.rate_limit.requests_per_second == 1.0 and c.retry.max_attempts == 4
