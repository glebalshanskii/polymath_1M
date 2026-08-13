from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import subprocess
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import plotly
import plotly.graph_objects as go
import pyarrow as pa
import pyarrow.parquet as pq
import torch
from plotly.subplots import make_subplots

from polymath_1M.historical.pmxt import load_pmxt_archive_config
from polymath_1M.strategy.model import calculate_platform_fee


class PracticalChainError(RuntimeError):
    """The frozen Stage 4h contract or its inputs are invalid."""


@dataclass(frozen=True)
class PracticalChainConfig:
    experiment_id: str
    universe_path: str
    universe_manifest_path: str
    pmxt_config: str
    cache_root: str
    asset: str
    duration: str
    development_start: datetime
    development_end_exclusive: datetime
    holdout_end_exclusive: datetime
    state_seconds_before_end: tuple[int, ...]
    execution_latency_ms: int
    state_bin_edges: tuple[float, ...]
    minimum_transition_support: int
    minimum_terminal_support: int
    minimum_net_edge: float
    target_notional_usdc: float
    minimum_fill_notional_usdc: float
    platform_fee_round_decimals: int
    primary_extra_cost_per_share: float
    stress_extra_cost_per_share: float
    initial_train_days: int
    validation_fold_days: int
    validation_fold_count: int
    minimum_total_fills: int
    minimum_positive_folds: int
    minimum_profit_factor: float
    pmxt_workers: int
    seed: int
    device: str
    dtype: str
    config_sha256: str

    @property
    def development_start_ms(self) -> int:
        return int(self.development_start.timestamp() * 1_000)

    @property
    def development_end_ms(self) -> int:
        return int(self.development_end_exclusive.timestamp() * 1_000)


@dataclass(frozen=True)
class ChainDataset:
    condition_ids: tuple[str, ...]
    token_up: tuple[str, ...]
    token_down: tuple[str, ...]
    market_start_ms: torch.Tensor
    market_end_ms: torch.Tensor
    outcome_up: torch.Tensor
    fee_rate: torch.Tensor
    fee_exponent: torch.Tensor
    mid_up: torch.Tensor
    bids: torch.Tensor
    asks: torch.Tensor
    receive_timestamp_ms: torch.Tensor
    valid: torch.Tensor

    def __len__(self) -> int:
        return len(self.condition_ids)

    def index(self, indices: torch.Tensor) -> ChainDataset:
        if indices.dtype != torch.int64 or indices.ndim != 1:
            raise TypeError("chain dataset indices must be one-dimensional int64")
        cpu = indices.detach().cpu().tolist()
        return ChainDataset(
            condition_ids=tuple(self.condition_ids[index] for index in cpu),
            token_up=tuple(self.token_up[index] for index in cpu),
            token_down=tuple(self.token_down[index] for index in cpu),
            market_start_ms=self.market_start_ms.index_select(0, indices),
            market_end_ms=self.market_end_ms.index_select(0, indices),
            outcome_up=self.outcome_up.index_select(0, indices),
            fee_rate=self.fee_rate.index_select(0, indices),
            fee_exponent=self.fee_exponent.index_select(0, indices),
            mid_up=self.mid_up.index_select(0, indices),
            bids=self.bids.index_select(0, indices),
            asks=self.asks.index_select(0, indices),
            receive_timestamp_ms=self.receive_timestamp_ms.index_select(0, indices),
            valid=self.valid.index_select(0, indices),
        )

    def to(self, device: torch.device) -> ChainDataset:
        return ChainDataset(
            condition_ids=self.condition_ids,
            token_up=self.token_up,
            token_down=self.token_down,
            market_start_ms=self.market_start_ms.to(device),
            market_end_ms=self.market_end_ms.to(device),
            outcome_up=self.outcome_up.to(device),
            fee_rate=self.fee_rate.to(device),
            fee_exponent=self.fee_exponent.to(device),
            mid_up=self.mid_up.to(device),
            bids=self.bids.to(device),
            asks=self.asks.to(device),
            receive_timestamp_ms=self.receive_timestamp_ms.to(device),
            valid=self.valid.to(device),
        )


@dataclass(frozen=True)
class RawChainModel:
    edges: torch.Tensor
    transition_counts: torch.Tensor
    transition_support: torch.Tensor
    transition_matrix: torch.Tensor
    terminal_counts: torch.Tensor
    terminal_support: torch.Tensor
    terminal_up: torch.Tensor
    available: torch.Tensor
    value_up: torch.Tensor


@dataclass(frozen=True)
class SignalEvaluation:
    states: torch.Tensor
    probability_up: torch.Tensor
    forecast_available: torch.Tensor
    side: torch.Tensor
    probability_side: torch.Tensor
    ask: torch.Tensor
    edge: torch.Tensor
    signal: torch.Tensor
    order_checkpoint: torch.Tensor


@dataclass(frozen=True)
class Fold:
    fold_id: str
    train_start_ms: int
    train_end_ms: int
    validation_start_ms: int
    validation_end_ms: int


EXPECTED_CONFIG_FIELDS = {
    "schema_version",
    "experiment_id",
    "universe_path",
    "universe_manifest_path",
    "pmxt_config",
    "cache_root",
    "asset",
    "duration",
    "development_start",
    "development_end_exclusive",
    "holdout_end_exclusive",
    "state_seconds_before_end",
    "execution_latency_ms",
    "state_bin_edges",
    "minimum_transition_support",
    "minimum_terminal_support",
    "minimum_net_edge",
    "target_notional_usdc",
    "minimum_fill_notional_usdc",
    "platform_fee_round_decimals",
    "primary_extra_cost_per_share",
    "stress_extra_cost_per_share",
    "initial_train_days",
    "validation_fold_days",
    "validation_fold_count",
    "minimum_total_fills",
    "minimum_positive_folds",
    "minimum_profit_factor",
    "pmxt_workers",
    "seed",
    "device",
    "dtype",
}


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PracticalChainError("Stage 4h timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise PracticalChainError(f"cannot write empty CSV: {path}")
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def load_practical_chain_config(path: str | Path) -> PracticalChainConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.keys() != EXPECTED_CONFIG_FIELDS:
        raise PracticalChainError(
            "Stage 4h config fields differ: "
            f"missing={sorted(EXPECTED_CONFIG_FIELDS - payload.keys())}, "
            f"extra={sorted(payload.keys() - EXPECTED_CONFIG_FIELDS)}"
        )
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    start = _utc(payload["development_start"])
    development_end = _utc(payload["development_end_exclusive"])
    holdout_end = _utc(payload["holdout_end_exclusive"])
    seconds = tuple(int(value) for value in payload["state_seconds_before_end"])
    edges = tuple(float(value) for value in payload["state_bin_edges"])
    if (
        payload["schema_version"] != 1
        or payload["asset"] != "BTC"
        or payload["duration"] != "5m"
        or seconds != (120, 105, 90, 75, 60, 45, 30, 15)
        or int(payload["execution_latency_ms"]) != 1_000
        or edges
        != (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.000001)
        or int(payload["minimum_transition_support"]) != 100
        or int(payload["minimum_terminal_support"]) != 100
        or float(payload["minimum_net_edge"]) != 0.02
        or float(payload["target_notional_usdc"]) != 10.0
        or float(payload["minimum_fill_notional_usdc"]) != 1.0
        or int(payload["platform_fee_round_decimals"]) != 4
        or float(payload["primary_extra_cost_per_share"]) != 0.0
        or float(payload["stress_extra_cost_per_share"]) != 0.01
        or int(payload["initial_train_days"]) != 24
        or int(payload["validation_fold_days"]) != 6
        or int(payload["validation_fold_count"]) != 4
        or int(payload["minimum_total_fills"]) != 100
        or int(payload["minimum_positive_folds"]) != 3
        or float(payload["minimum_profit_factor"]) != 1.0
        or int(payload["pmxt_workers"]) <= 0
        or payload["seed"] != 20260813
        or payload["device"] != "cuda"
        or payload["dtype"] != "float64"
        or not start < development_end < holdout_end
        or (development_end - start).days != 48
    ):
        raise PracticalChainError("Stage 4h frozen numerical contract differs")
    return PracticalChainConfig(
        experiment_id=str(payload["experiment_id"]),
        universe_path=str(payload["universe_path"]),
        universe_manifest_path=str(payload["universe_manifest_path"]),
        pmxt_config=str(payload["pmxt_config"]),
        cache_root=str(payload["cache_root"]),
        asset="BTC",
        duration="5m",
        development_start=start,
        development_end_exclusive=development_end,
        holdout_end_exclusive=holdout_end,
        state_seconds_before_end=seconds,
        execution_latency_ms=1_000,
        state_bin_edges=edges,
        minimum_transition_support=100,
        minimum_terminal_support=100,
        minimum_net_edge=0.02,
        target_notional_usdc=10.0,
        minimum_fill_notional_usdc=1.0,
        platform_fee_round_decimals=4,
        primary_extra_cost_per_share=0.0,
        stress_extra_cost_per_share=0.01,
        initial_train_days=24,
        validation_fold_days=6,
        validation_fold_count=4,
        minimum_total_fills=100,
        minimum_positive_folds=3,
        minimum_profit_factor=1.0,
        pmxt_workers=int(payload["pmxt_workers"]),
        seed=20260813,
        device="cuda",
        dtype="float64",
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _hour_key(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC).strftime(
        "%Y-%m-%dT%H"
    )


def _load_development_universe(config: PracticalChainConfig) -> list[dict[str, Any]]:
    manifest_path = Path(config.universe_manifest_path)
    universe_path = Path(config.universe_path)
    if not manifest_path.is_file() or not universe_path.is_file():
        raise PracticalChainError("Stage 4h universe or manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("universe_sha256") != _sha256(universe_path):
        raise PracticalChainError("Stage 4h universe hash differs from its manifest")
    table = pq.read_table(
        universe_path,
        filters=[
            ("asset", "=", config.asset),
            ("duration", "=", config.duration),
            ("market_start_ms", ">=", config.development_start_ms),
            ("market_start_ms", "<", config.development_end_ms),
        ],
    )
    rows = sorted(table.to_pylist(), key=lambda row: int(row["market_start_ms"]))
    if not rows or any(
        not config.development_start_ms
        <= int(row["market_start_ms"])
        < config.development_end_ms
        for row in rows
    ):
        raise PracticalChainError("Stage 4h development filter is empty or leaked")
    return rows


def _request_table(markets: list[dict[str, Any]]) -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                "condition_id": market["condition_id"],
                "market": market["condition_id"].encode(),
                "asset_id": market[token_key],
                "outcome": outcome,
                "first_cutoff_ms": int(market["market_end_ms"]) - 120_000,
                "last_cutoff_ms": int(market["market_end_ms"]) - 15_000,
            }
            for market in markets
            for outcome, token_key in (("Up", "token_up"), ("Down", "token_down"))
        ]
    )


TOP_QUERY = """
WITH top_events AS (
    SELECT
        r.condition_id,
        r.outcome,
        r.first_cutoff_ms,
        r.last_cutoff_ms,
        epoch_ms(p.timestamp_received) AS receive_timestamp_ms,
        epoch_ms(p.timestamp) AS source_timestamp_ms,
        CAST(p.best_bid AS DOUBLE) AS best_bid,
        CAST(p.best_ask AS DOUBLE) AS best_ask
    FROM read_parquet(?) AS p
    JOIN requested AS r
      ON p.market = r.market AND p.asset_id = r.asset_id
    WHERE p.event_type = 'price_change'
      AND epoch_ms(p.timestamp_received) <= r.last_cutoff_ms
      AND p.best_bid IS NOT NULL
      AND p.best_ask IS NOT NULL
),
bucketed AS (
    SELECT
        condition_id,
        outcome,
        least(7, greatest(0, CAST(ceil(
            (receive_timestamp_ms - first_cutoff_ms) / 15000.0
        ) AS INTEGER))) AS checkpoint_index,
        receive_timestamp_ms,
        source_timestamp_ms,
        best_bid,
        best_ask
    FROM top_events
    WHERE best_bid IS NOT NULL AND best_ask IS NOT NULL
),
latest AS (
    SELECT
        condition_id,
        outcome,
        checkpoint_index,
        arg_max(receive_timestamp_ms, struct_pack(
            r := receive_timestamp_ms, s := source_timestamp_ms
        )) AS receive_timestamp_ms,
        arg_max(best_bid, struct_pack(
            r := receive_timestamp_ms, s := source_timestamp_ms
        )) AS best_bid,
        arg_max(best_ask, struct_pack(
            r := receive_timestamp_ms, s := source_timestamp_ms
        )) AS best_ask
    FROM bucketed
    GROUP BY condition_id, outcome, checkpoint_index
)
SELECT * FROM latest ORDER BY condition_id, checkpoint_index, outcome
"""


def _top_rows(
    table: pa.Table, market: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    by_key = {
        (str(row["outcome"]), int(row["checkpoint_index"])): row
        for row in table.to_pylist()
        if str(row["condition_id"]) == market["condition_id"]
    }
    bids = [[None, None] for _ in range(8)]
    asks = [[None, None] for _ in range(8)]
    receive = [[None, None] for _ in range(8)]
    valid = [False] * 8
    invalid_reason = ""
    for side, outcome in enumerate(("Up", "Down")):
        previous: dict[str, Any] | None = None
        for checkpoint in range(8):
            previous = by_key.get((outcome, checkpoint), previous)
            if previous is None:
                continue
            bids[checkpoint][side] = float(previous["best_bid"])
            asks[checkpoint][side] = float(previous["best_ask"])
            receive[checkpoint][side] = int(previous["receive_timestamp_ms"])
    for checkpoint in range(8):
        values = (*bids[checkpoint], *asks[checkpoint])
        valid[checkpoint] = all(
            value is not None and math.isfinite(value) for value in values
        ) and all(
            0 <= float(bids[checkpoint][side])
            < float(asks[checkpoint][side])
            <= 1
            for side in range(2)
        )
    if not all(valid):
        invalid_reason = "missing_or_invalid_two_sided_top"
    mid_up = [
        (float(bids[index][0]) + float(asks[index][0])) / 2
        if valid[index]
        else None
        for index in range(8)
    ]
    return (
        {
            **market,
            "mid_up": mid_up,
            "bids": bids,
            "asks": asks,
            "receive_timestamp_ms": receive,
            "valid": valid,
            "snapshot_valid": all(valid),
            "invalid_reason": invalid_reason,
        },
        invalid_reason or "valid",
    )


def _build_top_hour(
    hour: str,
    markets: list[dict[str, Any]],
    config: PracticalChainConfig,
    output_dir: Path,
) -> dict[str, Any]:
    output_path = output_dir / f"hour={hour}.parquet"
    metadata_path = output_dir / f"hour={hour}.json"
    if output_path.is_file() and metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("config_sha256") == config.config_sha256
            and metadata.get("market_count") == len(markets)
            and metadata.get("parquet_sha256") == _sha256(output_path)
        ):
            return metadata
    archive = load_pmxt_archive_config(config.pmxt_config)
    source_url = archive.object_url(hour)
    requested = _request_table(markets)
    temporary_dir = Path(".tmp") / "stage4h_duckdb" / hour.replace(":", "")
    temporary_dir.mkdir(parents=True, exist_ok=True)
    error: Exception | None = None
    for attempt in range(8):
        connection = duckdb.connect()
        try:
            connection.execute("SET memory_limit='1GB'")
            connection.execute("SET temp_directory=?", [str(temporary_dir)])
            connection.register("requested", requested)
            table = connection.execute(TOP_QUERY, [source_url]).fetch_arrow_table()
            break
        except (duckdb.Error, OSError, RuntimeError) as exc:
            error = exc
            if attempt < 7:
                time.sleep(min(30, 2**attempt))
        finally:
            connection.close()
    else:
        raise PracticalChainError(f"PMXT top query failed for {hour}: {error}")
    rows: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for market in markets:
        row, reason = _top_rows(table, market)
        rows.append(row)
        reasons[reason] += 1
    temporary = output_path.with_suffix(".parquet.part")
    pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
    os.replace(temporary, output_path)
    metadata = {
        "schema_version": 1,
        "hour": hour,
        "config_sha256": config.config_sha256,
        "source_url": source_url,
        "market_count": len(rows),
        "valid_count": sum(bool(row["snapshot_valid"]) for row in rows),
        "reason_counts": dict(sorted(reasons.items())),
        "top_event_rows": table.num_rows,
        "parquet_sha256": _sha256(output_path),
        "parquet_bytes": output_path.stat().st_size,
    }
    _write_json(metadata_path, metadata)
    return metadata


def build_practical_chain_cache(config_path: str | Path) -> Path:
    config = load_practical_chain_config(config_path)
    markets = _load_development_universe(config)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for market in markets:
        grouped[_hour_key(int(market["market_start_ms"]))].append(market)
    cache_root = Path(config.cache_root)
    output_dir = cache_root / "top_hours"
    output_dir.mkdir(parents=True, exist_ok=True)
    inventory: list[dict[str, Any]] = []
    print(f"Stage 4h top cache: {len(markets)} markets / {len(grouped)} hours")
    with ThreadPoolExecutor(max_workers=config.pmxt_workers) as executor:
        iterator = iter(sorted(grouped.items()))
        futures = {}
        for _ in range(config.pmxt_workers):
            try:
                hour, rows = next(iterator)
            except StopIteration:
                break
            futures[executor.submit(_build_top_hour, hour, rows, config, output_dir)] = hour
        completed = 0
        while futures:
            future = next(as_completed(futures))
            inventory.append(future.result())
            del futures[future]
            completed += 1
            if completed == 1 or completed % 24 == 0 or completed == len(grouped):
                print(
                    f"Stage 4h top cache progress: {completed}/{len(grouped)}",
                    flush=True,
                )
            try:
                hour, rows = next(iterator)
            except StopIteration:
                continue
            futures[executor.submit(_build_top_hour, hour, rows, config, output_dir)] = hour
    inventory.sort(key=lambda item: item["hour"])
    if sum(int(item["market_count"]) for item in inventory) != len(markets):
        raise PracticalChainError("Stage 4h top cache lost markets")
    manifest = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "config_sha256": config.config_sha256,
        "created_at": datetime.now(UTC).isoformat(),
        "universe_path": config.universe_path,
        "universe_sha256": _sha256(config.universe_path),
        "pmxt_config_sha256": load_pmxt_archive_config(
            config.pmxt_config
        ).config_sha256,
        "market_count": len(markets),
        "valid_count": sum(int(item["valid_count"]) for item in inventory),
        "hour_count": len(inventory),
        "holdout_rows_read": 0,
        "hours": inventory,
    }
    manifest_path = cache_root / "top_cache_manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def load_practical_chain_dataset(config: PracticalChainConfig) -> ChainDataset:
    cache_root = Path(config.cache_root)
    manifest_path = cache_root / "top_cache_manifest.json"
    if not manifest_path.is_file():
        raise PracticalChainError("build the Stage 4h top cache first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("config_sha256") != config.config_sha256
        or manifest.get("holdout_rows_read") != 0
    ):
        raise PracticalChainError("Stage 4h cache contract differs or leaked holdout")
    tables = []
    for item in manifest["hours"]:
        path = cache_root / "top_hours" / f"hour={item['hour']}.parquet"
        if _sha256(path) != item["parquet_sha256"]:
            raise PracticalChainError(f"Stage 4h cache hash differs: {path}")
        tables.append(pq.read_table(path))
    table = pa.concat_tables(tables, promote_options="default")
    rows = sorted(table.to_pylist(), key=lambda row: int(row["market_start_ms"]))
    if len(rows) != int(manifest["market_count"]):
        raise PracticalChainError("Stage 4h cache row count differs")
    count = len(rows)
    mid = torch.full((count, 8), torch.nan, dtype=torch.float64)
    bids = torch.full((count, 8, 2), torch.nan, dtype=torch.float64)
    asks = torch.full((count, 8, 2), torch.nan, dtype=torch.float64)
    receive = torch.full((count, 8, 2), -1, dtype=torch.int64)
    valid = torch.zeros((count, 8), dtype=torch.bool)
    for index, row in enumerate(rows):
        mid[index] = torch.tensor(
            [float("nan") if value is None else value for value in row["mid_up"]],
            dtype=torch.float64,
        )
        bids[index] = torch.tensor(
            [
                [float("nan") if value is None else value for value in pair]
                for pair in row["bids"]
            ],
            dtype=torch.float64,
        )
        asks[index] = torch.tensor(
            [
                [float("nan") if value is None else value for value in pair]
                for pair in row["asks"]
            ],
            dtype=torch.float64,
        )
        receive[index] = torch.tensor(
            [[-1 if value is None else value for value in pair] for pair in row["receive_timestamp_ms"]],
            dtype=torch.int64,
        )
        valid[index] = torch.tensor(row["valid"], dtype=torch.bool)
    return ChainDataset(
        condition_ids=tuple(str(row["condition_id"]) for row in rows),
        token_up=tuple(str(row["token_up"]) for row in rows),
        token_down=tuple(str(row["token_down"]) for row in rows),
        market_start_ms=torch.tensor(
            [row["market_start_ms"] for row in rows], dtype=torch.int64
        ),
        market_end_ms=torch.tensor(
            [row["market_end_ms"] for row in rows], dtype=torch.int64
        ),
        outcome_up=torch.tensor(
            [row["outcome_up"] for row in rows], dtype=torch.float64
        ),
        fee_rate=torch.tensor(
            [row["fee_rate"] for row in rows], dtype=torch.float64
        ),
        fee_exponent=torch.tensor(
            [row["fee_exponent"] for row in rows], dtype=torch.float64
        ),
        mid_up=mid,
        bids=bids,
        asks=asks,
        receive_timestamp_ms=receive,
        valid=valid,
    )


def fit_raw_chain(
    mid_up: torch.Tensor,
    valid: torch.Tensor,
    outcome_up: torch.Tensor,
    edges: torch.Tensor,
    *,
    minimum_transition_support: int,
    minimum_terminal_support: int,
) -> RawChainModel:
    if (
        mid_up.dtype != torch.float64
        or outcome_up.dtype != torch.float64
        or valid.dtype != torch.bool
        or mid_up.ndim != 2
        or mid_up.shape[1] != 8
        or valid.shape != mid_up.shape
        or outcome_up.shape != (mid_up.shape[0],)
        or edges.dtype != torch.float64
        or edges.shape != (9,)
    ):
        raise TypeError("raw chain tensors differ from the frozen shape/dtype")
    states = torch.bucketize(mid_up, edges[1:-1], right=False)
    pair_valid = valid[:, :-1] & valid[:, 1:]
    checkpoint = torch.arange(7, device=mid_up.device).expand(mid_up.shape[0], -1)
    encoded = checkpoint * 64 + states[:, :-1] * 8 + states[:, 1:]
    transition_counts = torch.bincount(
        encoded[pair_valid], minlength=7 * 64
    ).reshape(7, 8, 8)
    transition_support = transition_counts.sum(dim=2)
    transition_matrix = torch.where(
        transition_support.unsqueeze(2) > 0,
        transition_counts.to(torch.float64)
        / transition_support.unsqueeze(2).clamp_min(1).to(torch.float64),
        torch.zeros_like(transition_counts, dtype=torch.float64),
    )
    terminal_valid = valid[:, -1] & torch.isfinite(outcome_up)
    terminal_state = states[:, -1][terminal_valid]
    terminal_support = torch.bincount(terminal_state, minlength=8)
    terminal_wins = torch.bincount(
        terminal_state,
        weights=outcome_up[terminal_valid],
        minlength=8,
    ).to(torch.float64)
    terminal_up = torch.where(
        terminal_support > 0,
        terminal_wins / terminal_support.clamp_min(1).to(torch.float64),
        torch.zeros(8, dtype=torch.float64, device=mid_up.device),
    )
    available_rows = [terminal_support >= minimum_terminal_support]
    value_rows = [terminal_up]
    downstream_available = available_rows[0]
    downstream_value = value_rows[0]
    for checkpoint_index in range(6, -1, -1):
        matrix = transition_matrix[checkpoint_index]
        supported = transition_support[checkpoint_index] >= minimum_transition_support
        touches_unavailable = (matrix[:, ~downstream_available] > 0).any(dim=1)
        current_available = supported & ~touches_unavailable
        current_value = matrix @ downstream_value
        current_value = torch.where(
            current_available, current_value, torch.zeros_like(current_value)
        )
        available_rows.append(current_available)
        value_rows.append(current_value)
        downstream_available = current_available
        downstream_value = current_value
    available = torch.stack(tuple(reversed(available_rows)), dim=0)
    value_up = torch.stack(tuple(reversed(value_rows)), dim=0)
    if bool(
        ((transition_support > 0) & ~torch.isclose(
            transition_matrix.sum(dim=2),
            torch.ones_like(transition_support, dtype=torch.float64),
            atol=1e-12,
            rtol=0,
        )).any().item()
    ):
        raise PracticalChainError("raw transition rows do not sum to one")
    if bool(((value_up[available] < 0) | (value_up[available] > 1)).any().item()):
        raise PracticalChainError("raw chain absorption probability is outside [0,1]")
    return RawChainModel(
        edges=edges,
        transition_counts=transition_counts,
        transition_support=transition_support,
        transition_matrix=transition_matrix,
        terminal_counts=torch.stack(
            (terminal_wins, terminal_support.to(torch.float64) - terminal_wins), dim=1
        ),
        terminal_support=terminal_support,
        terminal_up=terminal_up,
        available=available,
        value_up=value_up,
    )


def evaluate_signals(
    dataset: ChainDataset,
    model: RawChainModel | None,
    *,
    minimum_net_edge: float,
) -> SignalEvaluation:
    states = torch.bucketize(dataset.mid_up, model.edges[1:-1] if model else torch.tensor(
        (0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875),
        dtype=torch.float64,
        device=dataset.mid_up.device,
    ), right=False)
    if model is None:
        probability_up = dataset.mid_up
        forecast_available = dataset.valid & torch.isfinite(probability_up)
    else:
        checkpoints = torch.arange(8, device=dataset.mid_up.device).expand_as(states)
        probability_up = model.value_up[checkpoints, states]
        forecast_available = dataset.valid & model.available[checkpoints, states]
    probabilities = torch.stack((probability_up, 1 - probability_up), dim=2)
    fee = dataset.fee_rate[:, None, None] * torch.pow(
        dataset.asks * (1 - dataset.asks), dataset.fee_exponent[:, None, None]
    )
    side_edge = probabilities - dataset.asks - fee
    comparable = torch.nan_to_num(side_edge, nan=-torch.inf)
    side = torch.argmax(comparable, dim=2)
    gather = side.unsqueeze(2)
    probability_side = probabilities.gather(2, gather).squeeze(2)
    ask = dataset.asks.gather(2, gather).squeeze(2)
    edge = side_edge.gather(2, gather).squeeze(2)
    quote_valid = (
        torch.isfinite(dataset.bids).all(dim=2)
        & torch.isfinite(dataset.asks).all(dim=2)
        & (dataset.bids < dataset.asks).all(dim=2)
        & (dataset.bids >= 0).all(dim=2)
        & (dataset.asks <= 1).all(dim=2)
    )
    ties = torch.isclose(comparable[:, :, 0], comparable[:, :, 1], atol=1e-12, rtol=0)
    signal = (
        forecast_available
        & quote_valid
        & ~ties
        & torch.isfinite(edge)
        & (edge >= minimum_net_edge)
    )
    any_signal = signal.any(dim=1)
    first = torch.argmax(signal.to(torch.int64), dim=1)
    order_checkpoint = torch.where(
        any_signal, first, torch.full_like(first, -1)
    )
    return SignalEvaluation(
        states=states,
        probability_up=probability_up,
        forecast_available=forecast_available,
        side=side,
        probability_side=probability_side,
        ask=ask,
        edge=edge,
        signal=signal,
        order_checkpoint=order_checkpoint,
    )


def _folds(config: PracticalChainConfig) -> tuple[Fold, ...]:
    day_ms = 86_400_000
    start = config.development_start_ms
    first_validation = start + config.initial_train_days * day_ms
    folds = tuple(
        Fold(
            fold_id=f"fold_{index}",
            train_start_ms=start,
            train_end_ms=first_validation + index * config.validation_fold_days * day_ms,
            validation_start_ms=first_validation
            + index * config.validation_fold_days * day_ms,
            validation_end_ms=first_validation
            + (index + 1) * config.validation_fold_days * day_ms,
        )
        for index in range(config.validation_fold_count)
    )
    if folds[-1].validation_end_ms != config.development_end_ms:
        raise PracticalChainError("Stage 4h folds do not exhaust development interval")
    return folds


def _indices(dataset: ChainDataset, start_ms: int, end_ms: int) -> torch.Tensor:
    return torch.nonzero(
        (dataset.market_start_ms >= start_ms) & (dataset.market_start_ms < end_ms),
        as_tuple=False,
    ).flatten()


def _limit_price(
    probability: torch.Tensor,
    fee_rate: torch.Tensor,
    fee_exponent: torch.Tensor,
    minimum_edge: float,
) -> torch.Tensor:
    low = torch.zeros_like(probability)
    high = probability.clamp(0, 1)
    for _ in range(64):
        middle = (low + high) / 2
        edge = probability - middle - fee_rate * torch.pow(
            middle * (1 - middle), fee_exponent
        )
        low = torch.where(edge >= minimum_edge, middle, low)
        high = torch.where(edge >= minimum_edge, high, middle)
    return low


def _orders(
    fold: Fold,
    variant: str,
    validation: ChainDataset,
    evaluation: SignalEvaluation,
    config: PracticalChainConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    order_checkpoint = evaluation.order_checkpoint.detach().cpu()
    for market_index, condition_id in enumerate(validation.condition_ids):
        selected = int(order_checkpoint[market_index].item())
        for checkpoint, seconds in enumerate(config.state_seconds_before_end):
            side = int(evaluation.side[market_index, checkpoint].item())
            decision_ms = int(validation.market_end_ms[market_index].item()) - seconds * 1_000
            decisions.append(
                {
                    "variant": variant,
                    "fold_id": fold.fold_id,
                    "condition_id": condition_id,
                    "market_start_utc": datetime.fromtimestamp(
                        int(validation.market_start_ms[market_index].item()) / 1_000,
                        tz=UTC,
                    ).isoformat(),
                    "checkpoint": checkpoint,
                    "seconds_before_end": seconds,
                    "decision_utc": datetime.fromtimestamp(
                        decision_ms / 1_000, tz=UTC
                    ).isoformat(),
                    "snapshot_valid": bool(validation.valid[market_index, checkpoint].item()),
                    "state": int(evaluation.states[market_index, checkpoint].item()),
                    "forecast_available": bool(
                        evaluation.forecast_available[market_index, checkpoint].item()
                    ),
                    "mid_up": float(validation.mid_up[market_index, checkpoint].item()),
                    "probability_up": float(
                        evaluation.probability_up[market_index, checkpoint].item()
                    ),
                    "side": "Up" if side == 0 else "Down",
                    "probability_side": float(
                        evaluation.probability_side[market_index, checkpoint].item()
                    ),
                    "ask": float(evaluation.ask[market_index, checkpoint].item()),
                    "net_edge": float(evaluation.edge[market_index, checkpoint].item()),
                    "signal": bool(evaluation.signal[market_index, checkpoint].item()),
                    "selected_first_order": checkpoint == selected,
                }
            )
        if selected < 0:
            continue
        side = int(evaluation.side[market_index, selected].item())
        probability = evaluation.probability_side[market_index, selected]
        fee_rate = validation.fee_rate[market_index]
        fee_exponent = validation.fee_exponent[market_index]
        price_limit = _limit_price(
            probability.reshape(1),
            fee_rate.reshape(1),
            fee_exponent.reshape(1),
            config.minimum_net_edge,
        )[0]
        decision_ms = int(validation.market_end_ms[market_index].item()) - (
            config.state_seconds_before_end[selected] * 1_000
        )
        order_rows.append(
            {
                "variant": variant,
                "fold_id": fold.fold_id,
                "condition_id": condition_id,
                "market_start_ms": int(validation.market_start_ms[market_index].item()),
                "market_end_ms": int(validation.market_end_ms[market_index].item()),
                "outcome_up": float(validation.outcome_up[market_index].item()),
                "fee_rate": float(fee_rate.item()),
                "fee_exponent": float(fee_exponent.item()),
                "token_id": validation.token_up[market_index]
                if side == 0
                else validation.token_down[market_index],
                "other_token_id": validation.token_down[market_index]
                if side == 0
                else validation.token_up[market_index],
                "side": "Up" if side == 0 else "Down",
                "checkpoint": selected,
                "seconds_before_end": config.state_seconds_before_end[selected],
                "decision_ms": decision_ms,
                "arrival_ms": decision_ms + config.execution_latency_ms,
                "probability_up": float(
                    evaluation.probability_up[market_index, selected].item()
                ),
                "probability_side": float(probability.item()),
                "decision_ask": float(evaluation.ask[market_index, selected].item()),
                "decision_edge": float(evaluation.edge[market_index, selected].item()),
                "price_limit": float(price_limit.item()),
            }
        )
    return decisions, order_rows


SNAPSHOT_QUERY = """
WITH snapshots AS (
    SELECT
        r.condition_id,
        r.variant,
        r.asset_id,
        r.arrival_ms,
        epoch_ms(p.timestamp_received) AS snapshot_receive_ms,
        epoch_ms(p.timestamp) AS snapshot_source_ms,
        p.file_row_number AS snapshot_source_row,
        p.asset_id AS snapshot_asset_id,
        p.asset_id = r.asset_id AS is_direct,
        CASE WHEN p.asset_id = r.asset_id THEN p.asks ELSE p.bids END AS levels
    FROM requested_execution AS r
    JOIN read_parquet(?, file_row_number=true) AS p
      ON p.market = r.market
     AND p.asset_id IN (r.asset_id, r.other_asset_id)
    WHERE p.event_type = 'book'
      AND epoch_ms(p.timestamp_received) <= r.arrival_ms
    QUALIFY row_number() OVER (
        PARTITION BY r.condition_id, r.variant
        ORDER BY (p.asset_id = r.asset_id) DESC,
                 p.timestamp_received DESC, p.timestamp DESC,
                 p.file_row_number DESC
    ) = 1
)
SELECT * FROM snapshots ORDER BY condition_id, variant
"""


CHANGES_QUERY = """
SELECT
    s.condition_id,
    s.variant,
    CASE WHEN s.is_direct THEN CAST(p.price AS DOUBLE)
         ELSE 1 - CAST(p.price AS DOUBLE)
    END AS price,
    arg_max(CAST(p.size AS DOUBLE), struct_pack(
        r := epoch_ms(p.timestamp_received),
        s := epoch_ms(p.timestamp),
        n := p.file_row_number
    )) AS size
FROM snapshots_execution AS s
JOIN read_parquet(?, file_row_number=true) AS p
  ON p.asset_id = s.snapshot_asset_id
WHERE p.event_type = 'price_change'
  AND upper(p.side) = CASE WHEN s.is_direct THEN 'SELL' ELSE 'BUY' END
  AND epoch_ms(p.timestamp_received) <= s.arrival_ms
  AND struct_pack(
      r := epoch_ms(p.timestamp_received),
      s := epoch_ms(p.timestamp),
      n := p.file_row_number
  ) > struct_pack(
      r := s.snapshot_receive_ms,
      s := s.snapshot_source_ms,
      n := s.snapshot_source_row
  )
GROUP BY s.condition_id, s.variant,
         CASE WHEN s.is_direct THEN CAST(p.price AS DOUBLE)
              ELSE 1 - CAST(p.price AS DOUBLE)
         END
ORDER BY condition_id, variant, price
"""


def _rebuild_depths(
    snapshots: pa.Table, changes: pa.Table
) -> dict[tuple[str, str], tuple[list[float], list[float]]]:
    latest_changes: dict[tuple[str, str], dict[float, float]] = defaultdict(dict)
    for row in changes.to_pylist():
        latest_changes[(str(row["condition_id"]), str(row["variant"]))][
            float(row["price"])
        ] = float(row["size"])
    result: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
    for row in snapshots.to_pylist():
        key = (str(row["condition_id"]), str(row["variant"]))
        levels: dict[float, float] = {}
        decoded = json.loads(row["levels"] or "[]")
        for price_raw, size_raw in decoded:
            price = float(price_raw)
            if not bool(row["is_direct"]):
                price = 1 - price
            levels[price] = float(size_raw)
        levels.update(latest_changes.get(key, {}))
        ordered = sorted((price, size) for price, size in levels.items() if size > 0)
        result[key] = (
            [item[0] for item in ordered],
            [item[1] for item in ordered],
        )
    return result


def _execution_cache_record(
    output_path: Path,
    metadata_path: Path,
    hour: str,
    orders: list[dict[str, Any]],
    config: PracticalChainConfig,
) -> dict[str, Any] | None:
    if not output_path.is_file() or not metadata_path.is_file():
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if (
        metadata.get("config_sha256") == config.config_sha256
        and metadata.get("hour") == hour
        and metadata.get("order_count") == len(orders)
        and metadata.get("orders_sha256") == _orders_sha256(orders)
        and metadata.get("parquet_sha256") == _sha256(output_path)
    ):
        return metadata
    return None


def _orders_sha256(orders: list[dict[str, Any]]) -> str:
    identity = [
        {
            "condition_id": order["condition_id"],
            "variant": order["variant"],
            "token_id": order["token_id"],
            "other_token_id": order["other_token_id"],
            "arrival_ms": order["arrival_ms"],
        }
        for order in sorted(
            orders, key=lambda item: (item["condition_id"], item["variant"])
        )
    ]
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _execution_depth_hour(
    hour: str,
    orders: list[dict[str, Any]],
    config: PracticalChainConfig,
) -> dict[tuple[str, str], tuple[list[float], list[float]]]:
    cache_dir = Path(config.cache_root) / "execution_hours"
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path = cache_dir / f"hour={hour}.parquet"
    metadata_path = cache_dir / f"hour={hour}.json"
    if _execution_cache_record(output_path, metadata_path, hour, orders, config):
        table = pq.read_table(output_path)
        return {
            (str(row["condition_id"]), str(row["variant"])): (
                [float(value) for value in (row["ask_prices"] or [])],
                [float(value) for value in (row["ask_sizes"] or [])],
            )
            for row in table.to_pylist()
        }
    archive = load_pmxt_archive_config(config.pmxt_config)
    source_url = archive.object_url(hour)
    requested = pa.Table.from_pylist(
        [
            {
                "condition_id": order["condition_id"],
                "variant": order["variant"],
                "market": order["condition_id"].encode(),
                "asset_id": order["token_id"],
                "other_asset_id": order["other_token_id"],
                "arrival_ms": order["arrival_ms"],
            }
            for order in orders
        ]
    )
    error: Exception | None = None
    for attempt in range(8):
        connection = duckdb.connect()
        try:
            connection.register("requested_execution", requested)
            snapshots = connection.execute(
                SNAPSHOT_QUERY, [source_url]
            ).fetch_arrow_table()
            connection.register("snapshots_execution", snapshots)
            changes = connection.execute(CHANGES_QUERY, [source_url]).fetch_arrow_table()
            break
        except (duckdb.Error, OSError, RuntimeError) as exc:
            error = exc
            if attempt < 7:
                time.sleep(min(30, 2**attempt))
        finally:
            connection.close()
    else:
        raise PracticalChainError(f"PMXT execution query failed for {hour}: {error}")
    depths = _rebuild_depths(snapshots, changes)
    rows = [
        {
            "condition_id": condition_id,
            "variant": variant,
            "ask_prices": values[0],
            "ask_sizes": values[1],
        }
        for (condition_id, variant), values in sorted(depths.items())
    ]
    temporary = output_path.with_suffix(".parquet.part")
    pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
    os.replace(temporary, output_path)
    _write_json(
        metadata_path,
        {
            "schema_version": 1,
            "config_sha256": config.config_sha256,
            "hour": hour,
            "source_url": source_url,
            "order_count": len(orders),
            "orders_sha256": _orders_sha256(orders),
            "depth_count": len(rows),
            "snapshot_count": snapshots.num_rows,
            "change_count": changes.num_rows,
            "parquet_sha256": _sha256(output_path),
        },
    )
    return depths


def _fetch_execution_depths(
    orders: list[dict[str, Any]], config: PracticalChainConfig
) -> dict[tuple[str, str], tuple[list[float], list[float]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        grouped[_hour_key(int(order["market_start_ms"]))].append(order)
    result: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
    print(
        f"Stage 4h execution replay: {len(orders)} orders / {len(grouped)} hours",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=config.pmxt_workers) as executor:
        iterator = iter(sorted(grouped.items()))
        futures = {}
        for _ in range(config.pmxt_workers):
            try:
                hour, rows = next(iterator)
            except StopIteration:
                break
            futures[executor.submit(_execution_depth_hour, hour, rows, config)] = hour
        completed = 0
        while futures:
            future = next(as_completed(futures))
            result.update(future.result())
            del futures[future]
            completed += 1
            if completed == 1 or completed % 24 == 0 or completed == len(grouped):
                print(
                    f"Stage 4h execution progress: {completed}/{len(grouped)}",
                    flush=True,
                )
            try:
                hour, rows = next(iterator)
            except StopIteration:
                continue
            futures[executor.submit(_execution_depth_hour, hour, rows, config)] = hour
    return result


def apply_execution(
    orders: list[dict[str, Any]],
    depths: dict[tuple[str, str], tuple[list[float], list[float]]],
    config: PracticalChainConfig,
    device: torch.device,
) -> list[dict[str, Any]]:
    maximum_levels = max(
        (len(value[0]) for value in depths.values()), default=0
    )
    maximum_levels = max(maximum_levels, 1)
    count = len(orders)
    prices = torch.full(
        (count, 1, maximum_levels), torch.nan, dtype=torch.float64, device=device
    )
    sizes = torch.zeros_like(prices)
    for index, order in enumerate(orders):
        level_prices, level_sizes = depths.get(
            (order["condition_id"], order["variant"]), ([], [])
        )
        if level_prices:
            prices[index, 0, : len(level_prices)] = torch.tensor(
                level_prices, dtype=torch.float64, device=device
            )
            sizes[index, 0, : len(level_sizes)] = torch.tensor(
                level_sizes, dtype=torch.float64, device=device
            )
    limits = torch.tensor(
        [order["price_limit"] for order in orders], dtype=torch.float64, device=device
    )
    usable = (
        torch.isfinite(prices)
        & (prices > 0)
        & (prices <= limits[:, None, None] + 1e-12)
        & (sizes > 0)
    )
    safe_prices = torch.where(usable, prices, torch.ones_like(prices))
    safe_sizes = torch.where(usable, sizes, torch.zeros_like(sizes))
    level_notional = safe_prices * safe_sizes
    before = torch.cumsum(level_notional, dim=2) - level_notional
    remaining = torch.clamp(config.target_notional_usdc - before, min=0)
    taken = torch.minimum(safe_sizes, remaining / safe_prices)
    shares = taken.sum(dim=2).squeeze(1)
    cost = (taken * safe_prices).sum(dim=2).squeeze(1)
    fee_rates = torch.tensor(
        [order["fee_rate"] for order in orders], dtype=torch.float64, device=device
    )
    fee_exponents = torch.tensor(
        [order["fee_exponent"] for order in orders],
        dtype=torch.float64,
        device=device,
    )
    fees = calculate_platform_fee(
        taken,
        safe_prices,
        fee_rates,
        decimals=config.platform_fee_round_decimals,
        exponent=fee_exponents,
    ).squeeze(1)
    filled = cost >= config.minimum_fill_notional_usdc
    shares = torch.where(filled, shares, torch.zeros_like(shares))
    cost = torch.where(filled, cost, torch.zeros_like(cost))
    fees = torch.where(filled, fees, torch.zeros_like(fees))
    probabilities = torch.tensor(
        [order["probability_side"] for order in orders],
        dtype=torch.float64,
        device=device,
    )
    if bool(
        (
            taken
            * (
                probabilities[:, None, None]
                - safe_prices
                - fee_rates[:, None, None]
                * torch.pow(
                    safe_prices * (1 - safe_prices),
                    fee_exponents[:, None, None],
                )
                - config.minimum_net_edge
            )
            < -1e-10
        ).any().item()
    ):
        raise PracticalChainError("execution used a level below frozen net edge")
    settlement = torch.tensor(
        [
            order["outcome_up"]
            if order["side"] == "Up"
            else 1 - order["outcome_up"]
            for order in orders
        ],
        dtype=torch.float64,
        device=device,
    )
    primary = torch.where(filled, shares * settlement - cost - fees, 0)
    stress = primary - torch.where(
        filled, shares * config.stress_extra_cost_per_share, 0
    )
    vwap = torch.where(filled, cost / shares, torch.full_like(cost, torch.nan))
    rows: list[dict[str, Any]] = []
    for index, order in enumerate(orders):
        row = dict(order)
        row.update(
            {
                "decision_utc": datetime.fromtimestamp(
                    order["decision_ms"] / 1_000, tz=UTC
                ).isoformat(),
                "arrival_utc": datetime.fromtimestamp(
                    order["arrival_ms"] / 1_000, tz=UTC
                ).isoformat(),
                "depth_levels": len(
                    depths.get((order["condition_id"], order["variant"]), ([], []))[0]
                ),
                "filled": bool(filled[index].item()),
                "fill_shares": float(shares[index].item()),
                "fill_cost": float(cost[index].item()),
                "fill_vwap": float(vwap[index].item()),
                "platform_fee": float(fees[index].item()),
                "primary_pnl": float(primary[index].item()),
                "stress_pnl": float(stress[index].item()),
            }
        )
        rows.append(row)
    return rows


def _profit_factor(values: torch.Tensor) -> tuple[float | None, bool]:
    positive = values[values > 0].sum()
    negative = -values[values < 0].sum()
    if float(negative.item()) == 0:
        return (None, bool(positive > 0))
    return float((positive / negative).item()), False


def _metrics(
    decisions: list[dict[str, Any]], orders: list[dict[str, Any]]
) -> dict[str, Any]:
    pnl = torch.tensor([row["primary_pnl"] for row in orders], dtype=torch.float64)
    stress = torch.tensor([row["stress_pnl"] for row in orders], dtype=torch.float64)
    filled = torch.tensor([row["filled"] for row in orders], dtype=torch.bool)
    filled_pnl = pnl[filled]
    filled_stress = stress[filled]
    cumulative = torch.cumsum(pnl, dim=0)
    peaks = torch.cummax(
        torch.cat((torch.zeros(1, dtype=torch.float64), cumulative)), dim=0
    ).values[1:]
    pf, pf_infinite = _profit_factor(filled_pnl)
    stress_pf, stress_pf_infinite = _profit_factor(filled_stress)
    forecast = torch.tensor(
        [row["probability_up"] for row in decisions if row["forecast_available"]],
        dtype=torch.float64,
    )
    outcomes_by_market = {row["condition_id"]: row["outcome_up"] for row in orders}
    # Orders omit no-signal markets; derive outcomes from the first checkpoint rows later.
    return {
        "markets": len({row["condition_id"] for row in decisions}),
        "decision_checkpoints": len(decisions),
        "forecast_available": sum(bool(row["forecast_available"]) for row in decisions),
        "signals": sum(bool(row["signal"]) for row in decisions),
        "orders": len(orders),
        "fills": int(filled.sum().item()),
        "net_pnl_usdc": float(filled_pnl.sum().item()),
        "stress_net_pnl_usdc": float(filled_stress.sum().item()),
        "cash_turnover_usdc": sum(row["fill_cost"] + row["platform_fee"] for row in orders),
        "return_on_turnover": float(filled_pnl.sum().item())
        / sum(row["fill_cost"] + row["platform_fee"] for row in orders)
        if any(row["filled"] for row in orders)
        else None,
        "profit_factor": pf,
        "profit_factor_infinite": pf_infinite,
        "stress_profit_factor": stress_pf,
        "stress_profit_factor_infinite": stress_pf_infinite,
        "max_drawdown_usdc": float((peaks - cumulative).max().item())
        if cumulative.numel()
        else 0.0,
        "mean_forecast": float(forecast.mean().item()) if forecast.numel() else None,
        "_outcomes_by_market": outcomes_by_market,
    }


def _forecast_metrics(
    decisions: list[dict[str, Any]], outcomes: dict[str, float]
) -> dict[str, float | int | None]:
    rows = [row for row in decisions if row["forecast_available"]]
    if not rows:
        return {"brier_score": None, "calibration_bias": None, "observations": 0}
    forecast = torch.tensor([row["probability_up"] for row in rows], dtype=torch.float64)
    labels = torch.tensor([outcomes[row["condition_id"]] for row in rows], dtype=torch.float64)
    error = forecast - labels
    return {
        "brier_score": float(error.square().mean().item()),
        "calibration_bias": float(error.mean().item()),
        "observations": len(rows),
    }


def _model_record(fold: Fold, model: RawChainModel, train_markets: int) -> dict[str, Any]:
    return {
        "fold_id": fold.fold_id,
        "train_markets": train_markets,
        "transition_counts": model.transition_counts.detach().cpu().tolist(),
        "transition_support": model.transition_support.detach().cpu().tolist(),
        "transition_matrix": model.transition_matrix.detach().cpu().tolist(),
        "terminal_counts": model.terminal_counts.detach().cpu().tolist(),
        "terminal_support": model.terminal_support.detach().cpu().tolist(),
        "terminal_up": model.terminal_up.detach().cpu().tolist(),
        "available": model.available.detach().cpu().tolist(),
        "value_up": model.value_up.detach().cpu().tolist(),
    }


def _render_chart(path: Path, variant: str, orders: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    ordered = sorted(orders, key=lambda row: (row["decision_ms"], row["condition_id"]))
    times = [row["decision_utc"] for row in ordered]
    pnl = torch.tensor([row["primary_pnl"] for row in ordered], dtype=torch.float64)
    cumulative = torch.cumsum(pnl, dim=0)
    peaks = torch.cummax(
        torch.cat((torch.zeros(1, dtype=torch.float64), cumulative)), dim=0
    ).values[1:]
    drawdown = peaks - cumulative
    colors = ["#1f77b4" if row["side"] == "Up" else "#ff7f0e" for row in ordered]
    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        specs=[[{}], [{}], [{"secondary_y": True}], [{"secondary_y": True}]],
        subplot_titles=(
            "Terminal forecast, decision ask and edge",
            "Orders and fills",
            "Position notional and share of 10 USDC target",
            "Cumulative PnL and drawdown",
        ),
    )
    for name, key in (
        ("terminal p", "probability_side"),
        ("decision ask", "decision_ask"),
        ("net edge", "decision_edge"),
    ):
        figure.add_trace(
            go.Scatter(x=times, y=[row[key] for row in ordered], name=name),
            row=1,
            col=1,
        )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=[row["decision_ask"] for row in ordered],
            mode="markers",
            marker={
                "color": colors,
                "symbol": ["diamond" if row["filled"] else "x" for row in ordered],
                "size": 8,
            },
            text=[f"{row['side']} / {row['seconds_before_end']}s" for row in ordered],
            name="order (diamond=fill)",
        ),
        row=2,
        col=1,
    )
    notionals = [row["fill_cost"] + row["platform_fee"] for row in ordered]
    figure.add_trace(
        go.Bar(x=times, y=notionals, marker_color=colors, name="filled notional USDC"),
        row=3,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=[100 * value / 10 for value in notionals],
            name="% of 10 USDC target",
        ),
        row=3,
        col=1,
        secondary_y=True,
    )
    figure.add_trace(
        go.Scatter(x=times, y=cumulative.tolist(), name="cumulative PnL"),
        row=4,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=drawdown.tolist(),
            fill="tozeroy",
            line={"color": "firebrick"},
            name="drawdown",
        ),
        row=4,
        col=1,
        secondary_y=True,
    )
    figure.update_layout(
        title=(
            f"Stage 4h {variant}: fills {summary['fills']}, "
            f"PnL {summary['net_pnl_usdc']:.2f} USDC"
        ),
        template="plotly_white",
        height=1_180,
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.02},
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def _render_models(path: Path, models: list[dict[str, Any]]) -> None:
    figure = make_subplots(
        rows=len(models),
        cols=2,
        subplot_titles=tuple(
            title
            for record in models
            for title in (f"{record['fold_id']} A(-120)", f"{record['fold_id']} p(UP|-15)")
        ),
    )
    for row_index, record in enumerate(models, start=1):
        figure.add_trace(
            go.Heatmap(
                z=record["transition_matrix"][0],
                zmin=0,
                zmax=1,
                colorscale="Blues",
                showscale=False,
                text=record["transition_matrix"][0],
                texttemplate="%{text:.2f}",
            ),
            row=row_index,
            col=1,
        )
        figure.add_trace(
            go.Bar(x=list(range(8)), y=record["terminal_up"], showlegend=False),
            row=row_index,
            col=2,
        )
    figure.update_layout(
        title="Stage 4h raw unsmoothed chain diagnostics",
        template="plotly_white",
        height=330 * len(models),
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def _git(*args: str) -> str:
    return subprocess.run(
        ("git", *args), check=True, capture_output=True, text=True
    ).stdout.strip()


def run_stage4h_practical_chain(
    config_path: str | Path,
    output_root: str | Path = "outputs/practical_chain",
) -> Path:
    started = time.perf_counter()
    config = load_practical_chain_config(config_path)
    torch.manual_seed(config.seed)
    if config.device == "cuda" and not torch.cuda.is_available():
        raise PracticalChainError("Stage 4h frozen CUDA device is unavailable")
    device = torch.device(config.device)
    manifest_path = build_practical_chain_cache(config_path)
    dataset = load_practical_chain_dataset(config)
    if int(dataset.market_start_ms.max().item()) >= config.development_end_ms:
        raise PracticalChainError("Stage 4h loaded reserved holdout")
    edges = torch.tensor(config.state_bin_edges, dtype=torch.float64, device=device)
    all_decisions: list[dict[str, Any]] = []
    all_orders: list[dict[str, Any]] = []
    model_records: list[dict[str, Any]] = []
    fold_context: dict[tuple[str, str], tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    outcomes: dict[str, float] = {}
    for fold in _folds(config):
        train_indices = _indices(dataset, fold.train_start_ms, fold.train_end_ms)
        validation_indices = _indices(
            dataset, fold.validation_start_ms, fold.validation_end_ms
        )
        if train_indices.numel() == 0 or validation_indices.numel() == 0:
            raise PracticalChainError(f"Stage 4h fold is empty: {fold.fold_id}")
        if int(dataset.market_start_ms[train_indices].max().item()) >= int(
            dataset.market_start_ms[validation_indices].min().item()
        ):
            raise PracticalChainError("Stage 4h train/validation chronology overlaps")
        train = dataset.index(train_indices).to(device)
        validation = dataset.index(validation_indices).to(device)
        model = fit_raw_chain(
            train.mid_up,
            train.valid,
            train.outcome_up,
            edges,
            minimum_transition_support=config.minimum_transition_support,
            minimum_terminal_support=config.minimum_terminal_support,
        )
        model_records.append(_model_record(fold, model, len(train)))
        outcomes.update(
            zip(
                validation.condition_ids,
                validation.outcome_up.detach().cpu().tolist(),
                strict=True,
            )
        )
        for variant, fitted in (("practical_chain", model), ("market_midpoint", None)):
            evaluation = evaluate_signals(
                validation, fitted, minimum_net_edge=config.minimum_net_edge
            )
            decisions, orders = _orders(
                fold, variant, validation, evaluation, config
            )
            all_decisions.extend(decisions)
            all_orders.extend(orders)
            fold_context[(variant, fold.fold_id)] = (decisions, orders)
    depths = _fetch_execution_depths(all_orders, config)
    executed = apply_execution(all_orders, depths, config, device)
    execution_by_key = {
        (row["variant"], row["fold_id"], row["condition_id"]): row
        for row in executed
    }
    results: dict[str, Any] = {}
    fold_metrics: list[dict[str, Any]] = []
    for variant in ("practical_chain", "market_midpoint"):
        variant_decisions = [row for row in all_decisions if row["variant"] == variant]
        variant_orders = [row for row in executed if row["variant"] == variant]
        summary = _metrics(variant_decisions, variant_orders)
        summary.pop("_outcomes_by_market", None)
        summary.update(_forecast_metrics(variant_decisions, outcomes))
        positive_folds = 0
        for fold in _folds(config):
            decisions, raw_orders = fold_context[(variant, fold.fold_id)]
            orders = [
                execution_by_key[(variant, fold.fold_id, row["condition_id"])]
                for row in raw_orders
            ]
            metric = _metrics(decisions, orders)
            metric.pop("_outcomes_by_market", None)
            metric.update(_forecast_metrics(decisions, outcomes))
            metric.update({"variant": variant, "fold_id": fold.fold_id})
            positive_folds += int(metric["net_pnl_usdc"] > 0)
            fold_metrics.append(metric)
        summary["positive_folds"] = positive_folds
        results[variant] = summary
    candidate = results["practical_chain"]
    pf_value = (
        math.inf
        if candidate["profit_factor_infinite"]
        else (candidate["profit_factor"] or 0.0)
    )
    gates = {
        "minimum_total_fills": candidate["fills"] >= config.minimum_total_fills,
        "positive_primary_pnl": candidate["net_pnl_usdc"] > 0,
        "positive_stress_pnl": candidate["stress_net_pnl_usdc"] > 0,
        "minimum_positive_folds": candidate["positive_folds"]
        >= config.minimum_positive_folds,
        "minimum_profit_factor": pf_value > config.minimum_profit_factor,
    }
    if candidate["fills"] < config.minimum_total_fills:
        status = "insufficient_trades"
    elif all(gates.values()):
        status = "development_candidate"
    else:
        status = "rejected"
    run_id = (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + config.experiment_id
    )
    run_dir = Path(output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_csv(run_dir / "decisions.csv", all_decisions)
    _write_csv(run_dir / "orders.csv", executed)
    _write_csv(run_dir / "fold_metrics.csv", fold_metrics)
    _write_json(run_dir / "models.json", model_records)
    _write_json(run_dir / "summary.json", {"status": status, "gates": gates, "variants": results})
    _render_chart(
        run_dir / "practical_chain.html",
        "practical_chain",
        [row for row in executed if row["variant"] == "practical_chain"],
        results["practical_chain"],
    )
    _render_chart(
        run_dir / "market_midpoint.html",
        "market_midpoint",
        [row for row in executed if row["variant"] == "market_midpoint"],
        results["market_midpoint"],
    )
    _render_models(run_dir / "chain_matrices.html", model_records)
    provenance = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "created_at": datetime.now(UTC).isoformat(),
        "runtime_seconds": time.perf_counter() - started,
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "config_path": str(config_path),
        "config_sha256": config.config_sha256,
        "top_cache_manifest": str(manifest_path),
        "top_cache_manifest_sha256": _sha256(manifest_path),
        "universe_sha256": _sha256(config.universe_path),
        "seed": config.seed,
        "holdout_rows_read": 0,
        "python": platform.python_version(),
        "torch": torch.__version__,
        "plotly": plotly.__version__,
        "device": str(device),
        "device_name": torch.cuda.get_device_name(device),
        "dtype": config.dtype,
        "artifacts": {},
    }
    for path in sorted(run_dir.iterdir()):
        if path.name != "provenance.json":
            provenance["artifacts"][path.name] = {
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
    _write_json(run_dir / "provenance.json", provenance)
    return run_dir
