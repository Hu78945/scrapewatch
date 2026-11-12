"""scrapewatch CLI: the same steps the Airflow DAG runs, without Airflow."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from scrapewatch import pipeline


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="scrapewatch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="validate every configs/sites/*.yml")
    for name, help_ in [
        ("scrape", "scrape + monitor (bronze, run report)"),
        ("run", "scrape, monitor, silver, dbt, gate"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("sites", nargs="*", help="site names (default: all configs)")
    p = sub.add_parser("silver", help="rebuild silver + quarantine from bronze")
    p.add_argument("sites", nargs="*")
    sub.add_parser("dbt", help="dbt build (gold models + tests)")
    p = sub.add_parser("faultbench", help="inject every fault into the local test site, score the monitor")
    p.add_argument("--clean-runs", type=int, default=5)
    p = sub.add_parser("bench", help="real scrape of the toscrape.com sites, twice -> reports/bench.json")
    p.add_argument("sites", nargs="*")
    args = ap.parse_args(argv)

    if args.cmd == "validate":
        cfgs = pipeline.configs()
        print(json.dumps({n: {"fetcher": c.fetcher, "fields": list(c.fields)} for n, c in cfgs.items()}, indent=2))
        return 0
    if args.cmd == "scrape":
        worst = "ok"
        for name in pipeline.configs(args.sites):
            res = pipeline.step_monitor(pipeline.step_scrape(name))
            print(json.dumps(res))
            worst = max(worst, res["severity"], key=pipeline.monitor.SEVERITY.get)
        return 2 if worst == "critical" else 0
    if args.cmd == "silver":
        print(json.dumps(pipeline.step_silver(args.sites or None), indent=2))
        return 0
    if args.cmd == "dbt":
        print(json.dumps(pipeline.step_dbt()))
        return 0
    if args.cmd == "run":
        try:
            print(json.dumps(pipeline.run(args.sites or None)["gate"]))
        except pipeline.GateFailed as e:
            print(f"DQ gate failed: {e}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "faultbench":
        from scrapewatch.bench import faultbench

        print(json.dumps(faultbench(args.clean_runs)["summary"], indent=2))
        return 0
    if args.cmd == "bench":
        from scrapewatch.bench import realbench

        print(json.dumps(realbench(args.sites or None)["summary"], indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
