"""Data lake I/O: the local filesystem, or Azure Data Lake Storage Gen2 (also Azurite) through the Blob API.

ADLS Gen2 accounts serve the Blob API on the same data (multi-protocol access), and Azurite only implements
Blob, so one client covers both. Paths are hive-style: bronze/pages/site=x/dt=YYYY-MM-DD/run_id=....parquet
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from scrapewatch.config import ROOT


class LocalLake:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.uri = str(self.root)

    def put(self, path: str, data: bytes) -> None:
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)  # atomic: readers never see half a file

    def get(self, path: str) -> bytes:
        return (self.root / path).read_bytes()

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file() and p.suffix != ".tmp")


class AzureLake:
    def __init__(self, container: str):
        from azure.storage.blob import BlobServiceClient

        conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn:  # Azurite / local dev only; production uses Azure AD (no shared keys, see terraform/)
            svc = BlobServiceClient.from_connection_string(conn)
        else:
            from azure.identity import DefaultAzureCredential  # `azure-ad` extra

            svc = BlobServiceClient(os.environ["AZURE_STORAGE_ACCOUNT_URL"], credential=DefaultAzureCredential())
        self.client = svc.get_container_client(container)
        self.uri = f"az://{container}"
        if conn and not self.client.exists():
            self.client.create_container()  # Terraform owns the container in Azure; this is for Azurite

    def put(self, path: str, data: bytes) -> None:
        self.client.upload_blob(path, data, overwrite=True)

    def get(self, path: str) -> bytes:
        return self.client.download_blob(path).readall()

    def list(self, prefix: str) -> list[str]:
        return sorted(b.name for b in self.client.list_blobs(name_starts_with=prefix.rstrip("/") + "/"))


def open_lake(uri: str | None = None) -> LocalLake | AzureLake:
    uri = uri or os.environ.get("SCRAPEWATCH_LAKE") or str(ROOT / "lake")
    if uri.startswith("az://"):
        return AzureLake(uri.removeprefix("az://").strip("/"))
    return LocalLake(uri)


def put_parquet(lake, path: str, table: pa.Table) -> None:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    lake.put(path, buf.getvalue())


def get_parquet(lake, path: str) -> pa.Table:
    return pq.read_table(io.BytesIO(lake.get(path)))
