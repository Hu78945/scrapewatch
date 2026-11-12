"""The key proof: break the local site in each way the monitor claims to detect, and check it says so.

Every fault runs after 3 clean runs (the baseline), against the real engine, fetcher, lake and monitor.
"""

import json

import pytest
from conftest import run_once

from scrapewatch import monitor
from scrapewatch.testsite import CLEAN, FAULTS


def _classes(result):
    return {i.split(":", 1)[1] for i in result["issues"]}


@pytest.mark.parametrize("fault, expected", FAULTS.items())
def test_each_injected_fault_is_detected_and_classified(site, cfg, lake, fault, expected):
    for _ in range(3):
        assert run_once(cfg, lake)["issues"] == []
    site.set(fault)
    r = run_once(cfg, lake)
    assert _classes(r) == {expected}, r["issues"]  # the right class, and only that one
    assert r["severity"] in {"warning", "critical"}


@pytest.mark.parametrize("scenario", CLEAN)
def test_clean_scenarios_raise_no_alarm(site, cfg, lake, scenario):
    for _ in range(3):
        run_once(cfg, lake)
    site.set(scenario)
    for _ in range(3):
        r = run_once(cfg, lake)
        assert r["issues"] == [] and r["severity"] == "ok"


def test_first_run_without_baseline_still_catches_absolute_failures(site, cfg, lake):
    site.set("field_rename")
    r = run_once(cfg, lake)
    assert "critical:selector_breakage" in r["issues"]


def test_transient_429_is_retried_not_reported(site, cfg, lake):
    site.set("transient_429")
    r = run_once(cfg, lake)
    assert r["issues"] == [] and r["items"] == 100
    report = json.loads(lake.get(f"ops/runs/site=testsite/{r['run_id']}.json"))
    assert report["retries"] == 10  # 2 per page x 5 pages


def test_broken_runs_never_become_the_baseline(site, cfg, lake):
    for _ in range(2):
        run_once(cfg, lake)
    site.set("drop_items")
    for _ in range(4):  # a persisting regression keeps alarming...
        assert "warning:volume_anomaly" in run_once(cfg, lake)["issues"]
    site.set("selector_rename")
    run_once(cfg, lake)
    site.set("clean")
    assert run_once(cfg, lake)["issues"] == []  # ...and the critical run did not poison the baseline


def test_report_files_and_markdown(site, cfg, lake, tmp_path):
    from scrapewatch import pipeline
    from scrapewatch.fetch import Fetcher

    site.set("captcha")
    with Fetcher(cfg, sleep=lambda s: None) as f:
        m = pipeline.step_scrape(cfg.name, lake=lake, cfg=cfg, fetcher=f)
    pipeline.step_monitor(m, lake=lake, cfg=cfg, reports_dir=tmp_path)
    md = (tmp_path / "latest-testsite.md").read_text()
    assert "CRITICAL" in md and "`blocked`" in md
    rep = json.loads((tmp_path / "latest-testsite.json").read_text())
    assert rep["blocked_pages"] == {"challenge": 1}


def test_alert_webhook_posts_only_at_or_above_threshold(site, cfg, lake, monkeypatch):
    monkeypatch.setenv("SCRAPEWATCH_ALERT_WEBHOOK", f"{site.url}/hook")
    assert run_once(cfg, lake)["alerted"] is False  # clean: nothing sent
    site.set("forbidden")
    assert run_once(cfg, lake)["alerted"] is True
    (payload,) = site.webhooks
    assert payload["severity"] == "critical" and payload["issues"][0]["class"] == "blocked"
    assert "traceback" not in json.dumps(payload) and "fingerprint" not in payload
    monkeypatch.setenv("SCRAPEWATCH_ALERT_MIN_SEVERITY", "critical")
    site.set("drop_items")
    run_once(cfg, lake)  # warning only
    assert len(site.webhooks) == 1


def test_alerting_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SCRAPEWATCH_ALERT_WEBHOOK", raising=False)
    assert monitor.alert({"severity": "critical"}) is False


def test_alert_outage_does_not_fail_the_run(monkeypatch):
    monkeypatch.setenv("SCRAPEWATCH_ALERT_WEBHOOK", "http://127.0.0.1:9/unreachable")
    report = {
        "site": "s",
        "run_id": "r",
        "severity": "critical",
        "items": 0,
        "pages": 0,
        "started_at": "",
        "issues": [],
    }
    report |= {"pages_ok": 0, "requests": 0, "retries": 0, "duration_s": 0, "baseline_runs": 0, "baseline_items": None}
    assert monitor.alert(report) is False
