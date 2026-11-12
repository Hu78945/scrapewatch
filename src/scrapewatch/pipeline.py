"""Pipeline steps shared by the CLI and the Airflow DAG: scrape -> monitor -> silver -> dbt build -> DQ gate."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa

from scrapewatch import monitor, silver
from scrapewatch.config import ROOT, SiteConfig, load_all
from scrapewatch.engine import scrape
from scrapewatch.lake import LocalLake, open_lake, put_parquet

REPORTS = ROOT / "reports"
DBT_DIR = ROOT / "dbt"
MAX_QUARANTINE_RATE = 0.05


def configs(sites: list[str] | None = None) -> dict[str, SiteConfig]:
    allc = load_all()
    unknown = set(sites or []) - set(allc)
    if unknown:
        raise SystemExit(f"unknown site(s): {sorted(unknown)}; known: {sorted(allc)}")
    return {k: v for k, v in allc.items() if not sites or k in sites}


def silver_root(lake) -> Path:
    """Where dbt-duckdb reads silver: the lake itself when local, a mirrored working copy when on Azure."""
    return lake.root / "silver" if isinstance(lake, LocalLake) else ROOT / "warehouse" / "silver"


# ---- bronze ----------------------------------------------------------------------------------------------------
def write_bronze(lake, cfg: SiteConfig, result, run_id: str) -> dict:
    dt = datetime.fromtimestamp(result.started_at, UTC)
    part = f"site={cfg.name}/dt={dt:%Y-%m-%d}/run_id={run_id}.parquet"
    scraped_at = dt.isoformat()
    pages = pa.Table.from_pylist(
        [
            {
                "run_id": run_id,
                "url": p.url,
                "status": p.status,
                "attempts": p.attempts,
                "elapsed_s": round(p.elapsed_s, 4),
                "error": p.error,
                "block_reason": p.block_reason,
                "n_items": p.n_items,
                "html_sha256": hashlib.sha256(p.html.encode()).hexdigest(),
                "html": p.html,
            }
            for p in result.pages
        ],
        schema=pa.schema(
            [
                ("run_id", pa.string()),
                ("url", pa.string()),
                ("status", pa.int32()),
                ("attempts", pa.int32()),
                ("elapsed_s", pa.float64()),
                ("error", pa.string()),
                ("block_reason", pa.string()),
                ("n_items", pa.int32()),
                ("html_sha256", pa.string()),
                ("html", pa.large_string()),
            ]
        ),  # fmt: skip
    )
    records = pa.Table.from_pylist(
        [
            {
                "run_id": run_id,
                "scraped_at": scraped_at,
                "page_url": r["_page_url"],
                "record_json": json.dumps({k: v for k, v in r.items() if k != "_page_url"}, ensure_ascii=False),
            }
            for r in result.records
        ],
        schema=pa.schema(
            [
                ("run_id", pa.string()),
                ("scraped_at", pa.string()),
                ("page_url", pa.string()),
                ("record_json", pa.string()),
            ]
        ),
    )
    put_parquet(lake, f"bronze/pages/{part}", pages)
    put_parquet(lake, f"bronze/records/{part}", records)
    return {"pages": pages.num_rows, "records": records.num_rows, "html_bytes": sum(len(p.html) for p in result.pages)}


# ---- run history -----------------------------------------------------------------------------------------------
def history(lake, site: str) -> list[dict]:
    return [json.loads(lake.get(p)) for p in lake.list(f"ops/runs/site={site}") if p.endswith(".json")]


def step_scrape(site: str, lake=None, cfg: SiteConfig | None = None, fetcher=None) -> dict:
    """Scrape one site into bronze. Returns the run's measurements (input to step_monitor)."""
    lake = lake or open_lake()
    cfg = cfg or configs([site])[site]
    result = scrape(cfg, fetcher=fetcher)
    metrics = monitor.measure(cfg, result)
    metrics["bronze"] = write_bronze(lake, cfg, result, metrics["run_id"])
    return metrics


def step_monitor(metrics: dict, lake=None, cfg: SiteConfig | None = None, reports_dir: Path | None = REPORTS) -> dict:
    """Classify the run against the site's history, persist it to the run-history log, write reports, alert."""
    lake = lake or open_lake()
    cfg = cfg or configs([metrics["site"]])[metrics["site"]]
    report = monitor.assess(cfg, metrics, history(lake, cfg.name))
    lake.put(f"ops/runs/site={cfg.name}/{report['run_id']}.json", monitor.to_json(report))
    if reports_dir is not None:
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / f"latest-{cfg.name}.json").write_bytes(monitor.to_json(report))
        (reports_dir / f"latest-{cfg.name}.md").write_text(monitor.markdown(report))
    report["alerted"] = monitor.alert(report)
    return {k: report[k] for k in ("site", "run_id", "severity", "items", "pages", "duration_s", "alerted")} | {
        "issues": [f"{i['severity']}:{i['class']}" for i in report["issues"]]
    }


def step_silver(sites: list[str] | None = None, lake=None) -> list[dict]:
    lake = lake or open_lake()
    root = silver_root(lake)
    mirror = None if isinstance(lake, LocalLake) else root
    stats = [silver.build(lake, cfg, mirror=mirror) for cfg in configs(sites).values()]
    runs = [
        {
            "run_id": h["run_id"],
            "site": h["site"],
            "started_at": h["started_at"],
            "duration_s": float(h["duration_s"]),
            "pages": h["pages"],
            "items": h["items"],
            "requests": h["requests"],
            "retries": h["retries"],
            "severity": h["severity"],
            "issue_classes": sorted({i["class"] for i in h["issues"]}),
        }
        for cfg in configs(sites).values()
        for h in history(lake, cfg.name)
    ]
    put_parquet(LocalLake(root), "_ops/run_history.parquet", pa.Table.from_pylist(runs))
    return stats


def step_dbt(command: str = "build", lake=None, db_path: Path | None = None) -> dict:
    from dbt.cli.main import dbtRunner  # heavy import, only when needed

    lake = lake or open_lake()
    db = db_path or ROOT / "warehouse" / "gold.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    os.environ["SCRAPEWATCH_DUCKDB"] = str(db)
    args = [command, "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR), "--quiet",
            "--vars", json.dumps({"silver_root": str(silver_root(lake))})]  # fmt: skip
    res = dbtRunner().invoke(args)
    if not res.success:
        raise RuntimeError(f"dbt {command} failed: {res.exception}")
    results = getattr(res.result, "results", []) or []
    tests = [r for r in results if r.node.resource_type == "test"]
    return {
        "dbt": command,
        "nodes": len(results),
        "tests": len(tests),
        "tests_passed": sum(str(r.status) == "pass" for r in tests),
    }


class GateFailed(RuntimeError):
    pass


def step_gate(monitor_results: list[dict], silver_stats: list[dict], dbt_result: dict) -> dict:
    """Fail the pipeline (and so the Airflow run) on a critical scrape issue or a quarantine spike."""
    problems = [f"{m['site']}: {', '.join(m['issues'])}" for m in monitor_results if m["severity"] == "critical"]
    for s in silver_stats:
        rate = s["quarantined"] / s["bronze_records"] if s["bronze_records"] else 0
        if rate > MAX_QUARANTINE_RATE:
            problems.append(f"{s['site']}: quarantine rate {rate:.1%} > {MAX_QUARANTINE_RATE:.0%}")
        if s["silver_records"] == 0:
            problems.append(f"{s['site']}: silver is empty")
    if dbt_result.get("tests_passed") != dbt_result.get("tests"):
        problems.append(f"dbt: {dbt_result.get('tests_passed')}/{dbt_result.get('tests')} tests passed")
    if problems:
        raise GateFailed("; ".join(problems))
    return {"gate": "passed", "sites": len(silver_stats)}


def run(sites: list[str] | None = None, lake=None) -> dict:
    t0 = time.monotonic()
    lake = lake or open_lake()
    mon = []
    for name, cfg in configs(sites).items():
        mon.append(step_monitor(step_scrape(name, lake=lake, cfg=cfg), lake=lake, cfg=cfg))
        print(json.dumps(mon[-1]))
    stats = step_silver(sites, lake=lake)
    print(json.dumps(stats))
    dbt = step_dbt(lake=lake)
    print(json.dumps(dbt))
    gate = step_gate(mon, stats, dbt)
    return {"monitor": mon, "silver": stats, "dbt": dbt, "gate": gate, "wall_s": round(time.monotonic() - t0, 1)}
