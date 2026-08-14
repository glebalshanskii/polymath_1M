from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from polymath_1M.historical.pmxt import (
    load_pmxt_archive_config,
)

from .config import ScreeningConfig, load_screening_config


class ScreeningDatasetError(RuntimeError):
    """The PMXT screening dataset cannot satisfy the frozen data contract."""


BOOK_CONTRACT = "causal_price_change_hints_assume_10_usdc_at_execution_top_v3"
LEGACY_BOOK_CONTRACT = "causal_best_hints_assume_10_usdc_at_execution_top"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_metadata(
    url: str, *, expected: dict[str, Any] | None = None
) -> dict[str, Any]:
    request = Request(url, method="HEAD", headers={"User-Agent": "polymath-1m/1"})
    error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                observed = {
                    "url": url,
                    "status": response.status,
                    "bytes": int(response.headers.get("Content-Length", 0)),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                }
                if expected is not None and (
                    observed["bytes"] != expected.get("bytes")
                    or observed["etag"] != expected.get("etag")
                ):
                    raise ScreeningDatasetError(
                        f"PMXT source object changed during build: {url}"
                    )
                return observed
        except OSError as exc:
            error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise ScreeningDatasetError(f"PMXT HEAD failed for {url}: {error}")


def _invalid_row(market: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        **market,
        "snapshot_valid": False,
        "invalid_reason": reason,
        "previous_mid_up": None,
        "signal_mid_up": None,
        "signal_mid_down": None,
        "signal_bid_up": None,
        "signal_bid_down": None,
        "signal_ask_up": None,
        "signal_ask_down": None,
        "execution_ask_prices_up": [],
        "execution_ask_sizes_up": [],
        "execution_ask_prices_down": [],
        "execution_ask_sizes_down": [],
        "pmxt_event_rows": 0,
        "ignored_pre_snapshot_rows": 0,
        "initial_snapshot_received_ms": None,
    }


def _valid_top_row(
    market: dict[str, Any], snapshots: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any]:
    required = [
        snapshots.get((cutoff, side))
        for cutoff in ("previous", "signal", "execution")
        for side in ("Up", "Down")
    ]
    if any(row is None for row in required):
        return _invalid_row(market, "missing_causal_top")
    previous_up = snapshots[("previous", "Up")]
    signal_up = snapshots[("signal", "Up")]
    signal_down = snapshots[("signal", "Down")]
    execution_up = snapshots[("execution", "Up")]
    execution_down = snapshots[("execution", "Down")]
    if any(
        not 0 <= float(row["best_bid"]) <= float(row["best_ask"]) <= 1
        or float(row["best_ask"]) <= 0
        for row in required
        if row is not None
    ):
        return _invalid_row(market, "invalid_causal_top")
    assumed_notional = 10.0
    execution_ask_up = float(execution_up["best_ask"])
    execution_ask_down = float(execution_down["best_ask"])
    return {
        **market,
        "snapshot_valid": True,
        "invalid_reason": "",
        "previous_mid_up": (
            float(previous_up["best_bid"]) + float(previous_up["best_ask"])
        )
        / 2,
        "signal_mid_up": (float(signal_up["best_bid"]) + float(signal_up["best_ask"]))
        / 2,
        "signal_mid_down": (
            float(signal_down["best_bid"]) + float(signal_down["best_ask"])
        )
        / 2,
        "signal_bid_up": float(signal_up["best_bid"]),
        "signal_bid_down": float(signal_down["best_bid"]),
        "signal_ask_up": float(signal_up["best_ask"]),
        "signal_ask_down": float(signal_down["best_ask"]),
        "execution_ask_prices_up": [execution_ask_up],
        "execution_ask_sizes_up": [assumed_notional / execution_ask_up],
        "execution_ask_prices_down": [execution_ask_down],
        "execution_ask_sizes_down": [assumed_notional / execution_ask_down],
        "pmxt_event_rows": 6,
        "ignored_pre_snapshot_rows": 0,
        "initial_snapshot_received_ms": min(
            int(row["receive_timestamp_ms"]) for row in required if row is not None
        ),
    }


def _query_causal_tops(
    hour: str, markets: list[dict[str, Any]], config: ScreeningConfig
) -> tuple[str, pa.Table]:
    archive = load_pmxt_archive_config(config.pmxt_config)
    source_url = archive.object_url(hour)
    requests: list[dict[str, Any]] = []
    for market in markets:
        signal_ms = int(market["market_end_ms"]) - (
            config.decision_seconds_before_end * 1_000
        )
        cutoffs = {
            "previous": signal_ms - config.transition_horizon_seconds * 1_000,
            "signal": signal_ms,
            "execution": signal_ms + config.execution_latency_ms,
        }
        for cutoff_name, cutoff_ms in cutoffs.items():
            for outcome, token_key in (("Up", "token_up"), ("Down", "token_down")):
                requests.append(
                    {
                        "condition_id": market["condition_id"],
                        "market": market["condition_id"].encode(),
                        "asset_id": market[token_key],
                        "outcome": outcome,
                        "cutoff_name": cutoff_name,
                        "cutoff_ms": cutoff_ms,
                    }
                )
    requested = pa.Table.from_pylist(requests)
    conditions = tuple(market["condition_id"].encode() for market in markets)
    placeholders = ",".join("?" for _ in conditions)
    sql = f"""
        SELECT
            r.condition_id,
            r.outcome,
            r.cutoff_name,
            epoch_ms(p.timestamp_received) AS receive_timestamp_ms,
            CAST(p.best_bid AS DOUBLE) AS best_bid,
            CAST(p.best_ask AS DOUBLE) AS best_ask
        FROM read_parquet(?) AS p
        JOIN requested AS r
          ON p.market = r.market AND p.asset_id = r.asset_id
        WHERE p.market IN ({placeholders})
          AND p.event_type = 'price_change'
          AND epoch_ms(p.timestamp_received) <= r.cutoff_ms
          AND p.best_bid IS NOT NULL
          AND p.best_ask IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY r.condition_id, r.outcome, r.cutoff_name
            ORDER BY p.timestamp_received DESC, p.timestamp DESC
        ) = 1
    """
    temporary_dir = Path(".tmp") / "stage4_pmxt_duckdb" / hour.replace(":", "")
    temporary_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        connection.execute("SET memory_limit='1GB'")
        connection.execute("SET threads=2")
        connection.execute("SET temp_directory=?", [str(temporary_dir)])
        connection.register("requested", requested)
        table = connection.execute(sql, [source_url, *conditions]).fetch_arrow_table()
    finally:
        connection.close()
    return source_url, table


def _hour_key(market_start_ms: int) -> str:
    return datetime.fromtimestamp(market_start_ms / 1_000, tz=UTC).strftime(
        "%Y-%m-%dT%H"
    )


def _build_hour(
    hour: str,
    markets: list[dict[str, Any]],
    config: ScreeningConfig,
    output_dir: Path,
) -> dict[str, Any]:
    output_path = output_dir / f"hour={hour}.parquet"
    metadata_path = output_dir / f"hour={hour}.json"
    existing_rows: list[dict[str, Any]] | None = None
    if output_path.is_file() and metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("data_contract_sha256") == config.data_contract_sha256
            and metadata.get("book_contract") == BOOK_CONTRACT
            and metadata.get("parquet_sha256") == _sha256(output_path)
            and metadata.get("market_count") == len(markets)
        ):
            return metadata
        if (
            metadata.get("data_contract_sha256") == config.data_contract_sha256
            and metadata.get("book_contract") == LEGACY_BOOK_CONTRACT
            and metadata.get("parquet_sha256") == _sha256(output_path)
            and metadata.get("market_count") == len(markets)
        ):
            existing_rows = pq.read_table(output_path).to_pylist()
            retry_ids = {
                str(row["condition_id"])
                for row in existing_rows
                if row["invalid_reason"] == "invalid_causal_top"
            }
            if not retry_ids:
                metadata["book_contract"] = BOOK_CONTRACT
                temporary_metadata = metadata_path.with_suffix(".json.part")
                temporary_metadata.write_text(
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary_metadata, metadata_path)
                return metadata
            markets = [
                market for market in markets if market["condition_id"] in retry_ids
            ]

    error: Exception | None = None
    for attempt in range(3):
        try:
            source_url, table = _query_causal_tops(hour, markets, config)
            break
        except (duckdb.Error, OSError, RuntimeError) as exc:
            error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    else:
        raise ScreeningDatasetError(f"PMXT query failed for {hour}: {error}")

    refreshed_rows: list[dict[str, Any]] = []
    by_condition: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for row in table.to_pylist():
        by_condition[str(row["condition_id"]).lower()][
            (str(row["cutoff_name"]), str(row["outcome"]))
        ] = row
    for market in markets:
        condition_id = market["condition_id"]
        refreshed_rows.append(
            _valid_top_row(market, by_condition.get(condition_id, {}))
        )
    if existing_rows is None:
        rows = refreshed_rows
    else:
        refreshed = {str(row["condition_id"]): row for row in refreshed_rows}
        rows = [refreshed.get(str(row["condition_id"]), row) for row in existing_rows]

    temporary = output_path.with_suffix(".parquet.part")
    pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
    os.replace(temporary, output_path)
    reason_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        reason_counts[row["invalid_reason"] or "valid"] += 1
    metadata = {
        "schema_version": 1,
        "hour": hour,
        "config_sha256": config.config_sha256,
        "data_contract_sha256": config.data_contract_sha256,
        "pmxt_config_sha256": load_pmxt_archive_config(
            config.pmxt_config
        ).config_sha256,
        "book_contract": BOOK_CONTRACT,
        "source_url": source_url,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "source_event_rows": table.num_rows,
        "market_count": len(rows),
        "valid_count": sum(bool(row["snapshot_valid"]) for row in rows),
        "reason_counts": dict(sorted(reason_counts.items())),
        "parquet_path": str(output_path),
        "parquet_bytes": output_path.stat().st_size,
        "parquet_sha256": _sha256(output_path),
    }
    temporary_metadata = metadata_path.with_suffix(".json.part")
    temporary_metadata.write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_metadata, metadata_path)
    return metadata


def build_stage4_pmxt_dataset(config_path: str | Path) -> Path:
    config = load_screening_config(config_path)
    root = Path(config.data_root)
    universe_path = root / "universe.parquet"
    universe_manifest_path = root / "universe_manifest.json"
    if not universe_path.is_file() or not universe_manifest_path.is_file():
        raise ScreeningDatasetError("build the frozen Gamma universe first")
    universe_manifest = json.loads(universe_manifest_path.read_text(encoding="utf-8"))
    if universe_manifest.get("data_contract_sha256") != config.data_contract_sha256:
        raise ScreeningDatasetError("universe and screening config hashes differ")
    markets = pq.read_table(universe_path).to_pylist()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for market in markets:
        grouped[_hour_key(int(market["market_start_ms"]))].append(market)
    output_dir = root / "pmxt_hours"
    output_dir.mkdir(parents=True, exist_ok=True)
    inventory: list[dict[str, Any]] = []
    print(f"Stage 4 PMXT cache: {len(markets)} markets / {len(grouped)} hours")
    with ThreadPoolExecutor(max_workers=config.pmxt_workers) as executor:
        iterator = iter(sorted(grouped.items()))
        futures = {}
        for _ in range(config.pmxt_workers):
            try:
                hour, hour_rows = next(iterator)
            except StopIteration:
                break
            future = executor.submit(_build_hour, hour, hour_rows, config, output_dir)
            futures[future] = hour
        completed_count = 0
        while futures:
            completed = next(as_completed(futures))
            inventory.append(completed.result())
            del futures[completed]
            completed_count += 1
            if (
                completed_count == 1
                or completed_count % 24 == 0
                or completed_count == len(grouped)
            ):
                print(
                    f"Stage 4 PMXT cache progress: {completed_count}/{len(grouped)}",
                    flush=True,
                )
            try:
                hour, hour_rows = next(iterator)
            except StopIteration:
                continue
            future = executor.submit(_build_hour, hour, hour_rows, config, output_dir)
            futures[future] = hour
    inventory.sort(key=lambda item: item["hour"])
    if sum(int(item["market_count"]) for item in inventory) != len(markets):
        raise ScreeningDatasetError("PMXT hourly inventory lost universe markets")
    reason_counts: dict[str, int] = defaultdict(int)
    for item in inventory:
        for reason, count in item["reason_counts"].items():
            reason_counts[reason] += int(count)
    source_lock_path = root / "pmxt_source_lock.json"
    expected_by_url: dict[str, dict[str, Any]] = {}
    if source_lock_path.is_file():
        source_lock = json.loads(source_lock_path.read_text(encoding="utf-8"))
        if source_lock.get("data_contract_sha256") != config.data_contract_sha256:
            raise ScreeningDatasetError("PMXT source lock belongs to another dataset")
        expected_by_url = {
            str(item["url"]): item for item in source_lock.get("sources", [])
        }
    urls = [str(item["source_url"]) for item in inventory]
    with ThreadPoolExecutor(max_workers=config.pmxt_workers) as executor:
        futures = {
            executor.submit(
                _remote_metadata, url, expected=expected_by_url.get(url)
            ): url
            for url in urls
        }
        source_inventory = [future.result() for future in as_completed(futures)]
    source_inventory.sort(key=lambda item: item["url"])
    if not source_lock_path.is_file():
        source_lock = {
            "schema_version": 1,
            "data_contract_sha256": config.data_contract_sha256,
            "created_at": datetime.now(UTC).isoformat(),
            "sources": source_inventory,
        }
        temporary_source_lock = source_lock_path.with_suffix(".json.part")
        temporary_source_lock.write_text(
            json.dumps(source_lock, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_source_lock, source_lock_path)
    manifest = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "config_sha256": config.config_sha256,
        "data_contract_sha256": config.data_contract_sha256,
        "created_at": datetime.now(UTC).isoformat(),
        "universe_sha256": universe_manifest["universe_sha256"],
        "hour_count": len(inventory),
        "market_count": len(markets),
        "valid_count": sum(int(item["valid_count"]) for item in inventory),
        "reason_counts": dict(sorted(reason_counts.items())),
        "source_inventory": source_inventory,
        "hours": inventory,
    }
    manifest_path = root / "pmxt_dataset_manifest.json"
    temporary = manifest_path.with_suffix(".json.part")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)
    return manifest_path
