from __future__ import annotations

import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import torch

from .pmxt import PmxtDataError, load_pmxt_archive_config


class PmxtStoreError(RuntimeError):
    """The local PMXT store cannot satisfy its declared data contract."""


@dataclass(frozen=True)
class PmxtStorePilotConfig:
    schema_version: int
    experiment_id: str
    pmxt_config: Path
    universe_path: Path
    store_root: Path
    metrics_path: Path
    cohort_start: datetime
    cohort_end_exclusive: datetime
    assets: tuple[str, ...]
    duration: str
    source_padding_hours: int
    expected_market_count: int
    workers: int
    duckdb_memory_limit: str
    row_group_size: int
    compression_level: int


MARKET_SCHEMA = pa.schema(
    [
        ("market_key", pa.uint32()),
        ("series_id", pa.string()),
        ("series_slug", pa.string()),
        ("asset", pa.string()),
        ("duration", pa.string()),
        ("event_id", pa.string()),
        ("market_id", pa.string()),
        ("slug", pa.string()),
        ("condition_id", pa.string()),
        ("token_up", pa.string()),
        ("token_down", pa.string()),
        ("market_start_ms", pa.int64()),
        ("market_end_ms", pa.int64()),
        ("outcome_up", pa.float64()),
        ("fee_rate", pa.float64()),
        ("fee_exponent", pa.float64()),
        ("fees_enabled", pa.bool_()),
        ("resolution_source", pa.string()),
        ("rules", pa.string()),
    ]
)


def _parse_utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PmxtStoreError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PmxtStoreError(f"{field} must include the UTC offset")
    return parsed.astimezone(UTC)


def load_pmxt_store_pilot_config(path: str | Path) -> PmxtStorePilotConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "experiment_id",
        "pmxt_config",
        "universe_path",
        "store_root",
        "metrics_path",
        "cohort_start",
        "cohort_end_exclusive",
        "assets",
        "duration",
        "source_padding_hours",
        "expected_market_count",
        "workers",
        "duckdb_memory_limit",
        "row_group_size",
        "compression_level",
    }
    if payload.keys() != required:
        raise PmxtStoreError(
            "PMXT store config fields differ: "
            f"missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    cohort_start = _parse_utc(str(payload["cohort_start"]), "cohort_start")
    cohort_end = _parse_utc(
        str(payload["cohort_end_exclusive"]), "cohort_end_exclusive"
    )
    assets = tuple(str(value).upper() for value in payload["assets"])
    config = PmxtStorePilotConfig(
        schema_version=int(payload["schema_version"]),
        experiment_id=str(payload["experiment_id"]),
        pmxt_config=Path(payload["pmxt_config"]),
        universe_path=Path(payload["universe_path"]),
        store_root=Path(payload["store_root"]),
        metrics_path=Path(payload["metrics_path"]),
        cohort_start=cohort_start,
        cohort_end_exclusive=cohort_end,
        assets=assets,
        duration=str(payload["duration"]),
        source_padding_hours=int(payload["source_padding_hours"]),
        expected_market_count=int(payload["expected_market_count"]),
        workers=int(payload["workers"]),
        duckdb_memory_limit=str(payload["duckdb_memory_limit"]),
        row_group_size=int(payload["row_group_size"]),
        compression_level=int(payload["compression_level"]),
    )
    if config.schema_version != 1:
        raise PmxtStoreError("unsupported PMXT store pilot schema")
    if config.cohort_end_exclusive <= config.cohort_start:
        raise PmxtStoreError("cohort interval must be nonempty")
    if config.cohort_start.minute or config.cohort_start.second:
        raise PmxtStoreError("cohort_start must be hour-aligned")
    if config.cohort_end_exclusive.minute or config.cohort_end_exclusive.second:
        raise PmxtStoreError("cohort_end_exclusive must be hour-aligned")
    if set(assets) != {"BTC", "ETH", "SOL", "XRP"} or len(assets) != 4:
        raise PmxtStoreError("pilot assets must be exactly BTC/ETH/SOL/XRP")
    if config.duration != "5m":
        raise PmxtStoreError("the pilot supports the selected 5m universe only")
    if not 1 <= config.source_padding_hours <= 6:
        raise PmxtStoreError("source_padding_hours must be between 1 and 6")
    if config.expected_market_count <= 0 or not 1 <= config.workers <= 8:
        raise PmxtStoreError("invalid expected_market_count or workers")
    if config.row_group_size < 16_384 or not 1 <= config.compression_level <= 19:
        raise PmxtStoreError("invalid Parquet row-group or compression setting")
    return config


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


def _hour_key(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H")


def _hour_range(start: datetime, end_exclusive: datetime) -> list[datetime]:
    hours: list[datetime] = []
    current = start
    while current < end_exclusive:
        hours.append(current)
        current += timedelta(hours=1)
    return hours


def _select_markets(config: PmxtStorePilotConfig) -> list[dict[str, Any]]:
    if not config.universe_path.is_file():
        raise PmxtStoreError(f"universe is missing: {config.universe_path}")
    table = pq.read_table(
        config.universe_path,
        filters=[
            ("market_start_ms", ">=", _milliseconds(config.cohort_start)),
            (
                "market_start_ms",
                "<",
                _milliseconds(config.cohort_end_exclusive),
            ),
            ("duration", "=", config.duration),
            ("asset", "in", list(config.assets)),
        ],
    )
    rows = sorted(
        table.to_pylist(),
        key=lambda row: (
            int(row["market_start_ms"]),
            str(row["asset"]),
            str(row["condition_id"]),
        ),
    )
    if len(rows) != config.expected_market_count:
        raise PmxtStoreError(
            f"selected {len(rows)} markets; expected {config.expected_market_count}"
        )
    if {str(row["asset"]) for row in rows} != set(config.assets):
        raise PmxtStoreError("selected universe does not contain every declared asset")
    conditions = [str(row["condition_id"]).lower() for row in rows]
    tokens = [str(row[token]) for row in rows for token in ("token_up", "token_down")]
    if len(set(conditions)) != len(conditions) or len(set(tokens)) != len(tokens):
        raise PmxtStoreError("condition and token IDs must be unique")
    selected: list[dict[str, Any]] = []
    for market_key, row in enumerate(rows):
        selected.append({"market_key": market_key, **row})
    return selected


def _write_markets(path: Path, markets: list[dict[str, Any]]) -> None:
    table = pa.Table.from_pylist(markets, schema=MARKET_SCHEMA)
    pq.write_table(
        table,
        path,
        compression="zstd",
        compression_level=9,
        use_dictionary=True,
    )


def _candidate_requests(
    markets: list[dict[str, Any]], hour: datetime, padding_hours: int
) -> pa.Table:
    hour_start_ms = _milliseconds(hour)
    hour_end_ms = hour_start_ms + 3_600_000
    padding_ms = padding_hours * 3_600_000
    rows: list[dict[str, Any]] = []
    for market in markets:
        window_start_ms = int(market["market_start_ms"]) - padding_ms
        window_end_ms = int(market["market_end_ms"]) + padding_ms
        if window_start_ms >= hour_end_ms or window_end_ms <= hour_start_ms:
            continue
        condition = str(market["condition_id"]).lower()
        for token_side, token_field in enumerate(("token_up", "token_down")):
            rows.append(
                {
                    "market_key": int(market["market_key"]),
                    "asset": str(market["asset"]),
                    "market": condition.encode("ascii"),
                    "asset_id": str(market[token_field]),
                    "token_side": token_side,
                    "window_start_ms": window_start_ms,
                    "window_end_ms": window_end_ms,
                }
            )
    schema = pa.schema(
        [
            ("market_key", pa.uint32()),
            ("asset", pa.string()),
            ("market", pa.binary()),
            ("asset_id", pa.string()),
            ("token_side", pa.uint8()),
            ("window_start_ms", pa.int64()),
            ("window_end_ms", pa.int64()),
        ]
    )
    return pa.Table.from_pylist(rows, schema=schema)


def _quote_sql(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _source_size(url: str) -> int:
    local = Path(url)
    if local.is_file():
        return local.stat().st_size
    request = Request(url, method="HEAD", headers={"User-Agent": "polymath-1m/1"})
    error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                return int(response.headers.get("Content-Length", 0))
        except OSError as exc:
            error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise PmxtStoreError(f"PMXT HEAD failed for {url}: {error}")


def _profile_value(profile_path: Path, key: str) -> int:
    if not profile_path.is_file():
        return 0
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    value = payload.get(key, 0)
    return int(value) if isinstance(value, int | float) else 0


def _retryable_duckdb_error(error: duckdb.Error) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "ssl connect error",
            "http error",
            "connection reset",
            "connection timed out",
            "timeout was reached",
            "failed to connect",
            "failure when receiving data from the peer",
            "io error",
        )
    )


def _extract_hour(
    *,
    source_url: str,
    source_hour: datetime,
    requests: pa.Table,
    output_path: Path,
    profile_path: Path,
    duckdb_temp: Path,
    memory_limit: str,
) -> dict[str, Any]:
    if requests.num_rows == 0:
        raise PmxtStoreError(f"no candidate markets for {_hour_key(source_hour)}")
    conditions = tuple(dict.fromkeys(requests["market"].to_pylist()))
    placeholders = ",".join("?" for _ in conditions)
    query = f"""
        SELECT
            r.market_key::UINTEGER AS market_key,
            r.asset,
            r.token_side::UTINYINT AS token_side,
            epoch_ms(p.timestamp_received)::BIGINT AS receive_timestamp_ms,
            epoch_ms(p.timestamp)::BIGINT AS source_timestamp_ms,
            ?::BIGINT AS source_hour_ms,
            p.file_row_number::UBIGINT AS source_row,
            CASE p.event_type
                WHEN 'book' THEN 0
                WHEN 'price_change' THEN 1
                WHEN 'last_trade_price' THEN 2
                WHEN 'tick_size_change' THEN 3
                ELSE 255
            END::UTINYINT AS event_type,
            p.bids,
            p.asks,
            CASE WHEN p.price IS NULL THEN NULL
                 ELSE CAST(p.price * 10000 AS INTEGER) END AS price_e4,
            CASE WHEN p.size IS NULL THEN NULL
                 ELSE CAST(CAST(p.size AS DECIMAL(38, 6)) * 1000000 AS BIGINT)
                 END AS size_e6,
            CASE p.side WHEN 'BUY' THEN 0 WHEN 'SELL' THEN 1
                 ELSE NULL END::UTINYINT AS side,
            CASE WHEN p.best_bid IS NULL THEN NULL
                 ELSE CAST(p.best_bid * 10000 AS INTEGER) END AS best_bid_e4,
            CASE WHEN p.best_ask IS NULL THEN NULL
                 ELSE CAST(p.best_ask * 10000 AS INTEGER) END AS best_ask_e4,
            p.fee_rate_bps,
            p.transaction_hash,
            CASE WHEN p.old_tick_size IS NULL THEN NULL
                 ELSE CAST(p.old_tick_size * 10000 AS INTEGER) END
                 AS old_tick_size_e4,
            CASE WHEN p.new_tick_size IS NULL THEN NULL
                 ELSE CAST(p.new_tick_size * 10000 AS INTEGER) END
                 AS new_tick_size_e4
        FROM read_parquet(?, file_row_number=true) AS p
        JOIN requested AS r
          ON p.market = r.market AND p.asset_id = r.asset_id
        WHERE p.market IN ({placeholders})
          AND epoch_ms(p.timestamp_received) >= r.window_start_ms
          AND epoch_ms(p.timestamp_received) < r.window_end_ms
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_suffix(".parquet.partial")
    query_seconds = 0.0
    query_attempts = 0
    for attempt in range(3):
        query_attempts = attempt + 1
        partial_path.unlink(missing_ok=True)
        profile_path.unlink(missing_ok=True)
        shutil.rmtree(duckdb_temp, ignore_errors=True)
        duckdb_temp.mkdir(parents=True)
        connection = duckdb.connect()
        connection.register("requested", requests)
        started = time.monotonic()
        try:
            connection.execute("LOAD httpfs")
            connection.execute("SET http_timeout=30")
            connection.execute("SET http_retries=1")
            connection.execute(f"SET memory_limit={_quote_sql(memory_limit)}")
            connection.execute(f"SET temp_directory={_quote_sql(duckdb_temp)}")
            connection.execute("SET preserve_insertion_order=false")
            connection.execute("PRAGMA enable_profiling='json'")
            connection.execute(f"PRAGMA profiling_output={_quote_sql(profile_path)}")
            copy_sql = (
                f"COPY ({query}) TO {_quote_sql(partial_path)} "
                "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 3, "
                "ROW_GROUP_SIZE 131072)"
            )
            connection.execute(
                copy_sql,
                [_milliseconds(source_hour), source_url, *conditions],
            )
            query_seconds += time.monotonic() - started
            break
        except duckdb.Error as exc:
            query_seconds += time.monotonic() - started
            if attempt == 2 or not _retryable_duckdb_error(exc):
                raise
            time.sleep(attempt + 1)
        finally:
            connection.unregister("requested")
            connection.close()
    local = duckdb.connect()
    try:
        counts = local.execute(
            f"""
            SELECT
                count(*)::BIGINT,
                min(receive_timestamp_ms)::BIGINT,
                max(receive_timestamp_ms)::BIGINT,
                coalesce(count_if(event_type = 0), 0)::BIGINT,
                coalesce(count_if(event_type = 1), 0)::BIGINT,
                coalesce(count_if(event_type = 2), 0)::BIGINT,
                coalesce(count_if(event_type = 3), 0)::BIGINT,
                coalesce(count_if(event_type = 255), 0)::BIGINT
            FROM read_parquet({_quote_sql(partial_path)})
            """
        ).fetchone()
    finally:
        local.close()
    assert counts is not None
    if int(counts[7]) != 0:
        raise PmxtStoreError(f"unknown PMXT event type in {source_url}")
    result = {
        "source_hour": _hour_key(source_hour),
        "source_url": source_url,
        "candidate_market_count": requests.num_rows // 2,
        "event_row_count": int(counts[0]),
        "min_receive_timestamp_ms": counts[1],
        "max_receive_timestamp_ms": counts[2],
        "book_rows": int(counts[3]),
        "price_change_rows": int(counts[4]),
        "last_trade_rows": int(counts[5]),
        "tick_size_rows": int(counts[6]),
        "source_object_bytes": _source_size(source_url),
        "duckdb_bytes_read": _profile_value(profile_path, "total_bytes_read"),
        "peak_buffer_memory": _profile_value(profile_path, "system_peak_buffer_memory"),
        "query_seconds": query_seconds,
        "query_attempts": query_attempts,
        "status": "complete",
    }
    partial_path.replace(output_path)
    return result


def _compact_events(
    hourly_dir: Path,
    events_dir: Path,
    *,
    row_group_size: int,
    compression_level: int,
    memory_limit: str,
    duckdb_temp: Path,
) -> float:
    duckdb_temp.mkdir(parents=True, exist_ok=True)
    if events_dir.exists():
        shutil.rmtree(events_dir)
    connection = duckdb.connect()
    try:
        connection.execute(f"SET memory_limit={_quote_sql(memory_limit)}")
        connection.execute("SET threads=1")
        connection.execute(f"SET temp_directory={_quote_sql(duckdb_temp)}")
        connection.execute("SET preserve_insertion_order=true")
        source_glob = hourly_dir / "*.parquet"
        partitions = connection.execute(
            f"""
            SELECT DISTINCT
                CAST(epoch_ms(receive_timestamp_ms) AS DATE) AS receive_date,
                asset
            FROM read_parquet({_quote_sql(source_glob)})
            ORDER BY receive_date, asset
            """
        ).fetchall()
        started = time.monotonic()
        for receive_date, asset in partitions:
            partition = (
                events_dir
                / f"receive_date={receive_date.isoformat()}"
                / f"asset={asset}"
            )
            partition.mkdir(parents=True, exist_ok=True)
            output = partition / "data_0.parquet"
            query = f"""
                SELECT * EXCLUDE (asset)
                FROM read_parquet({_quote_sql(source_glob)})
                WHERE CAST(epoch_ms(receive_timestamp_ms) AS DATE) =
                          DATE {_quote_sql(receive_date.isoformat())}
                  AND asset = {_quote_sql(asset)}
                ORDER BY market_key, receive_timestamp_ms,
                         source_timestamp_ms, source_hour_ms, source_row
            """
            copy = (
                f"COPY ({query}) TO {_quote_sql(output)} "
                "(FORMAT PARQUET, COMPRESSION ZSTD, "
                f"COMPRESSION_LEVEL {compression_level}, "
                f"ROW_GROUP_SIZE {row_group_size})"
            )
            connection.execute(copy)
        return time.monotonic() - started
    finally:
        connection.close()


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


_PHYSICAL_ORDER_COLUMNS = (
    "market_key",
    "receive_timestamp_ms",
    "source_timestamp_ms",
    "source_hour_ms",
    "source_row",
)


def _count_physical_order_violations(
    event_files: list[Path], *, batch_size: int = 1_048_576
) -> int:
    """Count adjacent rows that violate the declared per-file sort key."""
    violations = 0
    null_sentinel = torch.iinfo(torch.int64).max
    for event_file in event_files:
        previous_key: tuple[int, ...] | None = None
        parquet = pq.ParquetFile(event_file)
        for batch in parquet.iter_batches(
            batch_size=batch_size, columns=list(_PHYSICAL_ORDER_COLUMNS)
        ):
            keys = [
                torch.from_numpy(
                    pc.fill_null(pc.cast(column, pa.int64()), null_sentinel)
                    .to_numpy(zero_copy_only=False)
                    .copy()
                ).to(dtype=torch.int64)
                for column in batch.columns
            ]
            row_count = batch.num_rows
            if row_count == 0:
                continue
            first_key = tuple(int(key[0].item()) for key in keys)
            if previous_key is not None and previous_key > first_key:
                violations += 1
            if row_count > 1:
                equal_prefix = torch.ones(row_count - 1, dtype=torch.bool)
                out_of_order = torch.zeros(row_count - 1, dtype=torch.bool)
                for key in keys:
                    left = key[:-1]
                    right = key[1:]
                    out_of_order |= equal_prefix & (left > right)
                    equal_prefix &= left == right
                violations += int(torch.count_nonzero(out_of_order).item())
            previous_key = tuple(int(key[-1].item()) for key in keys)
    return violations


def query_market_events(
    store_root: str | Path,
    *,
    market_key: int,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> pa.Table:
    root = Path(store_root)
    event_glob = root / "events" / "**" / "*.parquet"
    if not (root / "markets.parquet").is_file() or not list(
        (root / "events").glob("**/*.parquet")
    ):
        raise PmxtStoreError(f"incomplete PMXT store: {root}")
    predicates = ["market_key = ?"]
    parameters: list[Any] = [int(market_key)]
    if start_ms is not None:
        predicates.append("receive_timestamp_ms >= ?")
        parameters.append(int(start_ms))
    if end_ms is not None:
        predicates.append("receive_timestamp_ms < ?")
        parameters.append(int(end_ms))
    connection = duckdb.connect()
    try:
        return connection.execute(
            f"""
            SELECT * EXCLUDE (receive_date, asset)
            FROM read_parquet({_quote_sql(event_glob)}, hive_partitioning=true)
            WHERE {" AND ".join(predicates)}
            ORDER BY receive_timestamp_ms, source_timestamp_ms,
                     source_hour_ms, source_row
            """,
            parameters,
        ).to_arrow_table()
    finally:
        connection.close()


def validate_pmxt_store(store_root: str | Path) -> dict[str, Any]:
    root = Path(store_root)
    markets_path = root / "markets.parquet"
    coverage_path = root / "coverage.parquet"
    event_files = sorted((root / "events").glob("**/*.parquet"))
    if not markets_path.is_file() or not coverage_path.is_file() or not event_files:
        raise PmxtStoreError(f"incomplete PMXT store: {root}")
    connection = duckdb.connect()
    try:
        event_glob = root / "events" / "**" / "*.parquet"
        summary = connection.execute(
            f"""
            SELECT
                count(*)::BIGINT,
                count(DISTINCT market_key)::BIGINT,
                min(receive_timestamp_ms)::BIGINT,
                max(receive_timestamp_ms)::BIGINT,
                count_if(event_type IS NULL
                         OR event_type NOT BETWEEN 0 AND 3)::BIGINT,
                count_if(token_side IS NULL
                         OR token_side NOT BETWEEN 0 AND 1)::BIGINT,
                count_if(price_e4 IS NOT NULL AND price_e4 NOT BETWEEN 0 AND 10000)
                    ::BIGINT,
                count_if(best_bid_e4 IS NOT NULL
                         AND best_bid_e4 NOT BETWEEN 0 AND 10000)::BIGINT,
                count_if(best_ask_e4 IS NOT NULL
                         AND best_ask_e4 NOT BETWEEN 0 AND 10000)::BIGINT,
                count_if(size_e6 IS NOT NULL AND size_e6 < 0)::BIGINT,
                count_if(side IS NOT NULL AND side NOT BETWEEN 0 AND 1)::BIGINT,
                count_if(fee_rate_bps IS NOT NULL AND fee_rate_bps < 0)::BIGINT,
                count_if(market_key IS NULL
                         OR receive_timestamp_ms IS NULL
                         OR source_timestamp_ms IS NULL
                         OR source_hour_ms IS NULL
                         OR source_row IS NULL)::BIGINT,
                min(market_key)::BIGINT,
                max(market_key)::BIGINT
            FROM read_parquet({_quote_sql(event_glob)}, hive_partitioning=true)
            """
        ).fetchone()
        market_count = connection.execute(
            f"SELECT count(*) FROM read_parquet({_quote_sql(markets_path)})"
        ).fetchone()[0]
        coverage = connection.execute(
            f"""
            SELECT count(*), coalesce(sum(event_row_count), 0),
                   count_if(status IS NULL OR status != 'complete'),
                   count(DISTINCT source_hour),
                   coalesce(sum(book_rows + price_change_rows
                                + last_trade_rows + tick_size_rows), 0)
            FROM read_parquet({_quote_sql(coverage_path)})
            """
        ).fetchone()
    finally:
        connection.close()
    assert summary is not None and coverage is not None
    invalid_domains = summary[4:13]
    if any(int(value) != 0 for value in invalid_domains):
        raise PmxtStoreError(f"event-domain validation failed: {invalid_domains}")
    if (
        int(summary[0]) != int(coverage[1])
        or int(coverage[2]) != 0
        or int(coverage[0]) != int(coverage[3])
        or int(coverage[1]) != int(coverage[4])
    ):
        raise PmxtStoreError("coverage row count/status differs from event store")
    if (
        int(summary[1]) != int(market_count)
        or int(summary[13]) != 0
        or int(summary[14]) != int(market_count) - 1
    ):
        raise PmxtStoreError("at least one selected market has no PMXT events")
    physical_order_violations = _count_physical_order_violations(event_files)
    if physical_order_violations != 0:
        raise PmxtStoreError(
            "event partitions are not in declared physical order: "
            f"{physical_order_violations} adjacent violations"
        )
    return {
        "market_count": int(market_count),
        "event_row_count": int(summary[0]),
        "covered_market_count": int(summary[1]),
        "source_hour_count": int(coverage[0]),
        "min_receive_timestamp_ms": int(summary[2]),
        "max_receive_timestamp_ms": int(summary[3]),
        "event_file_count": len(event_files),
        "physical_order_violation_count": physical_order_violations,
        "store_bytes": _directory_bytes(root),
    }


def _benchmark_store(
    store_root: Path, markets: list[dict[str, Any]], config: PmxtStorePilotConfig
) -> dict[str, Any]:
    market = markets[len(markets) // 2]
    started = time.monotonic()
    point = query_market_events(store_root, market_key=int(market["market_key"]))
    point_seconds = time.monotonic() - started
    event_glob = store_root / "events" / "**" / "*.parquet"
    connection = duckdb.connect()
    try:
        started = time.monotonic()
        day = connection.execute(
            f"""
            SELECT count(*)::BIGINT, avg(best_ask_e4)::DOUBLE
            FROM read_parquet({_quote_sql(event_glob)}, hive_partitioning=true)
            WHERE receive_timestamp_ms >= ? AND receive_timestamp_ms < ?
            """,
            [
                _milliseconds(config.cohort_start),
                _milliseconds(config.cohort_end_exclusive),
            ],
        ).fetchone()
        day_scan_seconds = time.monotonic() - started
    finally:
        connection.close()
    assert day is not None
    return {
        "point_market_key": int(market["market_key"]),
        "point_event_rows": point.num_rows,
        "point_query_seconds": point_seconds,
        "day_event_rows": int(day[0]),
        "day_scan_seconds": day_scan_seconds,
    }


def _market_identity(markets: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            int(row["market_key"]),
            str(row["condition_id"]),
            str(row["token_up"]),
            str(row["token_down"]),
            int(row["market_start_ms"]),
            int(row["market_end_ms"]),
        )
        for row in markets
    ]


def _resume_coverage(
    *,
    hours: list[datetime],
    hourly_dir: Path,
    coverage_hours_dir: Path,
) -> tuple[list[dict[str, Any]], list[datetime]]:
    completed: list[dict[str, Any]] = []
    pending: list[datetime] = []
    for hour in hours:
        key = _hour_key(hour)
        output = hourly_dir / f"hour={key}.parquet"
        sidecar = coverage_hours_dir / f"hour={key}.json"
        try:
            row = json.loads(sidecar.read_text(encoding="utf-8"))
            parquet_rows = pq.ParquetFile(output).metadata.num_rows
            valid = (
                row.get("source_hour") == key
                and row.get("status") == "complete"
                and int(row.get("event_row_count", -1)) == parquet_rows
            )
        except FileNotFoundError, ValueError, TypeError, pa.ArrowInvalid:
            valid = False
        if valid:
            completed.append(row)
            continue
        output.unlink(missing_ok=True)
        output.with_suffix(".parquet.partial").unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        pending.append(hour)
    return completed, pending


def _write_coverage_hour(directory: Path, row: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"hour={row['source_hour']}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def build_pmxt_store_pilot(config_path: str | Path, *, resume: bool = False) -> Path:
    config = load_pmxt_store_pilot_config(config_path)
    archive = load_pmxt_archive_config(config.pmxt_config)
    markets = _select_markets(config)
    final_root = config.store_root
    building_root = final_root.with_name(final_root.name + ".building")
    if final_root.exists():
        raise PmxtStoreError(
            f"store target already exists; inspect it before retrying: {final_root}"
        )
    if building_root.exists() and not resume:
        raise PmxtStoreError(
            f"interrupted build exists; inspect it and use --resume: {building_root}"
        )
    if resume and not building_root.is_dir():
        raise PmxtStoreError(f"no interrupted build to resume: {building_root}")
    if resume:
        existing = pq.read_table(building_root / "markets.parquet").to_pylist()
        if _market_identity(existing) != _market_identity(markets):
            raise PmxtStoreError("resume market dimension differs from current config")
    else:
        building_root.mkdir(parents=True)
        _write_markets(building_root / "markets.parquet", markets)
    hourly_dir = building_root / "_hourly"
    profiles_dir = building_root / "_profiles"
    duckdb_root = building_root / "_duckdb"
    coverage_hours_dir = building_root / "_coverage_hours"
    source_start = config.cohort_start - timedelta(hours=config.source_padding_hours)
    source_end = config.cohort_end_exclusive + timedelta(
        hours=config.source_padding_hours
    )
    hours = _hour_range(source_start, source_end)
    requests_by_hour = {
        _hour_key(hour): _candidate_requests(markets, hour, config.source_padding_hours)
        for hour in hours
    }
    coverage_rows, pending_hours = _resume_coverage(
        hours=hours,
        hourly_dir=hourly_dir,
        coverage_hours_dir=coverage_hours_dir,
    )
    resumed_hour_count = len(coverage_rows)
    run_started = time.monotonic()
    for batch_start in range(0, len(pending_hours), config.workers):
        batch = pending_hours[batch_start : batch_start + config.workers]
        with ThreadPoolExecutor(max_workers=config.workers) as executor:
            futures = {}
            for hour in batch:
                key = _hour_key(hour)
                try:
                    source_url = archive.object_url(key)
                except PmxtDataError as exc:
                    raise PmxtStoreError(str(exc)) from exc
                future = executor.submit(
                    _extract_hour,
                    source_url=source_url,
                    source_hour=hour,
                    requests=requests_by_hour[key],
                    output_path=hourly_dir / f"hour={key}.parquet",
                    profile_path=profiles_dir / f"hour={key}.json",
                    duckdb_temp=duckdb_root / key,
                    memory_limit=config.duckdb_memory_limit,
                )
                futures[future] = key
            batch_errors: list[tuple[str, Exception]] = []
            for future in as_completed(futures):
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001
                    batch_errors.append((futures[future], exc))
                    continue
                coverage_rows.append(row)
                _write_coverage_hour(coverage_hours_dir, row)
            if batch_errors:
                key, error = batch_errors[0]
                raise PmxtStoreError(
                    f"PMXT extraction failed for {key}: {error}"
                ) from error
    extraction_seconds = time.monotonic() - run_started
    coverage_rows.sort(key=lambda row: str(row["source_hour"]))
    compaction_seconds = _compact_events(
        hourly_dir,
        building_root / "events",
        row_group_size=config.row_group_size,
        compression_level=config.compression_level,
        memory_limit=config.duckdb_memory_limit,
        duckdb_temp=duckdb_root / "compaction",
    )
    pq.write_table(
        pa.Table.from_pylist(coverage_rows),
        building_root / "coverage.parquet",
        compression="zstd",
        compression_level=9,
    )
    validate_pmxt_store(building_root)
    shutil.rmtree(hourly_dir)
    shutil.rmtree(profiles_dir)
    shutil.rmtree(duckdb_root)
    shutil.rmtree(coverage_hours_dir)
    building_root.rename(final_root)
    validation = validate_pmxt_store(final_root)
    benchmark = _benchmark_store(final_root, markets, config)
    metrics = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "source": "PMXT v2",
        "cohort_start": config.cohort_start.isoformat(),
        "cohort_end_exclusive": config.cohort_end_exclusive.isoformat(),
        "assets": list(config.assets),
        "duration": config.duration,
        "source_padding_hours": config.source_padding_hours,
        "resumed_hour_count": resumed_hour_count,
        "network_retry_count": sum(
            int(row["query_attempts"]) - 1 for row in coverage_rows
        ),
        "source_object_bytes": sum(
            int(row["source_object_bytes"]) for row in coverage_rows
        ),
        "duckdb_bytes_read": sum(
            int(row["duckdb_bytes_read"]) for row in coverage_rows
        ),
        "maximum_peak_buffer_memory": max(
            int(row["peak_buffer_memory"]) for row in coverage_rows
        ),
        "successful_query_worker_seconds": sum(
            float(row["query_seconds"]) for row in coverage_rows
        ),
        "extraction_seconds": extraction_seconds,
        "compaction_seconds": compaction_seconds,
        "total_seconds": time.monotonic() - run_started,
        "first_source_hour_event_rows": int(coverage_rows[0]["event_row_count"]),
        "last_source_hour_event_rows": int(coverage_rows[-1]["event_row_count"]),
        "validation": validation,
        "benchmark": benchmark,
    }
    config.metrics_path.parent.mkdir(parents=True, exist_ok=True)
    config.metrics_path.write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return config.metrics_path
