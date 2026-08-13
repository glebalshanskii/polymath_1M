from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch


class PolymarketChainlinkError(RuntimeError):
    """Polymarket frontend Chainlink history is missing or violates contract."""


@dataclass(frozen=True)
class PolymarketChainlinkConfig:
    schema_version: int
    dataset_id: str
    provider: str
    endpoint: str
    symbol: str
    variant: str
    period_start: datetime
    period_end_exclusive: datetime
    request_span_minutes: int
    context_only: bool
    max_workers: int
    config_sha256: str


@dataclass(frozen=True)
class RequestSpec:
    index: int
    start_s: int
    end_s: int
    url: str

    @property
    def filename(self) -> str:
        stamp = datetime.fromtimestamp(self.start_s, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{self.index:04d}_{stamp}.json"


@dataclass(frozen=True)
class PolymarketChainlinkSeries:
    timestamp_s: torch.Tensor
    value: torch.Tensor

    def __len__(self) -> int:
        return self.timestamp_s.numel()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_utc(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise PolymarketChainlinkError(f"{name} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PolymarketChainlinkError(f"{name} must include timezone")
    return parsed.astimezone(UTC)


def load_polymarket_chainlink_config(
    path: str | Path,
) -> PolymarketChainlinkConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "dataset_id",
        "provider",
        "endpoint",
        "symbol",
        "variant",
        "period_start",
        "period_end_exclusive",
        "request_span_minutes",
        "context_only",
        "max_workers",
    }
    if payload.keys() != required:
        raise PolymarketChainlinkError(
            f"Chainlink config fields differ: missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    start = _parse_utc(payload["period_start"], "period_start")
    end = _parse_utc(payload["period_end_exclusive"], "period_end_exclusive")
    span = int(payload["request_span_minutes"])
    workers = int(payload["max_workers"])
    if (
        payload["schema_version"] != 1
        or payload["provider"] != "Polymarket frontend proxy"
        or payload["endpoint"] != "https://polymarket.com/api/crypto/price-history"
        or payload["symbol"] != "BTC"
        or payload["variant"] != "hourly"
        or payload["context_only"] is not True
        or span != 60
        or not 1 <= workers <= 16
        or start.second != 0
        or start.microsecond != 0
        or end.second != 0
        or end.microsecond != 0
        or (end - start).total_seconds() < 2 * 3_600
        or int((end - start).total_seconds()) % 3_600
    ):
        raise PolymarketChainlinkError(
            "Chainlink source must be the frozen hourly BTC development context"
        )
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return PolymarketChainlinkConfig(
        schema_version=1,
        dataset_id=str(payload["dataset_id"]),
        provider=str(payload["provider"]),
        endpoint=str(payload["endpoint"]),
        symbol=str(payload["symbol"]),
        variant=str(payload["variant"]),
        period_start=start,
        period_end_exclusive=end,
        request_span_minutes=span,
        context_only=True,
        max_workers=workers,
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _request_specs(config: PolymarketChainlinkConfig) -> tuple[RequestSpec, ...]:
    start_s = int(config.period_start.timestamp())
    end_s = int(config.period_end_exclusive.timestamp())
    span_s = config.request_span_minutes * 60
    # Aligned requests cover through the beginning of the final hour. The last
    # request is shifted by one minute, so its inclusive endpoint is 23:59 and
    # no raw response contains a point at the holdout boundary.
    starts = list(range(start_s, end_s - span_s, span_s))
    starts.append(end_s - span_s - 60)
    specs: list[RequestSpec] = []
    for index, request_start in enumerate(starts):
        request_end = request_start + span_s
        query = urllib.parse.urlencode(
            {
                "symbol": config.symbol,
                "eventStartTime": datetime.fromtimestamp(request_start, tz=UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "variant": config.variant,
                "endDate": datetime.fromtimestamp(request_end, tz=UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        specs.append(
            RequestSpec(
                index=index,
                start_s=request_start,
                end_s=request_end,
                url=f"{config.endpoint}?{query}",
            )
        )
    return tuple(specs)


def _parse_response(raw: bytes, spec: RequestSpec) -> tuple[list[int], list[float]]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolymarketChainlinkError(
            f"invalid JSON for request {spec.index}"
        ) from exc
    if not isinstance(payload, list) or len(payload) != 61:
        raise PolymarketChainlinkError(
            f"request {spec.index} returned {len(payload) if isinstance(payload, list) else 'non-list'} points"
        )
    timestamps: list[int] = []
    values: list[float] = []
    for point in payload:
        if not isinstance(point, dict) or point.keys() != {"timestamp", "value"}:
            raise PolymarketChainlinkError("Chainlink history point shape differs")
        timestamp_ms = int(point["timestamp"])
        value = float(point["value"])
        if timestamp_ms % 1_000 or not math.isfinite(value) or value <= 0:
            raise PolymarketChainlinkError("Chainlink history point is invalid")
        timestamps.append(timestamp_ms // 1_000)
        values.append(value)
    expected = list(range(spec.start_s, spec.end_s + 1, 60))
    if timestamps != expected:
        raise PolymarketChainlinkError(
            f"request {spec.index} is not a complete inclusive minute series"
        )
    return timestamps, values


def _fetch(spec: RequestSpec, destination: Path) -> dict[str, Any]:
    request = urllib.request.Request(
        spec.url,
        headers={"User-Agent": "polymath_1M/0.1 development-context-downloader"},
    )
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
            _parse_response(raw, spec)
            temporary = destination.with_suffix(".json.part")
            temporary.write_bytes(raw)
            os.replace(temporary, destination)
            return {
                "index": spec.index,
                "request_start_utc": datetime.fromtimestamp(
                    spec.start_s, tz=UTC
                ).isoformat(),
                "request_end_utc": datetime.fromtimestamp(
                    spec.end_s, tz=UTC
                ).isoformat(),
                "url": spec.url,
                "path": str(destination),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "points": 61,
            }
        except (OSError, urllib.error.URLError, PolymarketChainlinkError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.5 * 2**attempt)
    raise PolymarketChainlinkError(
        f"failed request {spec.index} after retries: {last_error}"
    )


def _existing_record(spec: RequestSpec, path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = path.read_bytes()
    try:
        _parse_response(raw, spec)
    except PolymarketChainlinkError:
        return None
    return {
        "index": spec.index,
        "request_start_utc": datetime.fromtimestamp(spec.start_s, tz=UTC).isoformat(),
        "request_end_utc": datetime.fromtimestamp(spec.end_s, tz=UTC).isoformat(),
        "url": spec.url,
        "path": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "points": 61,
    }


def download_polymarket_chainlink_context(
    config_path: str | Path,
    data_root: str | Path = "data/historical",
) -> Path:
    config = load_polymarket_chainlink_config(config_path)
    dataset_dir = Path(data_root) / config.dataset_id
    raw_dir = dataset_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    records: dict[int, dict[str, Any]] = {}
    pending: list[tuple[RequestSpec, Path]] = []
    for spec in _request_specs(config):
        path = raw_dir / spec.filename
        existing = _existing_record(spec, path)
        if existing is None:
            pending.append((spec, path))
        else:
            records[spec.index] = existing
    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        futures = {
            executor.submit(_fetch, spec, path): spec.index for spec, path in pending
        }
        for future in as_completed(futures):
            record = future.result()
            records[int(record["index"])] = record
    expected = _request_specs(config)
    if set(records) != {spec.index for spec in expected}:
        raise PolymarketChainlinkError("Chainlink download is incomplete")
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "config_path": str(config_path),
        "config_sha256": config.config_sha256,
        "provider": config.provider,
        "endpoint": config.endpoint,
        "symbol": config.symbol,
        "variant": config.variant,
        "context_only": True,
        "period_start": config.period_start.isoformat(),
        "period_end_exclusive": config.period_end_exclusive.isoformat(),
        "holdout_boundary_excluded_from_raw": True,
        "requests": [records[index] for index in sorted(records)],
    }
    manifest_path = dataset_dir / "manifest.json"
    temporary = manifest_path.with_suffix(".json.part")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)
    return manifest_path


def load_polymarket_chainlink_series(
    config_path: str | Path,
    data_root: str | Path,
) -> tuple[PolymarketChainlinkSeries, dict[str, Any]]:
    config = load_polymarket_chainlink_config(config_path)
    dataset_dir = Path(data_root) / config.dataset_id
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.is_file():
        raise PolymarketChainlinkError(
            "download Polymarket Chainlink development context first"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("config_sha256") != config.config_sha256
        or manifest.get("context_only") is not True
        or manifest.get("holdout_boundary_excluded_from_raw") is not True
    ):
        raise PolymarketChainlinkError("Chainlink manifest differs from config")
    points: dict[int, float] = {}
    file_records: list[dict[str, Any]] = []
    manifest_records = {
        int(record["index"]): record for record in manifest.get("requests", [])
    }
    for spec in _request_specs(config):
        record = manifest_records.get(spec.index)
        path = dataset_dir / "raw" / spec.filename
        if (
            record is None
            or not path.is_file()
            or _sha256(path) != record.get("sha256")
        ):
            raise PolymarketChainlinkError(
                f"Chainlink raw response changed: {spec.filename}"
            )
        timestamps, values = _parse_response(path.read_bytes(), spec)
        for timestamp, value in zip(timestamps, values, strict=True):
            existing = points.get(timestamp)
            if existing is not None and not math.isclose(
                existing, value, rel_tol=0, abs_tol=1e-9
            ):
                raise PolymarketChainlinkError(
                    f"overlapping requests disagree at {timestamp}"
                )
            points[timestamp] = value
        file_records.append(
            {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": record["sha256"],
            }
        )
    start_s = int(config.period_start.timestamp())
    end_s = int(config.period_end_exclusive.timestamp())
    selected = sorted(
        (timestamp, value)
        for timestamp, value in points.items()
        if start_s <= timestamp < end_s
    )
    timestamps = torch.tensor([item[0] for item in selected], dtype=torch.int64)
    values = torch.tensor([item[1] for item in selected], dtype=torch.float64)
    expected_rows = (end_s - start_s) // 60
    if (
        timestamps.numel() != expected_rows
        or bool((timestamps[1:] - timestamps[:-1] != 60).any().item())
        or bool((~torch.isfinite(values) | (values <= 0)).any().item())
        or int(timestamps[-1].item()) >= end_s
    ):
        raise PolymarketChainlinkError(
            "Chainlink development context is not minute-complete"
        )
    return (
        PolymarketChainlinkSeries(timestamp_s=timestamps, value=values),
        {
            "config_sha256": config.config_sha256,
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "provider": config.provider,
            "endpoint": config.endpoint,
            "symbol": "btc/usd",
            "variant": config.variant,
            "context_only": True,
            "rows": int(timestamps.numel()),
            "request_count": len(file_records),
            "files": file_records,
        },
    )
