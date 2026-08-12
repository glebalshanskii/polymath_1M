from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

_SAFE_ID = re.compile(r"^[a-zA-Z0-9_.-]+$")
_ASSETS = {"BTC", "ETH", "SOL", "XRP"}
_DURATIONS = {"5m": 300, "15m": 900, "1h": 3_600}


@dataclass(frozen=True)
class SeriesSpec:
    id: str
    slug: str
    asset: str
    duration: str

    @property
    def duration_seconds(self) -> int:
        return _DURATIONS[self.duration]


@dataclass(frozen=True)
class CollectorConfig:
    collector_id: str
    duration_seconds: int
    discovery_interval_seconds: float
    market_lookback_seconds: int
    market_lookahead_seconds: int
    http_timeout_seconds: float
    http_retries: int
    clob_heartbeat_seconds: float
    rtds_heartbeat_seconds: float
    reconnect_max_seconds: float
    stale_after_seconds: float
    resolution_delay_seconds: int
    flush_every_records: int
    gamma_url: str
    clob_url: str
    clob_ws_url: str
    rtds_ws_url: str
    series: tuple[SeriesSpec, ...]

    def with_duration(self, duration_seconds: int | None) -> CollectorConfig:
        if duration_seconds is None:
            return self
        if duration_seconds <= 0:
            raise ValueError("duration override must be positive")
        return replace(self, duration_seconds=duration_seconds)


def _positive_number(raw: dict[str, Any], name: str) -> float:
    value = float(raw[name])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def load_collector_config(path: str | Path) -> CollectorConfig:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    collector_id = str(raw["collector_id"])
    if not _SAFE_ID.fullmatch(collector_id):
        raise ValueError("collector_id must be filesystem-safe")

    series: list[SeriesSpec] = []
    seen_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for item in raw.get("series", []):
        spec = SeriesSpec(
            id=str(item["id"]),
            slug=str(item["slug"]),
            asset=str(item["asset"]).upper(),
            duration=str(item["duration"]),
        )
        if spec.asset not in _ASSETS:
            raise ValueError(f"unsupported asset: {spec.asset}")
        if spec.duration not in _DURATIONS:
            raise ValueError(f"unsupported duration: {spec.duration}")
        if spec.id in seen_ids:
            raise ValueError(f"duplicate series id: {spec.id}")
        pair = (spec.asset, spec.duration)
        if pair in seen_pairs:
            raise ValueError(f"duplicate asset/duration series: {pair}")
        seen_ids.add(spec.id)
        seen_pairs.add(pair)
        series.append(spec)
    expected_pairs = {(asset, duration) for asset in _ASSETS for duration in _DURATIONS}
    if seen_pairs != expected_pairs:
        missing = sorted(expected_pairs - seen_pairs)
        extra = sorted(seen_pairs - expected_pairs)
        raise ValueError(
            f"series grid must cover 4 assets x 3 durations; missing={missing}, extra={extra}"
        )

    config = CollectorConfig(
        collector_id=collector_id,
        duration_seconds=int(_positive_number(raw, "duration_seconds")),
        discovery_interval_seconds=_positive_number(raw, "discovery_interval_seconds"),
        market_lookback_seconds=int(_positive_number(raw, "market_lookback_seconds")),
        market_lookahead_seconds=int(_positive_number(raw, "market_lookahead_seconds")),
        http_timeout_seconds=_positive_number(raw, "http_timeout_seconds"),
        http_retries=int(raw["http_retries"]),
        clob_heartbeat_seconds=_positive_number(raw, "clob_heartbeat_seconds"),
        rtds_heartbeat_seconds=_positive_number(raw, "rtds_heartbeat_seconds"),
        reconnect_max_seconds=_positive_number(raw, "reconnect_max_seconds"),
        stale_after_seconds=_positive_number(raw, "stale_after_seconds"),
        resolution_delay_seconds=int(_positive_number(raw, "resolution_delay_seconds")),
        flush_every_records=int(_positive_number(raw, "flush_every_records")),
        gamma_url=str(raw["gamma_url"]),
        clob_url=str(raw["clob_url"]),
        clob_ws_url=str(raw["clob_ws_url"]),
        rtds_ws_url=str(raw["rtds_ws_url"]),
        series=tuple(series),
    )
    if config.http_retries < 0:
        raise ValueError("http_retries must be non-negative")
    if config.market_lookahead_seconds < config.discovery_interval_seconds:
        raise ValueError("market lookahead must cover at least one discovery interval")
    for name in ("gamma_url", "clob_url"):
        if not getattr(config, name).startswith("https://"):
            raise ValueError(f"{name} must use https")
    for name in ("clob_ws_url", "rtds_ws_url"):
        if not getattr(config, name).startswith("wss://"):
            raise ValueError(f"{name} must use wss")
    return config
