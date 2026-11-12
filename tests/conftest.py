import pytest

from scrapewatch import pipeline
from scrapewatch.config import SiteConfig
from scrapewatch.fetch import Fetcher
from scrapewatch.lake import LocalLake
from scrapewatch.testsite import TestSite


@pytest.fixture
def site():
    with TestSite() as s:
        yield s


@pytest.fixture
def cfg(site):
    return SiteConfig.model_validate(site.config())


@pytest.fixture
def lake(tmp_path):
    return LocalLake(tmp_path / "lake")


def run_once(cfg, lake) -> dict:
    """scrape -> bronze -> monitor, with backoff sleeps skipped (attempt counts are unchanged)."""
    with Fetcher(cfg, sleep=lambda s: None) as f:
        metrics = pipeline.step_scrape(cfg.name, lake=lake, cfg=cfg, fetcher=f)
    return pipeline.step_monitor(metrics, lake=lake, cfg=cfg, reports_dir=None)
