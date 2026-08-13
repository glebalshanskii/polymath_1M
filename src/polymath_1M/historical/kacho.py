from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import torch

from polymath_1M.domain import DecisionBatch

from .config import KachoDatasetConfig
from .download import validate_kacho_files


class KachoDataError(ValueError):
    """The pinned Kacho release does not satisfy the adapter contract."""


MARKET_COLUMNS = (
    "condition_id",
    "market_start",
    "market_end",
    "outcome",
    "n_ticks",
)
TICK_COLUMNS = (
    "condition_id",
    "t",
    "bu",
    "au",
    "bd",
    "ad",
    "sau",
    "sad",
)


def _require_columns(schema: pa.Schema, columns: tuple[str, ...], source: Path) -> None:
    missing = set(columns) - set(schema.names)
    if missing:
        raise KachoDataError(f"{source} is missing columns: {sorted(missing)}")


def _epoch_seconds(values: pa.ChunkedArray) -> torch.Tensor:
    python_values = values.to_pylist()
    if any(value is None for value in python_values):
        raise KachoDataError("market timestamps contain null values")
    return torch.tensor(
        [int(value.timestamp()) for value in python_values], dtype=torch.int64
    )


def _float_tensor(values: pa.ChunkedArray) -> torch.Tensor:
    array = values.combine_chunks().to_numpy(zero_copy_only=False)
    return torch.tensor(array, dtype=torch.float64)


def _load_markets(
    config: KachoDatasetConfig,
    dataset_dir: Path,
    assets: tuple[str, ...],
    max_markets: int,
    period_start_s: int | None,
    period_end_exclusive_s: int | None,
    require_inferred_labels: bool,
) -> pa.Table:
    tables: list[pa.Table] = []
    specs = {(item.asset, item.kind): item for item in config.select(assets)}
    for asset in assets:
        path = dataset_dir / specs[(asset, "markets")].path
        schema = pq.read_schema(path)
        _require_columns(schema, MARKET_COLUMNS, path)
        table = pq.read_table(path, columns=list(MARKET_COLUMNS))
        table = table.append_column("asset", pa.array([asset] * table.num_rows))
        tables.append(table)
    combined = pa.concat_tables(tables)
    if require_inferred_labels:
        label_mask = pc.is_in(
            combined["outcome"], value_set=pa.array(["Up", "Down"])
        )
        combined = combined.filter(label_mask)
    if period_start_s is not None:
        combined = combined.filter(
            pc.greater_equal(
                combined["market_start"],
                pa.scalar(datetime.fromtimestamp(period_start_s, tz=UTC)),
            )
        )
    if period_end_exclusive_s is not None:
        combined = combined.filter(
            pc.less(
                combined["market_start"],
                pa.scalar(datetime.fromtimestamp(period_end_exclusive_s, tz=UTC)),
            )
        )
    order = pc.sort_indices(
        combined,
        sort_keys=[("market_start", "ascending"), ("condition_id", "ascending")],
    )
    combined = combined.take(order)
    if max_markets > 0:
        combined = combined.slice(0, max_markets)
    condition_ids = combined["condition_id"].to_pylist()
    if len(condition_ids) != len(set(condition_ids)):
        raise KachoDataError("selected market condition IDs are not unique")
    if combined.num_rows < 5:
        raise KachoDataError("fewer than five labeled markets are available")
    return combined


def _load_selected_ticks(
    config: KachoDatasetConfig,
    dataset_dir: Path,
    markets: pa.Table,
    assets: tuple[str, ...],
) -> pa.Table:
    specs = {(item.asset, item.kind): item for item in config.select(assets)}
    chunks: list[pa.Table] = []
    market_assets = markets["asset"].to_pylist()
    market_ids = markets["condition_id"].to_pylist()
    for asset in assets:
        selected_ids = [
            condition_id
            for condition_id, market_asset in zip(
                market_ids, market_assets, strict=True
            )
            if market_asset == asset
        ]
        if not selected_ids:
            continue
        path = dataset_dir / specs[(asset, "ticks")].path
        dataset = ds.dataset(path, format="parquet")
        _require_columns(dataset.schema, TICK_COLUMNS, path)
        chunks.append(
            dataset.to_table(
                columns=list(TICK_COLUMNS),
                filter=ds.field("condition_id").isin(selected_ids),
            )
        )
    if not chunks:
        raise KachoDataError("no tick rows matched the selected markets")
    return pa.concat_tables(chunks)


def _snapshot_at(
    ticks: pa.Table,
    condition_ids: list[str],
    timestamps: torch.Tensor,
) -> pa.Table:
    selection = pa.table(
        {
            "market_index": pa.array(range(len(condition_ids)), type=pa.int64()),
            "condition_id": pa.array(condition_ids),
            "t": pa.array(timestamps.tolist(), type=pa.int64()),
        }
    )
    joined = selection.join(
        ticks,
        keys=["condition_id", "t"],
        join_type="left outer",
    )
    if joined.num_rows != len(condition_ids):
        raise KachoDataError("duplicate Kacho rows exist for a condition/timestamp key")
    return joined.take(
        pc.sort_indices(joined, sort_keys=[("market_index", "ascending")])
    )


def load_kacho_decision_batch(
    config: KachoDatasetConfig,
    data_root: str | Path,
    *,
    assets: tuple[str, ...] | list[str],
    max_markets: int,
    decision_seconds_before_end: int,
    transition_horizon_seconds: int,
    label_policy: str,
    execution_latency_seconds: int = 0,
    period_start_s: int | None = None,
    period_end_exclusive_s: int | None = None,
) -> DecisionBatch:
    supported_label_policies = {
        "kacho_inferred_development_only",
        "external_authoritative_labels",
    }
    if label_policy not in supported_label_policies:
        raise KachoDataError(
            f"unsupported Kacho label policy: {label_policy}"
        )
    normalized_assets = tuple(asset.upper() for asset in assets)
    dataset_dir, _ = validate_kacho_files(config, data_root, assets=normalized_assets)
    if execution_latency_seconds < 0:
        raise KachoDataError("execution latency must be nonnegative")
    markets = _load_markets(
        config,
        dataset_dir,
        normalized_assets,
        max_markets,
        period_start_s,
        period_end_exclusive_s,
        label_policy == "kacho_inferred_development_only",
    )
    condition_ids = markets["condition_id"].to_pylist()
    market_end_s = _epoch_seconds(markets["market_end"])
    market_start_s = _epoch_seconds(markets["market_start"])
    decision_s = market_end_s - decision_seconds_before_end
    previous_s = decision_s - transition_horizon_seconds
    ticks = _load_selected_ticks(config, dataset_dir, markets, normalized_assets)
    current = _snapshot_at(ticks, condition_ids, decision_s)
    previous = _snapshot_at(ticks, condition_ids, previous_s)
    execution = _snapshot_at(
        ticks, condition_ids, decision_s + execution_latency_seconds
    )

    current_bid_up = _float_tensor(current["bu"])
    current_ask_up = _float_tensor(current["au"])
    current_bid_down = _float_tensor(current["bd"])
    current_ask_down = _float_tensor(current["ad"])
    previous_mid_up = (
        _float_tensor(previous["bu"]) + _float_tensor(previous["au"])
    ) / 2
    current_mid_up = (current_bid_up + current_ask_up) / 2
    bids = torch.stack((current_bid_up, current_bid_down), dim=1)
    asks = torch.stack((current_ask_up, current_ask_down), dim=1)
    current_mid = (bids + asks) / 2
    execution_asks = torch.stack(
        (_float_tensor(execution["au"]), _float_tensor(execution["ad"])), dim=1
    )
    ask_sizes = torch.stack(
        (_float_tensor(execution["sau"]), _float_tensor(execution["sad"])), dim=1
    )
    finite_book = torch.isfinite(bids).all(dim=1) & torch.isfinite(asks).all(dim=1)
    bounded_book = (
        (bids >= 0).all(dim=1)
        & (bids <= 1).all(dim=1)
        & (asks > 0).all(dim=1)
        & (asks <= 1).all(dim=1)
    )
    uncrossed = (bids <= asks).all(dim=1)
    snapshot_valid = (
        finite_book
        & bounded_book
        & uncrossed
        & torch.isfinite(previous_mid_up)
        & (previous_mid_up >= 0)
        & (previous_mid_up <= 1)
        & torch.isfinite(execution_asks).all(dim=1)
        & (execution_asks > 0).all(dim=1)
        & (execution_asks <= 1).all(dim=1)
    )
    if label_policy == "kacho_inferred_development_only":
        outcomes = torch.tensor(
            [
                1.0 if value == "Up" else 0.0
                for value in markets["outcome"].to_pylist()
            ],
            dtype=torch.float64,
        )
    else:
        outcomes = torch.full((markets.num_rows,), torch.nan, dtype=torch.float64)
    return DecisionBatch(
        condition_ids=tuple(condition_ids),
        assets=tuple(markets["asset"].to_pylist()),
        market_start_s=market_start_s,
        market_end_s=market_end_s,
        decision_s=decision_s,
        previous_s=previous_s,
        outcome_up=outcomes,
        n_ticks=torch.tensor(markets["n_ticks"].to_pylist(), dtype=torch.int64),
        previous_mid_up=previous_mid_up,
        current_mid_up=current_mid_up,
        current_mid=current_mid,
        bids=bids,
        asks=asks,
        ask_depth_prices=execution_asks.unsqueeze(2),
        ask_depth_sizes=ask_sizes.unsqueeze(2),
        snapshot_valid=snapshot_valid,
        label_source=label_policy,
    )
