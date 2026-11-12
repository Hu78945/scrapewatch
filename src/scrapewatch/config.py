"""Site config schema. One YAML file per site under configs/sites/; adding a site = adding a file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[2]
SITES_DIR = ROOT / "configs" / "sites"
USER_AGENT = "scrapewatch/0.1 (+https://github.com/Hu78945/scrapewatch; portfolio project, polite crawler)"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldSpec(_Strict):
    css: str | None = None
    xpath: str | None = None
    attr: str | None = None  # None -> element text
    regex: str | None = None  # first capture group (or whole match) of the extracted string
    type: Literal["str", "int", "float"] = "str"
    many: bool = False  # list of values (e.g. tags) instead of the first match
    required: bool = False
    absolute_url: bool = False  # resolve a relative href against the page URL

    @model_validator(mode="after")
    def _one_selector(self):
        if (self.css is None) == (self.xpath is None):
            raise ValueError("exactly one of css / xpath is required")
        return self


class Pagination(_Strict):
    next_css: str | None = None
    next_xpath: str | None = None
    max_pages: int = Field(50, ge=1, le=1000)


class RateLimit(_Strict):
    requests_per_second: float = Field(1.0, gt=0, le=10)  # per host; hard cap of 10 rps


class Retry(_Strict):
    max_attempts: int = Field(4, ge=1, le=10)
    backoff_base_s: float = Field(0.5, ge=0)
    backoff_max_s: float = Field(30.0, ge=0)
    timeout_s: float = Field(20.0, gt=0)


class MonitorSpec(_Strict):
    min_items_per_page: int = Field(1, ge=0)  # a 200 page with fewer items than this is suspicious
    max_required_null_rate: float = Field(0.02, ge=0, le=1)  # absolute ceiling, used with or without a baseline
    null_rate_jump: float = Field(0.10, ge=0, le=1)  # optional-field null rate above baseline + this -> warning
    max_type_error_rate: float = Field(0.02, ge=0, le=1)  # values present but no longer parse as their type
    min_layout_similarity: float = Field(0.80, ge=0, le=1)  # Jaccard of DOM fingerprint vs baseline
    volume_low: float = Field(0.5, gt=0, le=1)  # items < 50% of baseline median -> anomaly
    volume_high: float = Field(2.0, ge=1)
    baseline_runs: int = Field(5, ge=1, le=50)


class SiteConfig(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    description: str = ""
    start_urls: list[HttpUrl] = Field(min_length=1)
    fetcher: Literal["http", "browser"] = "http"
    wait_for_css: str | None = None  # browser only: wait until this selector renders
    item_css: str | None = None
    item_xpath: str | None = None
    fields: dict[str, FieldSpec] = Field(min_length=1)
    key_fields: list[str] = Field(min_length=1)  # identity of a record for dedup / upserts
    pagination: Pagination = Pagination()
    rate_limit: RateLimit = RateLimit()
    retry: Retry = Retry()
    monitor: MonitorSpec = MonitorSpec()
    respect_robots: bool = True

    @model_validator(mode="after")
    def _check(self):
        if (self.item_css is None) == (self.item_xpath is None):
            raise ValueError("exactly one of item_css / item_xpath is required")
        unknown = set(self.key_fields) - set(self.fields)
        if unknown:
            raise ValueError(f"key_fields not defined in fields: {sorted(unknown)}")
        if not any(f.required for f in self.fields.values()):
            raise ValueError("at least one field must be required (selector-breakage detection depends on it)")
        if self.fetcher == "http" and self.wait_for_css:
            raise ValueError("wait_for_css only applies to fetcher: browser")
        return self

    @field_validator("start_urls")
    @classmethod
    def _http_only(cls, urls):
        for u in urls:
            if urlparse(str(u)).scheme not in {"http", "https"}:
                raise ValueError(f"unsupported scheme: {u}")
        return urls


def load(path: str | Path) -> SiteConfig:
    return SiteConfig.model_validate(yaml.safe_load(Path(path).read_text()))


def load_all(directory: Path = SITES_DIR) -> dict[str, SiteConfig]:
    configs = {}
    for p in sorted(directory.glob("*.yml")):
        cfg = load(p)
        if cfg.name != p.stem:
            raise ValueError(f"{p.name}: name {cfg.name!r} must match the file name")
        configs[cfg.name] = cfg
    return configs
