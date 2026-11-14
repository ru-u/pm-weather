"""Storage backend: a local directory or an S3-compatible bucket, same interface.

Uses pyarrow's built-in filesystems so the logger needs no extra S3 dependency.

Environment (all optional; with none set, storage is the local `data/` folder):
    PMW_DATA_DIR            local root (default: ./data)
    PMW_S3_BUCKET           bucket name; setting this switches to bucket storage
    PMW_S3_ENDPOINT         endpoint URL of the S3-compatible service
    PMW_S3_ACCESS_KEY_ID    bucket credentials (NOT trading credentials)
    PMW_S3_SECRET_ACCESS_KEY
    PMW_S3_REGION           default "auto"
    PMW_S3_PREFIX           optional key prefix inside the bucket
    PMW_S3_PATH_STYLE       "1" to use path-style URLs (default: virtual-hosted)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq


@dataclass(frozen=True)
class FileInfo:
    path: str  # relative to the storage root
    size: int


class Storage:
    def __init__(self, fs: pafs.FileSystem, root: str, kind: str) -> None:
        self.fs = fs
        self.root = root.rstrip("/")
        self.kind = kind

    @classmethod
    def local(cls, root: str | Path) -> Storage:
        path = Path(root).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return cls(pafs.LocalFileSystem(), str(path), "local")

    @classmethod
    def from_env(cls) -> Storage:
        bucket = os.environ.get("PMW_S3_BUCKET")
        if not bucket:
            return cls.local(os.environ.get("PMW_DATA_DIR", "data"))
        endpoint = os.environ.get("PMW_S3_ENDPOINT", "")
        scheme = "https"
        if "://" in endpoint:
            scheme, endpoint = endpoint.split("://", 1)
        fs = pafs.S3FileSystem(
            access_key=os.environ["PMW_S3_ACCESS_KEY_ID"],
            secret_key=os.environ["PMW_S3_SECRET_ACCESS_KEY"],
            endpoint_override=endpoint.rstrip("/") or None,
            scheme=scheme,
            region=os.environ.get("PMW_S3_REGION", "auto"),
            # Railway buckets use virtual-hosted URLs (bucket name as subdomain).
            # Set PMW_S3_PATH_STYLE=1 for an older bucket that needs path style.
            force_virtual_addressing=os.environ.get("PMW_S3_PATH_STYLE", "") not in ("1", "true"),
        )
        prefix = os.environ.get("PMW_S3_PREFIX", "").strip("/")
        root = f"{bucket}/{prefix}" if prefix else bucket
        return cls(fs, root, "s3")

    def _full(self, rel: str) -> str:
        return f"{self.root}/{rel.lstrip('/')}"

    def _ensure_parent(self, full: str) -> None:
        if self.kind == "local":
            self.fs.create_dir(full.rsplit("/", 1)[0], recursive=True)

    def write_parquet(self, rel: str, table: pa.Table) -> int:
        """Write one Parquet file. Returns bytes written."""
        full = self._full(rel)
        self._ensure_parent(full)
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink, compression="zstd", compression_level=9)
        buf = sink.getvalue()
        with self.fs.open_output_stream(full) as out:
            out.write(buf)
        return int(buf.size)

    def write_bytes(self, rel: str, data: bytes) -> int:
        full = self._full(rel)
        self._ensure_parent(full)
        with self.fs.open_output_stream(full) as out:
            out.write(data)
        return len(data)

    def read_bytes(self, rel: str) -> bytes:
        with self.fs.open_input_stream(self._full(rel)) as src:
            return bytes(src.read())

    def read_parquet(self, rel: str) -> pa.Table:
        return pq.read_table(self._full(rel), filesystem=self.fs)

    def exists(self, rel: str) -> bool:
        return bool(self.fs.get_file_info(self._full(rel)).type != pafs.FileType.NotFound)

    def list(self, rel_dir: str = "") -> list[FileInfo]:
        """All files under a directory, recursively. Missing directory gives []."""
        base = self._full(rel_dir) if rel_dir else self.root
        selector = pafs.FileSelector(base, recursive=True, allow_not_found=True)
        out = []
        for info in self.fs.get_file_info(selector):
            if info.type == pafs.FileType.File:
                out.append(FileInfo(info.path[len(self.root) + 1 :], int(info.size or 0)))
        return sorted(out, key=lambda f: f.path)

    def delete(self, rel: str) -> None:
        self.fs.delete_file(self._full(rel))

    def describe(self) -> str:
        return f"{self.kind}:{self.root}"
