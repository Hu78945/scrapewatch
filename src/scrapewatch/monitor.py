"""Per-run monitoring: measure a scrape, compare it with the site's recent healthy runs, classify what broke.

Failure classes (one root cause wins; symptoms it explains are suppressed):
  crash             the scraper raised (bad markup, bug)                                   critical
  blocked           403/429 after retries, challenge/CAPTCHA page, sudden empty body       critical
  fetch_error       5xx / network errors after retries                                     critical
  selector_breakage 2xx pages with no items, or a required field mostly null              critical
                    an optional field's null rate jumped vs baseline                       warning
  schema_drift      values present but no longer parse (format change)                     critical (required)
                    DOM fingerprint diverged from baseline while selectors still match     warning
  volume_anomaly    item count outside [low, high] x baseline median                       warning
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import uuid
from datetime import UTC, datetime

import httpx

from scrapewatch.config import SiteConfig
from scrapewatch.engine import ScrapeResult, coerce

log = logging.getLogger(__name__)
SEVERITY = {"ok": 0, "warning": 1, "critical": 2}
ROOT_CAUSES = {"crash", "blocked", "fetch_error"}


def measure(cfg: SiteConfig, result: ScrapeResult, run_id: str | None = None) -> dict:
    pages = result.pages
    ok_pages = [p for p in pages if p.status is not None and 200 <= p.status < 300 and not p.block_reason]
    blocked: dict[str, int] = {}
    for p in pages:
        if p.block_reason:
            blocked[p.block_reason] = blocked.get(p.block_reason, 0) + 1
    status_counts: dict[str, int] = {}
    for p in pages:
        status_counts[str(p.status)] = status_counts.get(str(p.status), 0) + 1
    recs = result.records
    null_rate, type_error_rate = {}, {}
    for name, spec in cfg.fields.items():
        raw = [r.get(name) for r in recs]
        present = [v for v in raw if v not in (None, [])]
        null_rate[name] = round(1 - len(present) / len(raw), 4) if raw else None
        bad = sum(not coerce(spec, v)[1] for v in present)
        type_error_rate[name] = round(bad / len(present), 4) if present else None
    return {
        "run_id": run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6],
        "site": cfg.name,
        "started_at": datetime.fromtimestamp(result.started_at, UTC).isoformat(),
        "duration_s": round(result.finished_at - result.started_at, 3),
        "pages": len(pages),
        "pages_ok": len(ok_pages),
        "items": len(recs),
        "requests": sum(p.attempts for p in pages),
        "retries": sum(max(p.attempts - 1, 0) for p in pages),
        "status_counts": status_counts,
        "blocked_pages": blocked,
        "fetch_error_pages": sum(
            1 for p in pages if p.error != "robots_disallowed" and (p.status is None or p.status >= 500)
        ),
        "robots_disallowed": sum(1 for p in pages if p.error == "robots_disallowed"),
        "zero_item_pages": sum(1 for p in ok_pages if p.n_items < cfg.monitor.min_items_per_page),
        "null_rate": null_rate,
        "type_error_rate": type_error_rate,
        "fingerprint": sorted(result.fingerprint),
        "crash": result.crash,
    }


def baseline(history: list[dict], n: int) -> dict | None:
    """The last n runs with no issue at all. A run that raised anything (even a warning) never becomes the baseline,
    so a persisting regression keeps alarming instead of quietly becoming the new normal."""
    good = [h for h in sorted(history, key=lambda h: h["started_at"]) if h.get("severity") == "ok"][-n:]
    if not good:
        return None
    fields = good[-1]["null_rate"].keys()
    return {
        "runs": len(good),
        "items": statistics.median(h["items"] for h in good),
        "null_rate": {
            f: statistics.median(h["null_rate"][f] for h in good if h["null_rate"].get(f) is not None)
            for f in fields
            if any(h["null_rate"].get(f) is not None for h in good)
        },
        "fingerprint": set(good[-1]["fingerprint"]),
    }


def evaluate(cfg: SiteConfig, m: dict, base: dict | None) -> list[dict]:
    t = cfg.monitor
    issues: list[dict] = []

    def add(cls, severity, message, **evidence):
        issues.append({"class": cls, "severity": severity, "message": message, "evidence": evidence})

    if m["crash"]:
        add("crash", "critical", "scraper raised: " + m["crash"].strip().splitlines()[-1], traceback=m["crash"])
    if m["blocked_pages"]:
        add("blocked", "critical", f"blocked on {sum(m['blocked_pages'].values())} page(s)", reasons=m["blocked_pages"])
    if m["fetch_error_pages"]:
        add(
            "fetch_error",
            "critical",
            f"{m['fetch_error_pages']} page(s) failed after retries",
            status=m["status_counts"],
        )
    if m["zero_item_pages"]:
        add(
            "selector_breakage",
            "critical",
            f"{m['zero_item_pages']} page(s) returned 2xx with no items: item selector no longer matches",
            item_selector=cfg.item_css or cfg.item_xpath,
        )
    for name, spec in cfg.fields.items():
        nr, ter = m["null_rate"].get(name), m["type_error_rate"].get(name)
        if nr is not None:
            if spec.required and nr > t.max_required_null_rate:
                add("selector_breakage", "critical", f"required field {name!r} null in {nr:.0%} of items", field=name)
            elif not spec.required and base and name in base["null_rate"]:
                if nr > base["null_rate"][name] + t.null_rate_jump:
                    add(
                        "selector_breakage",
                        "warning",
                        f"field {name!r} null rate {nr:.0%} vs baseline {base['null_rate'][name]:.0%}",
                        field=name,
                    )
        if ter is not None and ter > t.max_type_error_rate:
            add(
                "schema_drift",
                "critical" if spec.required else "warning",
                f"field {name!r}: {ter:.0%} of values no longer parse as {spec.type}"
                + (f" with /{spec.regex}/" if spec.regex else ""),
                field=name,
            )
    if base and m["fingerprint"]:
        fp = set(m["fingerprint"])
        sim = len(fp & base["fingerprint"]) / len(fp | base["fingerprint"])
        if sim < t.min_layout_similarity:
            add(
                "schema_drift",
                "warning",
                f"page layout changed: DOM similarity {sim:.2f} vs baseline (< {t.min_layout_similarity})",
                added=sorted(fp - base["fingerprint"])[:15],
                removed=sorted(base["fingerprint"] - fp)[:15],
            )
    explained = any(i["class"] in ROOT_CAUSES for i in issues) or m["zero_item_pages"]
    if base and not explained and base["items"]:
        ratio = m["items"] / base["items"]
        if not t.volume_low <= ratio <= t.volume_high:
            add(
                "volume_anomaly",
                "warning",
                f"{m['items']} items vs baseline median {base['items']:g} ({ratio:.0%})",
                pages=m["pages"],
            )
    return issues


def severity(issues: list[dict]) -> str:
    return max((i["severity"] for i in issues), key=SEVERITY.get, default="ok")


def assess(cfg: SiteConfig, m: dict, history: list[dict]) -> dict:
    base = baseline(history, cfg.monitor.baseline_runs)
    issues = evaluate(cfg, m, base)
    return {
        **m,
        "baseline_runs": base["runs"] if base else 0,
        "baseline_items": base["items"] if base else None,
        "issues": issues,
        "severity": severity(issues),
    }


def markdown(report: dict) -> str:
    icon = {"ok": "OK", "warning": "WARNING", "critical": "CRITICAL"}[report["severity"]]
    lines = [
        f"## {report['site']} - {icon}",
        "",
        f"run `{report['run_id']}` · {report['pages']} pages ({report['pages_ok']} ok) · {report['items']} items · "
        f"{report['requests']} requests ({report['retries']} retries) · {report['duration_s']}s · "
        + (
            f"baseline: {report['baseline_runs']} healthy run(s), median {report['baseline_items']:g} items"
            if report["baseline_runs"]
            else "no baseline yet (first healthy run): relative checks skipped"
        ),
        "",
    ]
    lines += [f"- **{i['severity']}** `{i['class']}`: {i['message']}" for i in report["issues"]] or ["- no issues"]
    return "\n".join(lines) + "\n"


def alert(report: dict) -> bool:
    """POST the report (minus raw tracebacks/fingerprints) to SCRAPEWATCH_ALERT_WEBHOOK. Off unless set."""
    url = os.environ.get("SCRAPEWATCH_ALERT_WEBHOOK")
    floor = os.environ.get("SCRAPEWATCH_ALERT_MIN_SEVERITY", "warning")
    if not url or SEVERITY[report["severity"]] < SEVERITY.get(floor, 1):
        return False
    payload = {k: report[k] for k in ("site", "run_id", "severity", "items", "pages", "started_at")}
    payload["issues"] = [{k: i[k] for k in ("class", "severity", "message")} for i in report["issues"]]
    payload["text"] = markdown(report)
    try:
        httpx.post(url, json=payload, timeout=5).raise_for_status()
        return True
    except httpx.HTTPError as e:  # an alerting outage must not fail the pipeline; the report is still stored
        log.warning("alert webhook failed: %s", type(e).__name__)
        return False


def to_json(report: dict) -> bytes:
    return json.dumps(report, indent=2, default=str).encode()
