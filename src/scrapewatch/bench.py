"""Measurements behind the README's Results table.

faultbench: every fault in testsite.FAULTS, injected after a clean baseline, against the real engine + monitor.
realbench:  the full pipeline on the toscrape.com practice sites, run twice (second run = baseline + dedup).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from scrapewatch import pipeline
from scrapewatch.config import ROOT, SiteConfig
from scrapewatch.fetch import Fetcher
from scrapewatch.lake import LocalLake
from scrapewatch.testsite import CLEAN, FAULTS, TestSite

WARMUP = 3


def _run(site_cfg: SiteConfig, lake) -> dict:
    fetcher = Fetcher(site_cfg, sleep=lambda s: None)  # retries/backoff still happen, without wall-clock waits
    with fetcher:
        m = pipeline.step_scrape(site_cfg.name, lake=lake, cfg=site_cfg, fetcher=fetcher)
    return pipeline.step_monitor(m, lake=lake, cfg=site_cfg, reports_dir=None)


def faultbench(clean_runs: int = 5, out: Path | None = ROOT / "reports" / "faultbench.json") -> dict:
    faults, clean = [], []
    with TestSite() as site, tempfile.TemporaryDirectory() as tmp:
        cfg = SiteConfig.model_validate(site.config())
        for fault, expected in FAULTS.items():
            lake = LocalLake(Path(tmp) / fault)
            site.set("clean")
            for _ in range(WARMUP):
                clean.append({"scenario": "clean (warm-up)", **_run(cfg, lake)})
            site.set(fault)
            r = _run(cfg, lake)
            classes = {i.split(":", 1)[1] for i in r["issues"]}
            faults.append(
                {
                    "fault": fault,
                    "expected": expected,
                    "detected": expected in classes,
                    "severity": r["severity"],
                    "issues": r["issues"],
                    "other_classes": sorted(classes - {expected}),
                }
            )
        for scenario in CLEAN:
            lake = LocalLake(Path(tmp) / f"clean-{scenario}")
            site.set("clean")
            for _ in range(WARMUP):
                _run(cfg, lake)
            for _ in range(clean_runs):
                site.set(scenario)
                clean.append({"scenario": scenario, **_run(cfg, lake)})
    false_pos = [c for c in clean if c["issues"]]
    summary = {
        "faults_injected": len(faults),
        "faults_detected": sum(f["detected"] for f in faults),
        "faults_with_extra_classes": sum(bool(f["other_classes"]) for f in faults),
        "clean_runs": len(clean),
        "clean_runs_with_any_issue": len(false_pos),
        "per_fault": {f["fault"]: f"{'detected' if f['detected'] else 'MISSED'} ({f['severity']})" for f in faults},
    }
    result = {"summary": summary, "faults": faults, "false_positives": false_pos}
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
    return result


def realbench(sites: list[str] | None = None, out: Path = ROOT / "reports" / "bench.json") -> dict:
    lake_dir = ROOT / "lake-bench"
    shutil.rmtree(lake_dir, ignore_errors=True)  # our own scratch lake: numbers must come from a clean slate
    lake = LocalLake(lake_dir)
    runs = []
    for i in (1, 2):
        t0 = time.monotonic()
        res = pipeline.run(sites, lake=lake)
        runs.append({"run": i, "wall_s": round(time.monotonic() - t0, 1), **res})
    bronze_bytes = sum(p.stat().st_size for p in (lake_dir / "bronze").rglob("*.parquet"))
    html_bytes = sum(m["bronze"]["html_bytes"] for m in _history_metrics(lake))
    summary = {
        "sites": {
            m["site"]: {k: m[k] for k in ("items", "pages", "duration_s", "severity", "issues")}
            for m in runs[0]["monitor"]
        },
        "second_run_severity": {m["site"]: m["severity"] for m in runs[1]["monitor"]},
        "silver_after_two_runs": runs[1]["silver"],
        "dbt": runs[1]["dbt"],
        "gate": runs[1]["gate"],
        "wall_s_per_run": [r["wall_s"] for r in runs],
        "bronze_parquet_bytes": bronze_bytes,
        "raw_html_bytes": html_bytes,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "runs": runs}, indent=2, default=str))
    return {"summary": summary, "runs": runs}


def _history_metrics(lake) -> list[dict]:
    return [json.loads(lake.get(p)) for p in lake.list("ops/runs") if p.endswith(".json")]
