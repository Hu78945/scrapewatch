import os
import socket

import pyarrow as pa
import pytest

from scrapewatch.lake import AzureLake, LocalLake, get_parquet, open_lake, put_parquet

AZURITE = "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"  # noqa: E501 - Azurite's public, well-known dev key


def _azurite_up() -> bool:
    try:
        socket.create_connection(("127.0.0.1", 10000), timeout=0.5).close()
        return True
    except OSError:
        return False


def _roundtrip(lake):
    t = pa.table({"a": [1, 2], "b": ["x", None]})
    put_parquet(lake, "bronze/records/site=s/dt=2026-01-01/run_id=r1.parquet", t)
    put_parquet(lake, "bronze/records/site=s/dt=2026-01-02/run_id=r2.parquet", t)
    lake.put("ops/runs/site=s/r1.json", b"{}")
    assert lake.list("bronze/records/site=s") == [
        "bronze/records/site=s/dt=2026-01-01/run_id=r1.parquet",
        "bronze/records/site=s/dt=2026-01-02/run_id=r2.parquet",
    ]
    assert get_parquet(lake, "bronze/records/site=s/dt=2026-01-02/run_id=r2.parquet").equals(t)
    assert lake.list("nothing/here") == []


def test_local_lake_roundtrip(tmp_path):
    _roundtrip(LocalLake(tmp_path))


def test_open_lake_selects_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPEWATCH_LAKE", str(tmp_path))
    assert isinstance(open_lake(), LocalLake)


@pytest.mark.skipif(not _azurite_up(), reason="Azurite not running on 127.0.0.1:10000")
def test_azure_lake_roundtrip_on_azurite(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", os.environ.get("AZURE_STORAGE_CONNECTION_STRING", AZURITE))
    lake = AzureLake(f"test-{os.getpid()}")
    try:
        _roundtrip(lake)
    finally:
        lake.client.delete_container()


@pytest.mark.skipif(not _azurite_up(), reason="Azurite not running on 127.0.0.1:10000")
def test_full_pipeline_against_azurite(site, monkeypatch, tmp_path):
    """scrape -> monitor -> silver with the lake on Azurite; dbt reads the mirrored working copy."""
    from conftest import run_once

    from scrapewatch import pipeline
    from scrapewatch.config import SiteConfig

    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", os.environ.get("AZURE_STORAGE_CONNECTION_STRING", AZURITE))
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    cfg = SiteConfig.model_validate(site.config())
    monkeypatch.setattr(pipeline, "configs", lambda sites=None: {cfg.name: cfg})
    lake = AzureLake(f"pipe-{os.getpid()}")
    try:
        assert run_once(cfg, lake)["severity"] == "ok"
        (stats,) = pipeline.step_silver(lake=lake)
        assert stats["silver_records"] == 100
        assert any(p.startswith("bronze/pages/site=testsite/") for p in lake.list("bronze"))
        assert (tmp_path / "warehouse/silver/testsite/testsite.parquet").exists()
    finally:
        lake.client.delete_container()
