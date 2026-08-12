from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from polymath_1M.audit.http import UrllibJsonTransport
from polymath_1M.collector.config import SeriesSpec, load_collector_config

from .config import ScreeningConfig, load_screening_config


class UniverseDataError(RuntimeError):
    """Gamma data do not satisfy the frozen Stage 4 universe contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _array(value: Any, name: str) -> list[Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list):
        raise UniverseDataError(f"{name} is not an array")
    return decoded


def _time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise UniverseDataError(f"{name} is missing")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise UniverseDataError(f"{name} has no timezone")
    return result.astimezone(UTC)


def _market_row(
    event: dict[str, Any], series: SeriesSpec, config: ScreeningConfig
) -> dict[str, Any] | None:
    end = _time(event.get("endDate"), "event.endDate")
    start = (
        _time(event["startTime"], "event.startTime")
        if event.get("startTime")
        else end - timedelta(seconds=series.duration_seconds)
    )
    start_ms = int(start.timestamp() * 1_000)
    end_ms = int(end.timestamp() * 1_000)
    if not config.period_start_ms <= start_ms < config.period_end_ms:
        return None
    if end_ms - start_ms != series.duration_seconds * 1_000:
        raise UniverseDataError(f"unexpected duration for event {event.get('slug')}")
    markets = event.get("markets")
    if not isinstance(markets, list) or len(markets) != 1:
        raise UniverseDataError(f"event {event.get('slug')} has !=1 market")
    market = markets[0]
    outcomes = tuple(str(value) for value in _array(market.get("outcomes"), "outcomes"))
    token_ids = tuple(
        str(value) for value in _array(market.get("clobTokenIds"), "clobTokenIds")
    )
    prices = tuple(
        float(value) for value in _array(market.get("outcomePrices"), "outcomePrices")
    )
    if outcomes != ("Up", "Down") or len(token_ids) != 2:
        raise UniverseDataError(f"unexpected outcome mapping for {market.get('slug')}")
    if prices == (1.0, 0.0):
        outcome_up = 1.0
    elif prices == (0.0, 1.0):
        outcome_up = 0.0
    else:
        return None
    schedule = market.get("feeSchedule")
    fee_enabled = bool(market.get("feesEnabled"))
    if fee_enabled:
        if not isinstance(schedule, dict) or schedule.get("rate") is None:
            raise UniverseDataError(
                f"fee-enabled market lacks rate: {market.get('slug')}"
            )
        fee_rate = float(schedule["rate"])
        fee_exponent = float(schedule.get("exponent", 1))
        taker_only = bool(schedule.get("takerOnly"))
    else:
        fee_rate = 0.0
        fee_exponent = 1.0
        taker_only = True
    if fee_rate < 0 or fee_exponent != 1 or not taker_only:
        raise UniverseDataError(f"unsupported fee schedule for {market.get('slug')}")
    condition_id = str(market.get("conditionId") or "").lower()
    if len(condition_id) != 66 or not condition_id.startswith("0x"):
        raise UniverseDataError(f"invalid condition ID for {market.get('slug')}")
    return {
        "series_id": series.id,
        "series_slug": series.slug,
        "asset": series.asset,
        "duration": series.duration,
        "event_id": str(event.get("id")),
        "market_id": str(market.get("id")),
        "slug": str(market.get("slug") or event.get("slug")),
        "condition_id": condition_id,
        "token_up": token_ids[0],
        "token_down": token_ids[1],
        "market_start_ms": start_ms,
        "market_end_ms": end_ms,
        "outcome_up": outcome_up,
        "fee_rate": fee_rate,
        "fee_exponent": fee_exponent,
        "fees_enabled": fee_enabled,
        "resolution_source": str(
            market.get("resolutionSource") or event.get("resolutionSource") or ""
        ),
        "rules": str(market.get("description") or event.get("description") or ""),
    }


def build_stage4_universe(config_path: str | Path) -> Path:
    config = load_screening_config(config_path)
    collector = load_collector_config(config.collector_config)
    root = Path(config.data_root)
    raw_dir = root / "gamma_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    universe_path = root / "universe.parquet"
    manifest_path = root / "universe_manifest.json"
    if universe_path.is_file() and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("config_sha256") != config.config_sha256:
            raise UniverseDataError("existing universe belongs to a different config")
        if manifest.get("universe_sha256") != _sha256(universe_path):
            raise UniverseDataError("existing universe hash mismatch")
        return manifest_path

    transport = UrllibJsonTransport(timeout_seconds=30, retries=4)
    rows: list[dict[str, Any]] = []
    raw_inventory: list[dict[str, Any]] = []
    for series in collector.series:
        page = 0
        cursor: str | None = None
        while True:
            params: dict[str, object] = {
                "series_id": series.id,
                "closed": True,
                "limit": 500,
                "end_date_min": config.period_start.isoformat(),
                "end_date_max": config.period_end_exclusive.isoformat(),
            }
            if cursor is not None:
                params["after_cursor"] = cursor
            response = transport.get_json(
                collector.gamma_url,
                "/events/keyset",
                params,
            )
            if not isinstance(response.data, dict) or not isinstance(
                response.data.get("events"), list
            ):
                raise UniverseDataError(
                    f"Gamma series {series.id} keyset page is invalid"
                )
            events = response.data["events"]
            raw_path = raw_dir / f"series_{series.id}_page_{page:05d}.json"
            temporary = raw_path.with_suffix(".json.part")
            temporary.write_bytes(response.body)
            os.replace(temporary, raw_path)
            raw_inventory.append(
                {
                    "path": str(raw_path.relative_to(root)),
                    "url": response.url,
                    "retrieved_at": response.retrieved_at,
                    "bytes": raw_path.stat().st_size,
                    "sha256": _sha256(raw_path),
                    "rows": len(events),
                }
            )
            for event in events:
                if not isinstance(event, dict):
                    raise UniverseDataError("Gamma event is not an object")
                row = _market_row(event, series, config)
                if row is not None:
                    rows.append(row)
            next_cursor = response.data.get("next_cursor")
            if not events or not next_cursor:
                break
            if next_cursor == cursor:
                raise UniverseDataError(f"Gamma cursor did not advance for {series.id}")
            cursor = str(next_cursor)
            page += 1

    rows.sort(key=lambda row: (row["market_start_ms"], row["condition_id"]))
    condition_ids = [row["condition_id"] for row in rows]
    if not rows or len(condition_ids) != len(set(condition_ids)):
        raise UniverseDataError("universe is empty or condition IDs are duplicated")
    temporary_universe = universe_path.with_suffix(".parquet.part")
    pq.write_table(pa.Table.from_pylist(rows), temporary_universe, compression="zstd")
    os.replace(temporary_universe, universe_path)
    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['asset']}_{row['duration']}"
        counts[key] = counts.get(key, 0) + 1
    manifest = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "config_sha256": config.config_sha256,
        "created_at": datetime.now(UTC).isoformat(),
        "gamma_base_url": collector.gamma_url,
        "period_start": config.period_start.isoformat(),
        "period_end_exclusive": config.period_end_exclusive.isoformat(),
        "market_count": len(rows),
        "counts": dict(sorted(counts.items())),
        "universe_path": universe_path.name,
        "universe_bytes": universe_path.stat().st_size,
        "universe_sha256": _sha256(universe_path),
        "raw_inventory": raw_inventory,
    }
    temporary_manifest = manifest_path.with_suffix(".json.part")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_manifest, manifest_path)
    return manifest_path
