from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import torch

from polymath_1M.collector.book import BookDataError, OrderBookStore, PRICE_SCALE


class PmxtDataError(ValueError):
    """PMXT data cannot be mapped to the Polymarket CLOB event contract."""


@dataclass(frozen=True)
class PmxtArchiveConfig:
    schema_version: int
    dataset_id: str
    license: str
    object_url_template: str
    coverage_start_hour: str
    audited_through_hour: str
    config_sha256: str

    def object_url(self, hour: str) -> str:
        parsed = datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=UTC)
        start = datetime.strptime(self.coverage_start_hour, "%Y-%m-%dT%H").replace(
            tzinfo=UTC
        )
        end = datetime.strptime(self.audited_through_hour, "%Y-%m-%dT%H").replace(
            tzinfo=UTC
        )
        if not start <= parsed <= end:
            raise PmxtDataError(f"PMXT hour {hour} is outside audited coverage")
        return self.object_url_template.format(hour=hour)


@dataclass(frozen=True)
class PmxtBookSnapshot:
    condition_id: str
    receive_timestamp_ms: int
    token_ids: tuple[str, str]
    bids: torch.Tensor
    asks: torch.Tensor
    ask_depth_prices: torch.Tensor
    ask_depth_sizes: torch.Tensor
    event_rows: int
    ignored_pre_snapshot_rows: int
    initial_snapshot_received_ms: int
    initialized: bool


def load_pmxt_archive_config(path: str | Path) -> PmxtArchiveConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "dataset_id",
        "license",
        "object_url_template",
        "coverage_start_hour",
        "audited_through_hour",
    }
    if payload.keys() != required:
        raise PmxtDataError(
            f"PMXT config fields differ: missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    if payload["schema_version"] != 1 or "{hour}" not in payload["object_url_template"]:
        raise PmxtDataError("unsupported PMXT config schema or URL template")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return PmxtArchiveConfig(
        schema_version=1,
        dataset_id=str(payload["dataset_id"]),
        license=str(payload["license"]),
        object_url_template=str(payload["object_url_template"]),
        coverage_start_hour=str(payload["coverage_start_hour"]),
        audited_through_hour=str(payload["audited_through_hour"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def query_pmxt_hour(
    config: PmxtArchiveConfig,
    hour: str,
    condition_ids: tuple[str, ...] | list[str],
) -> tuple[str, pa.Table]:
    if not condition_ids:
        raise PmxtDataError("at least one condition ID is required")
    normalized = tuple(str(value).lower() for value in condition_ids)
    if any(len(value) != 66 or not value.startswith("0x") for value in normalized):
        raise PmxtDataError("condition IDs must be 0x-prefixed 32-byte hex strings")
    url = config.object_url(hour)
    placeholders = ",".join("?" for _ in normalized)
    sql = f"""
        SELECT
            row_number() OVER () - 1 AS source_row,
            epoch_ms(timestamp_received) AS receive_timestamp_ms,
            epoch_ms(timestamp) AS source_timestamp_ms,
            decode(market) AS condition_id,
            event_type,
            asset_id,
            bids,
            asks,
            CAST(price AS DOUBLE) AS price,
            CAST(size AS DOUBLE) AS size,
            side,
            CAST(best_bid AS DOUBLE) AS best_bid,
            CAST(best_ask AS DOUBLE) AS best_ask,
            fee_rate_bps,
            transaction_hash,
            CAST(old_tick_size AS DOUBLE) AS old_tick_size,
            CAST(new_tick_size AS DOUBLE) AS new_tick_size
        FROM read_parquet(?)
        WHERE market IN ({placeholders})
        ORDER BY receive_timestamp_ms, source_timestamp_ms, source_row
    """
    parameters: list[Any] = [url, *(value.encode() for value in normalized)]
    connection = duckdb.connect()
    try:
        table = connection.execute(sql, parameters).fetch_arrow_table()
    finally:
        connection.close()
    if table.num_rows == 0:
        raise PmxtDataError(f"no PMXT rows for requested conditions in {hour}")
    returned = {str(value).lower() for value in table["condition_id"].to_pylist()}
    missing = set(normalized) - returned
    if missing:
        raise PmxtDataError(f"PMXT hour is missing conditions: {sorted(missing)}")
    return url, table


def _levels(raw: str | None) -> list[dict[str, str]]:
    if raw is None:
        return []
    decoded = json.loads(raw)
    if not isinstance(decoded, list):
        raise PmxtDataError("PMXT book levels are not a JSON array")
    result: list[dict[str, str]] = []
    for row in decoded:
        if not isinstance(row, list) or len(row) != 2:
            raise PmxtDataError("PMXT book level is not [price, size]")
        result.append({"price": str(row[0]), "size": str(row[1])})
    return result


def _apply_snapshot(store: OrderBookStore, row: dict[str, Any]) -> None:
    store.apply_raw(
        json.dumps(
            {
                "event_type": "book",
                "asset_id": row["asset_id"],
                "bids": _levels(row["bids"]),
                "asks": _levels(row["asks"]),
                "timestamp": row["source_timestamp_ms"],
            }
        )
    )


def _complement_levels(raw: str | None) -> list[dict[str, str]]:
    complement: list[dict[str, str]] = []
    for level in _levels(raw):
        price = Decimal(1) - Decimal(level["price"])
        complement.append({"price": format(price, "f"), "size": level["size"]})
    return complement


def _apply_binary_snapshot(
    store: OrderBookStore,
    row: dict[str, Any],
    token_ids: tuple[str, str],
) -> None:
    """Initialize both complementary outcome books from one CLOB snapshot."""

    _apply_snapshot(store, row)
    token_id = str(row["asset_id"])
    if token_id not in token_ids:
        raise PmxtDataError(f"snapshot token {token_id} is outside requested market")
    other_token = token_ids[1] if token_id == token_ids[0] else token_ids[0]
    store.apply_raw(
        json.dumps(
            {
                "event_type": "book",
                "asset_id": other_token,
                "bids": _complement_levels(row["asks"]),
                "asks": _complement_levels(row["bids"]),
                "timestamp": row["source_timestamp_ms"],
            }
        )
    )


def _apply_change_group(store: OrderBookStore, rows: list[dict[str, Any]]) -> None:
    changes = []
    for row in rows:
        change = {
            "asset_id": row["asset_id"],
            "price": format(Decimal(str(row["price"])), "f"),
            "size": format(Decimal(str(row["size"])), "f"),
            "side": row["side"],
        }
        if row["best_bid"] is not None:
            change["best_bid"] = format(Decimal(str(row["best_bid"])), "f")
        if row["best_ask"] is not None:
            change["best_ask"] = format(Decimal(str(row["best_ask"])), "f")
        changes.append(change)
    store.apply_raw(
        json.dumps(
            {
                "event_type": "price_change",
                "price_changes": changes,
                "timestamp": rows[-1]["source_timestamp_ms"],
            }
        )
    )


def replay_pmxt_book(
    table: pa.Table,
    *,
    condition_id: str,
    token_ids: tuple[str, str],
    decision_timestamp_ms: int,
) -> PmxtBookSnapshot:
    rows = sorted(
        [
            row
            for row in table.to_pylist()
            if str(row["condition_id"]).lower() == condition_id.lower()
            and int(row["receive_timestamp_ms"]) <= decision_timestamp_ms
        ],
        key=lambda row: (
            int(row["receive_timestamp_ms"]),
            int(row["source_timestamp_ms"]),
            int(row.get("source_row", 0)),
        ),
    )
    if not rows:
        raise PmxtDataError("no PMXT events are observable by the decision timestamp")
    store = OrderBookStore()
    index = 0
    ignored_pre_snapshot_rows = 0
    initial_snapshot_received_ms: int | None = None
    try:
        while index < len(rows):
            row = rows[index]
            event_type = row["event_type"]
            if event_type == "book":
                _apply_binary_snapshot(store, row, token_ids)
                if initial_snapshot_received_ms is None:
                    initial_snapshot_received_ms = int(row["receive_timestamp_ms"])
                index += 1
            elif event_type == "price_change":
                receive_timestamp = row["receive_timestamp_ms"]
                group: list[dict[str, Any]] = []
                while (
                    index < len(rows)
                    and rows[index]["event_type"] == "price_change"
                    and rows[index]["receive_timestamp_ms"] == receive_timestamp
                ):
                    group.append(rows[index])
                    index += 1
                if initial_snapshot_received_ms is None:
                    ignored_pre_snapshot_rows += len(group)
                else:
                    _apply_change_group(store, group)
            else:
                index += 1
    except (BookDataError, KeyError, TypeError, ValueError) as exc:
        raise PmxtDataError(f"PMXT book replay failed: {exc}") from exc

    if initial_snapshot_received_ms is None:
        raise PmxtDataError("no full-L2 snapshot exists by the decision timestamp")

    top_bids = torch.full((2,), torch.nan, dtype=torch.float64)
    top_asks = torch.full((2,), torch.nan, dtype=torch.float64)
    depth: list[tuple[torch.Tensor, torch.Tensor]] = []
    initialized = True
    for side_index, token_id in enumerate(token_ids):
        book = store.books.get(token_id)
        if book is None or not book.initialized:
            initialized = False
            depth.append((torch.empty(0), torch.empty(0)))
            continue
        best_bid, best_ask = book.best_prices()
        if best_bid is not None:
            top_bids[side_index] = best_bid / PRICE_SCALE
        if best_ask is not None:
            top_asks[side_index] = best_ask / PRICE_SCALE
        ask_indices = torch.nonzero(book.levels[1] > 0, as_tuple=False).flatten()
        ask_sizes = book.levels[1].index_select(0, ask_indices)
        depth.append((ask_indices.to(torch.float64) / PRICE_SCALE, ask_sizes))
    maximum_depth = max((prices.numel() for prices, _ in depth), default=0)
    depth_prices = torch.full((2, maximum_depth), torch.nan, dtype=torch.float64)
    depth_sizes = torch.zeros((2, maximum_depth), dtype=torch.float64)
    for side_index, (prices, sizes) in enumerate(depth):
        depth_prices[side_index, : prices.numel()] = prices
        depth_sizes[side_index, : sizes.numel()] = sizes
    return PmxtBookSnapshot(
        condition_id=condition_id.lower(),
        receive_timestamp_ms=decision_timestamp_ms,
        token_ids=token_ids,
        bids=top_bids,
        asks=top_asks,
        ask_depth_prices=depth_prices,
        ask_depth_sizes=depth_sizes,
        event_rows=len(rows),
        ignored_pre_snapshot_rows=ignored_pre_snapshot_rows,
        initial_snapshot_received_ms=initial_snapshot_received_ms,
        initialized=initialized,
    )
