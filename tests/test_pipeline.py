"""silver -> dbt build -> DQ gate on a small synthetic lake shaped like the three shipped site configs."""

import json

import pyarrow as pa
import pytest

from scrapewatch import pipeline
from scrapewatch.lake import LocalLake, put_parquet

QUOTES = [("“Quote one.”", "Ann", ["life", "truth"]), ("“Quote two.”", "Bob", ["life"]), ("“Three.”", "Ann", [])]


def _bronze(lake, site, recs, run_id="r1", ts="2026-11-12T00:00:00+00:00"):
    rows = [{"run_id": run_id, "scraped_at": ts, "page_url": "http://x/", "record_json": json.dumps(r)} for r in recs]
    put_parquet(lake, f"bronze/records/site={site}/dt=2026-11-12/run_id={run_id}.parquet", pa.Table.from_pylist(rows))
    report = {"run_id": f"{site}-{run_id}", "site": site, "started_at": ts, "duration_s": 1.5, "pages": 1,
              "items": len(recs), "requests": 1, "retries": 0, "severity": "ok", "issues": []}  # fmt: skip
    lake.put(f"ops/runs/site={site}/{site}-{run_id}.json", json.dumps(report).encode())


def _seed(lake, js_quotes=QUOTES, bad_books=0):
    books = [
        {"url": f"http://b/{i}", "title": f"B{i}", "price_gbp": f"£{10 + i}.50",
         "rating": f"star-rating {['One', 'Two', 'Three', 'Four', 'Five'][i % 5]}", "availability": "In stock"}
        for i in range(20)
    ]  # fmt: skip
    books += [
        {"url": f"http://b/bad{i}", "title": "X", "price_gbp": "n/a", "rating": "star-rating One"}
        for i in range(bad_books)
    ]
    _bronze(lake, "books_toscrape", books)
    _bronze(lake, "books_toscrape", books[:20], run_id="r2", ts="2026-11-12T06:00:00+00:00")  # full re-scrape
    for site, quotes in [("quotes_toscrape", QUOTES), ("quotes_toscrape_js", js_quotes)]:
        _bronze(lake, site, [{"text": t, "author": a, "tags": g} for t, a, g in quotes])


def _ok_monitor():
    return [{"site": s, "severity": "ok", "issues": []} for s in ("books_toscrape", "quotes_toscrape")]


def test_silver_dbt_build_and_gate(tmp_path):
    lake = LocalLake(tmp_path / "lake")
    _seed(lake)
    stats = {s["site"]: s for s in pipeline.step_silver(lake=lake)}
    assert stats["books_toscrape"] | {} == {"site": "books_toscrape", "bronze_records": 40, "quarantined": 0,
        "valid": 40, "silver_records": 20, "duplicates_removed": 20, "changed_records": 0}  # fmt: skip
    dbt = pipeline.step_dbt(lake=lake, db_path=tmp_path / "gold.duckdb")
    assert dbt["tests"] >= 20 and dbt["tests_passed"] == dbt["tests"]

    import duckdb

    con = duckdb.connect(str(tmp_path / "gold.duckdb"))
    assert con.execute("select sum(n_books) from main_gold.mart_books_by_rating").fetchone()[0] == 20
    assert con.execute("select n_quotes from main_gold.mart_tag_popularity where tag = 'life'").fetchone()[0] == 2
    assert con.execute("select n_quotes from main_gold.mart_author_quotes where author = 'Ann'").fetchone()[0] == 2
    assert con.execute("select sum(runs) from main_gold.mart_scrape_health").fetchone()[0] == 4

    assert pipeline.step_gate(_ok_monitor(), list(stats.values()), dbt)["gate"] == "passed"


def test_dbt_parity_test_catches_a_browser_extraction_gap(tmp_path):
    lake = LocalLake(tmp_path / "lake")
    _seed(lake, js_quotes=QUOTES[:2])  # the JS scrape lost a quote
    pipeline.step_silver(lake=lake)
    with pytest.raises(RuntimeError, match="dbt build failed"):
        pipeline.step_dbt(lake=lake, db_path=tmp_path / "gold.duckdb")


@pytest.mark.parametrize(
    "monitor, silver, dbt, match",
    [
        ([{"site": "a", "severity": "critical", "issues": ["critical:blocked"]}], [], {}, "a: critical:blocked"),
        (
            [],
            [{"site": "a", "bronze_records": 100, "quarantined": 6, "silver_records": 94}],
            {},
            "quarantine rate 6.0%",
        ),
        ([], [{"site": "a", "bronze_records": 0, "quarantined": 0, "silver_records": 0}], {}, "silver is empty"),
        ([], [], {"tests": 5, "tests_passed": 4}, "4/5 tests passed"),
    ],
)
def test_gate_fails_loudly(monitor, silver, dbt, match):
    with pytest.raises(pipeline.GateFailed, match=match):
        pipeline.step_gate(monitor, silver, dbt)


def test_gate_tolerates_warnings():
    mon = [{"site": "a", "severity": "warning", "issues": ["warning:volume_anomaly"]}]
    silver = [{"site": "a", "bronze_records": 100, "quarantined": 1, "silver_records": 99}]
    assert pipeline.step_gate(mon, silver, {"tests": 3, "tests_passed": 3})["gate"] == "passed"
