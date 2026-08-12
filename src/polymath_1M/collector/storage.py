from __future__ import annotations

import gzip
import hashlib
import json
import platform
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

import torch

from polymath_1M.audit.http import JsonResponse

from .config import CollectorConfig


def _json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if indent is None else None,
        indent=indent,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip()


def _git_dirty() -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return bool(result.stdout.strip())


@dataclass(frozen=True)
class HttpManifestEntry:
    source: str
    url: str
    retrieved_at: str
    status: int
    sha256: str
    bytes: int
    relative_path: str


class RawStreamWriter:
    _MAGIC = b"POLYMATH-FRAMES-v1\n"
    _HEADER = struct.Struct(">QQQBBHI")

    def __init__(
        self,
        run_dir: Path,
        stream_name: str,
        flush_every_records: int,
    ) -> None:
        self.run_dir = run_dir
        self.stream_name = stream_name
        self.flush_every_records = flush_every_records
        self.stream_dir = run_dir / "raw" / "websocket" / stream_name
        self.stream_dir.mkdir(parents=True, exist_ok=True)
        self._bucket: str | None = None
        self._stream: Any = None
        self._records_since_flush = 0
        self.records = 0
        self.bytes = 0

    def write(
        self,
        raw: str,
        *,
        receive_timestamp_ns: int,
        receive_monotonic_ns: int,
        connection_id: str,
        direction: str = "inbound",
    ) -> None:
        bucket = datetime.fromtimestamp(
            receive_timestamp_ns / 1_000_000_000, tz=UTC
        ).strftime("%Y%m%dT%H")
        if bucket != self._bucket:
            self._rotate(bucket)
        raw_bytes = raw.encode("utf-8")
        connection_bytes = connection_id.encode("ascii")
        if len(connection_bytes) > 255:
            raise ValueError("connection_id exceeds 255 bytes")
        direction_code = {"inbound": 0, "outbound": 1}.get(direction)
        if direction_code is None:
            raise ValueError(f"unsupported frame direction: {direction}")
        header = self._HEADER.pack(
            self.records,
            receive_timestamp_ns,
            receive_monotonic_ns,
            direction_code,
            len(connection_bytes),
            0,
            len(raw_bytes),
        )
        self._stream.write(header + connection_bytes + raw_bytes)
        self.records += 1
        self.bytes += len(raw_bytes)
        self._records_since_flush += 1
        if self._records_since_flush >= self.flush_every_records:
            self._stream.flush()
            self._records_since_flush = 0

    def close(self) -> None:
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()
            self._stream = None

    def _rotate(self, bucket: str) -> None:
        self.close()
        path = self.stream_dir / f"{bucket}.frames.gz"
        self._stream = gzip.open(path, "xb", compresslevel=1)
        self._stream.write(self._MAGIC)
        self._bucket = bucket


@dataclass(frozen=True)
class RawFrameRecord:
    sequence: int
    receive_timestamp_ns: int
    receive_monotonic_ns: int
    direction: str
    connection_id: str
    raw: str


def iter_raw_frames(path: Path):
    with gzip.open(path, "rb") as stream:
        magic = stream.read(len(RawStreamWriter._MAGIC))
        if magic != RawStreamWriter._MAGIC:
            raise ValueError(f"invalid raw frame magic in {path}")
        while header := stream.read(RawStreamWriter._HEADER.size):
            if len(header) != RawStreamWriter._HEADER.size:
                raise ValueError(f"truncated raw frame header in {path}")
            (
                sequence,
                wall_ns,
                monotonic_ns,
                direction_code,
                connection_length,
                reserved,
                raw_length,
            ) = RawStreamWriter._HEADER.unpack(header)
            if reserved != 0 or direction_code not in (0, 1):
                raise ValueError(f"invalid raw frame header values in {path}")
            connection = stream.read(connection_length)
            raw = stream.read(raw_length)
            if len(connection) != connection_length or len(raw) != raw_length:
                raise ValueError(f"truncated raw frame payload in {path}")
            yield RawFrameRecord(
                sequence=sequence,
                receive_timestamp_ns=wall_ns,
                receive_monotonic_ns=monotonic_ns,
                direction="inbound" if direction_code == 0 else "outbound",
                connection_id=connection.decode("ascii"),
                raw=raw.decode("utf-8"),
            )


class CollectorStorage:
    def __init__(
        self,
        output_root: str | Path,
        config: CollectorConfig,
        *,
        started_at: datetime | None = None,
    ) -> None:
        self.config = config
        self.started_at = (started_at or datetime.now(UTC)).astimezone(UTC)
        timestamp = self.started_at.strftime("%Y%m%dT%H%M%SZ")
        self.run_dir = Path(output_root) / f"{timestamp}_{config.collector_id}"
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.raw_dir = self.run_dir / "raw"
        self.raw_dir.mkdir()
        self._http_lock = Lock()
        self._http_manifest_path = self.run_dir / "http_manifest.jsonl"
        self._http_manifest = self._http_manifest_path.open("a", encoding="utf-8")
        self._http_entries: list[HttpManifestEntry] = []
        self.clob_writer = RawStreamWriter(
            self.run_dir, "clob_market", config.flush_every_records
        )
        self.rtds_writer = RawStreamWriter(
            self.run_dir, "rtds_reference", config.flush_every_records
        )
        self.write_json("effective_config.json", asdict(config))

    def archive_http(self, source: str, response: JsonResponse) -> None:
        body_hash = hashlib.sha256(response.body).hexdigest()
        url_hash = hashlib.sha256(response.url.encode()).hexdigest()[:16]
        directory = self.raw_dir / "http" / source
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{url_hash}_{body_hash[:16]}.json"
        entry = HttpManifestEntry(
            source=source,
            url=response.url,
            retrieved_at=response.retrieved_at,
            status=response.status,
            sha256=body_hash,
            bytes=len(response.body),
            relative_path=str(path.relative_to(self.run_dir)),
        )
        with self._http_lock:
            if not path.exists():
                path.write_bytes(response.body)
            self._http_entries.append(entry)
            self._http_manifest.write(_json(asdict(entry)) + "\n")
            self._http_manifest.flush()

    def write_json(self, name: str, value: Any) -> Path:
        path = self.run_dir / name
        path.write_text(_json(value, indent=2) + "\n", encoding="utf-8")
        return path

    def finalize(
        self,
        *,
        status: str,
        health: dict[str, Any],
        book_summary: dict[str, Any],
        markets: list[dict[str, Any]],
        reference_boundaries: list[dict[str, Any]],
        resolutions: dict[str, Any],
        errors: list[str],
    ) -> Path:
        self.clob_writer.close()
        self.rtds_writer.close()
        self._http_manifest.flush()
        self._http_manifest.close()
        market_path = self.write_json("markets.json", {"markets": markets})
        boundary_path = self.write_json(
            "reference_boundaries.json", {"boundaries": reference_boundaries}
        )
        resolution_path = self.write_json(
            "market_resolutions.json", {"resolutions": resolutions}
        )
        health_path = self.write_json("feed_health.json", health)
        book_path = self.write_json("live_book_summary.json", book_summary)
        ended_at = datetime.now(UTC)
        inventory = []
        for path in sorted(self.raw_dir.rglob("*")):
            if path.is_file():
                inventory.append(
                    {
                        "relative_path": str(path.relative_to(self.run_dir)),
                        "bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                )
        artifacts = []
        for path in (
            market_path,
            boundary_path,
            resolution_path,
            health_path,
            book_path,
            self.run_dir / "effective_config.json",
            self._http_manifest_path,
        ):
            artifacts.append(
                {
                    "relative_path": str(path.relative_to(self.run_dir)),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        manifest = {
            "schema_version": 1,
            "collector_id": self.config.collector_id,
            "status": status,
            "run_kind": (
                "production_24h"
                if self.config.duration_seconds >= 86_400
                else "development_smoke"
            ),
            "started_at": self.started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "runtime_seconds": (ended_at - self.started_at).total_seconds(),
            "source_commit": _git_commit(),
            "source_worktree_dirty": _git_dirty(),
            "runtime": {
                "python": sys.version,
                "platform": platform.platform(),
                "torch": torch.__version__,
                "torch_device": "cpu",
                "torch_dtype": "float64",
            },
            "raw_inventory": inventory,
            "artifacts": artifacts,
            "http_requests": [asdict(entry) for entry in self._http_entries],
            "errors": errors,
        }
        return self.write_json("manifest.json", manifest)


def wall_and_monotonic_ns() -> tuple[int, int]:
    return time.time_ns(), time.monotonic_ns()
