import json

from scrapewatch import silver
from scrapewatch.config import SiteConfig


def _cfg(site):
    return SiteConfig.model_validate(site.config())


def _row(run, ts, **rec):
    base = {"url": "http://x/1", "title": "A", "price_gbp": "£1.00", "rating": "star-rating One", "availability": None}
    return {"run_id": run, "scraped_at": ts, "page_url": "http://x/", "record_json": json.dumps(base | rec)}


def test_dedup_by_key_keeps_latest_and_counts_versions(site):
    rows = [
        _row("r1", "2026-01-01", url="http://x/1"),
        _row("r1", "2026-01-01", url="http://x/2"),
        _row("r2", "2026-01-02", url="http://x/1"),  # unchanged re-scrape
        _row("r2", "2026-01-02", url="http://x/2", price_gbp="£2.50"),  # price changed
    ]
    table, quarantine, stats = silver.transform(_cfg(site), rows)
    out = {r["url"]: r for r in table.to_pylist()}
    assert stats == {"site": "testsite", "bronze_records": 4, "quarantined": 0, "valid": 4, "silver_records": 2,
                     "duplicates_removed": 2, "changed_records": 1}  # fmt: skip
    assert out["http://x/2"]["price_gbp"] == 2.5 and out["http://x/2"]["content_versions"] == 2
    assert out["http://x/1"]["times_seen"] == 2 and out["http://x/1"]["content_versions"] == 1
    assert out["http://x/1"]["first_seen_at"] == "2026-01-01" and out["http://x/1"]["last_seen_at"] == "2026-01-02"


def test_invalid_records_are_quarantined_with_reasons_never_dropped(site):
    rows = [
        _row("r1", "t", url="http://x/1"),
        _row("r1", "t", url="http://x/2", price_gbp="£2,50"),
        _row("r1", "t", url="http://x/3", title=None),
        _row("r1", "t", url="http://x/4", rating="unrated"),
    ]
    table, q, stats = silver.transform(_cfg(site), rows)
    reasons = {json.loads(r["record_json"])["url"]: r["reasons"] for r in q.to_pylist()}
    assert table.num_rows == 1 and stats["quarantined"] == 3
    assert reasons == {
        "http://x/2": ["price_gbp: unparseable '£2,50'"],
        "http://x/3": ["title: missing"],
        "http://x/4": ["rating: unparseable 'unrated'"],
    }
    assert stats["valid"] + stats["quarantined"] == stats["bronze_records"]


def test_silver_schema_is_typed(site):
    schema = silver.silver_schema(_cfg(site))
    assert str(schema.field("price_gbp").type) == "double"
    assert str(schema.field("rating").type) == "string"
