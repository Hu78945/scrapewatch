"""Bronze -> silver: type every record against its config, quarantine what fails, dedup by key + content hash.

Silver is rebuilt from all bronze partitions for the site on every run (idempotent; fine at this scale).
"""

from __future__ import annotations

import hashlib
import json

import pyarrow as pa

from scrapewatch.config import FieldSpec, SiteConfig
from scrapewatch.engine import coerce
from scrapewatch.lake import LocalLake, get_parquet, put_parquet

_PA = {"str": pa.string(), "int": pa.int64(), "float": pa.float64()}
QUARANTINE_SCHEMA = pa.schema(
    [("run_id", pa.string()), ("scraped_at", pa.string()), ("page_url", pa.string()),
     ("record_json", pa.string()), ("reasons", pa.list_(pa.string()))]
)  # fmt: skip


def _pa_type(spec: FieldSpec) -> pa.DataType:
    return pa.list_(_PA[spec.type]) if spec.many else _PA[spec.type]


def silver_schema(cfg: SiteConfig) -> pa.Schema:
    return pa.schema(
        [(n, _pa_type(s)) for n, s in cfg.fields.items()]
        + [
            ("record_key", pa.string()),
            ("content_hash", pa.string()),
            ("first_seen_at", pa.string()),
            ("last_seen_at", pa.string()),
            ("times_seen", pa.int64()),
            ("content_versions", pa.int64()),
            ("last_run_id", pa.string()),
        ]
    )


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate(cfg: SiteConfig, raw: dict) -> tuple[dict | None, list[str]]:
    typed, reasons = {}, []
    for name, spec in cfg.fields.items():
        value, ok = coerce(spec, raw.get(name))
        if not ok:
            reasons.append(f"{name}: unparseable {raw.get(name)!r}")
        elif spec.required and value in (None, []):
            reasons.append(f"{name}: missing")
        typed[name] = value
    return (None, reasons) if reasons else (typed, [])


def transform(cfg: SiteConfig, bronze_rows: list[dict]) -> tuple[pa.Table, pa.Table, dict]:
    """bronze record rows (run_id, scraped_at, page_url, record_json) -> (silver, quarantine, stats)."""
    latest: dict[str, dict] = {}
    quarantine = []
    valid = 0
    for row in sorted(bronze_rows, key=lambda r: (r["scraped_at"], r["run_id"])):
        typed, reasons = validate(cfg, json.loads(row["record_json"]))
        if typed is None:
            quarantine.append({**row, "reasons": reasons})
            continue
        valid += 1
        key = _sha([typed[k] for k in cfg.key_fields])
        content = _sha(typed)
        prev = latest.get(key)
        latest[key] = {
            **typed,
            "record_key": key,
            "content_hash": content,
            "first_seen_at": prev["first_seen_at"] if prev else row["scraped_at"],
            "last_seen_at": row["scraped_at"],
            "times_seen": (prev["times_seen"] if prev else 0) + 1,
            "content_versions": (prev["content_versions"] + (prev["content_hash"] != content)) if prev else 1,
            "last_run_id": row["run_id"],
        }
    silver = pa.Table.from_pylist(list(latest.values()), schema=silver_schema(cfg))
    q = pa.Table.from_pylist(quarantine, schema=QUARANTINE_SCHEMA)
    stats = {
        "site": cfg.name,
        "bronze_records": len(bronze_rows),
        "quarantined": len(quarantine),
        "valid": valid,
        "silver_records": silver.num_rows,
        "duplicates_removed": valid - silver.num_rows,
        "changed_records": sum(r["content_versions"] > 1 for r in latest.values()),
    }
    return silver, q, stats


def build(lake, cfg: SiteConfig, mirror=None) -> dict:
    rows: list[dict] = []
    for path in lake.list(f"bronze/records/site={cfg.name}"):
        rows += get_parquet(lake, path).select(["run_id", "scraped_at", "page_url", "record_json"]).to_pylist()
    silver, quarantine, stats = transform(cfg, rows)
    put_parquet(lake, f"silver/{cfg.name}/{cfg.name}.parquet", silver)
    put_parquet(lake, f"silver/_quarantine/{cfg.name}.parquet", quarantine)
    if mirror is not None:  # local working copy for dbt-duckdb when the lake is remote
        LocalLake(mirror).put(f"{cfg.name}/{cfg.name}.parquet", lake.get(f"silver/{cfg.name}/{cfg.name}.parquet"))
    return stats
