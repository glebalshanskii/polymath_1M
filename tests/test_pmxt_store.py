from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from polymath_1M.historical.pmxt_store import (
    MARKET_SCHEMA,
    _candidate_requests,
    _compact_events,
    _count_physical_order_violations,
    _extract_hour,
    _resume_coverage,
    _retryable_duckdb_error,
    _write_coverage_hour,
    load_pmxt_store_pilot_config,
    query_market_events,
    validate_pmxt_store,
)


def _market() -> dict[str, object]:
    return {
        "market_key": 0,
        "series_id": "series",
        "series_slug": "btc-updown-5m",
        "asset": "BTC",
        "duration": "5m",
        "event_id": "event",
        "market_id": "market",
        "slug": "btc-updown-5m-1780877100",
        "condition_id": "0x" + "ab" * 32,
        "token_up": "111",
        "token_down": "222",
        "market_start_ms": 1_780_877_100_000,
        "market_end_ms": 1_780_877_400_000,
        "outcome_up": 1.0,
        "fee_rate": 0.25,
        "fee_exponent": 2.0,
        "fees_enabled": True,
        "resolution_source": "https://example.test",
        "rules": "test rules",
    }


def _source_table(condition_id: str) -> pa.Table:
    schema = pa.schema(
        [
            ("timestamp_received", pa.timestamp("ms", tz="UTC")),
            ("timestamp", pa.timestamp("ms", tz="UTC")),
            ("market", pa.binary(66)),
            ("event_type", pa.string()),
            ("asset_id", pa.string()),
            ("bids", pa.string()),
            ("asks", pa.string()),
            ("price", pa.decimal128(9, 4)),
            ("size", pa.decimal128(18, 6)),
            ("side", pa.string()),
            ("best_bid", pa.decimal128(9, 4)),
            ("best_ask", pa.decimal128(9, 4)),
            ("fee_rate_bps", pa.uint16()),
            ("transaction_hash", pa.string()),
            ("old_tick_size", pa.decimal128(9, 4)),
            ("new_tick_size", pa.decimal128(9, 4)),
        ]
    )
    base = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)
    common = {
        "market": condition_id.encode("ascii"),
        "bids": None,
        "asks": None,
        "price": None,
        "size": None,
        "side": None,
        "best_bid": None,
        "best_ask": None,
        "fee_rate_bps": None,
        "transaction_hash": None,
        "old_tick_size": None,
        "new_tick_size": None,
    }
    rows = [
        {
            **common,
            "timestamp_received": base,
            "timestamp": base,
            "event_type": "book",
            "asset_id": "111",
            "bids": '[["0.4000","2.000000"]]',
            "asks": '[["0.5000","3.000000"]]',
        },
        {
            **common,
            "timestamp_received": base.replace(minute=6),
            "timestamp": base.replace(minute=6),
            "event_type": "price_change",
            "asset_id": "111",
            "price": Decimal("0.4321"),
            "size": Decimal("1001996.510000"),
            "side": "BUY",
            "best_bid": Decimal("0.4321"),
            "best_ask": Decimal("0.4400"),
        },
        {
            **common,
            "timestamp_received": base.replace(minute=7),
            "timestamp": base.replace(minute=7),
            "event_type": "last_trade_price",
            "asset_id": "222",
            "price": Decimal("0.5600"),
            "size": Decimal("1.250000"),
            "side": "SELL",
            "fee_rate_bps": 200,
            "transaction_hash": "0x1234",
        },
        {
            **common,
            "timestamp_received": base.replace(minute=8),
            "timestamp": base.replace(minute=8),
            "event_type": "tick_size_change",
            "asset_id": "222",
            "old_tick_size": Decimal("0.0100"),
            "new_tick_size": Decimal("0.0010"),
        },
    ]
    return pa.Table.from_pylist(rows, schema=schema)


class PmxtStoreTest(unittest.TestCase):
    def test_frozen_pilot_config_loads(self) -> None:
        config = load_pmxt_store_pilot_config(
            "cfg/experiments/pmxt_parquet_pilot_20260608.json"
        )
        self.assertEqual(config.assets, ("BTC", "ETH", "SOL", "XRP"))
        self.assertEqual(config.expected_market_count, 1152)
        self.assertEqual(config.source_padding_hours, 2)
        self.assertEqual(config.workers, 2)
        self.assertEqual(config.duckdb_memory_limit, "2GB")

    def test_full_resolution_extract_compact_query_and_validate(self) -> None:
        market = _market()
        hour = datetime(2026, 6, 8, 0, tzinfo=UTC)
        requests = _candidate_requests([market], hour, padding_hours=2)
        self.assertEqual(requests.num_rows, 2)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.parquet"
            hourly = root / "hourly"
            output = hourly / "hour=2026-06-08T00.parquet"
            profile = root / "profile.json"
            pq.write_table(_source_table(str(market["condition_id"])), source)
            coverage = _extract_hour(
                source_url=str(source),
                source_hour=hour,
                requests=requests,
                output_path=output,
                profile_path=profile,
                duckdb_temp=root / "duckdb-extract",
                memory_limit="256MB",
            )
            self.assertEqual(coverage["event_row_count"], 4)
            self.assertEqual(coverage["book_rows"], 1)
            self.assertEqual(coverage["price_change_rows"], 1)
            self.assertFalse(output.with_suffix(".parquet.partial").exists())
            extracted = pq.read_table(output)
            pq.write_table(extracted.take(pa.array([3, 2, 1, 0])), output)
            store = root / "store"
            store.mkdir()
            pq.write_table(
                pa.Table.from_pylist([market], schema=MARKET_SCHEMA),
                store / "markets.parquet",
            )
            _compact_events(
                hourly,
                store / "events",
                row_group_size=16_384,
                compression_level=3,
                memory_limit="256MB",
                duckdb_temp=root / "duckdb-compact",
            )
            pq.write_table(pa.Table.from_pylist([coverage]), store / "coverage.parquet")
            events = query_market_events(store, market_key=0)
            self.assertEqual(events.num_rows, 4)
            self.assertEqual(events["event_type"].to_pylist(), [0, 1, 2, 3])
            self.assertEqual(events["price_e4"].to_pylist(), [None, 4321, 5600, None])
            self.assertEqual(
                events["size_e6"].to_pylist(),
                [None, 1_001_996_510_000, 1_250_000, None],
            )
            validation = validate_pmxt_store(store)
            self.assertEqual(validation["market_count"], 1)
            self.assertEqual(validation["event_row_count"], 4)
            self.assertEqual(validation["event_file_count"], 1)
            self.assertEqual(validation["physical_order_violation_count"], 0)

    def test_physical_order_validator_detects_adjacent_inversion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.parquet"
            pq.write_table(
                pa.table(
                    {
                        "market_key": [1, 1],
                        "receive_timestamp_ms": [2, 1],
                        "source_timestamp_ms": [2, 1],
                        "source_hour_ms": [0, 0],
                        "source_row": [2, 1],
                    }
                ),
                path,
            )
            self.assertEqual(_count_physical_order_violations([path]), 1)

    def test_extract_accepts_an_empty_boundary_hour(self) -> None:
        market = _market()
        hour = datetime(2026, 6, 8, 0, tzinfo=UTC)
        requests = _candidate_requests([market], hour, padding_hours=2)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.parquet"
            output = root / "empty.parquet"
            source_table = _source_table(str(market["condition_id"]))
            pq.write_table(source_table.slice(0, 0), source)
            coverage = _extract_hour(
                source_url=str(source),
                source_hour=hour,
                requests=requests,
                output_path=output,
                profile_path=root / "profile.json",
                duckdb_temp=root / "duckdb",
                memory_limit="256MB",
            )
            self.assertEqual(coverage["event_row_count"], 0)
            self.assertEqual(coverage["book_rows"], 0)
            self.assertEqual(pq.ParquetFile(output).metadata.num_rows, 0)

    def test_resume_reuses_only_complete_matching_hour(self) -> None:
        hour = datetime(2026, 6, 8, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hourly = root / "hourly"
            hourly.mkdir()
            output = hourly / "hour=2026-06-08T00.parquet"
            pq.write_table(pa.table({"x": [1, 2]}), output)
            row = {
                "source_hour": "2026-06-08T00",
                "status": "complete",
                "event_row_count": 2,
            }
            coverage_hours = root / "coverage"
            _write_coverage_hour(coverage_hours, row)
            completed, pending = _resume_coverage(
                hours=[hour],
                hourly_dir=hourly,
                coverage_hours_dir=coverage_hours,
            )
            self.assertEqual(completed, [row])
            self.assertEqual(pending, [])

            row["event_row_count"] = 3
            _write_coverage_hour(coverage_hours, row)
            completed, pending = _resume_coverage(
                hours=[hour],
                hourly_dir=hourly,
                coverage_hours_dir=coverage_hours,
            )
            self.assertEqual(completed, [])
            self.assertEqual(pending, [hour])
            self.assertFalse(output.exists())

    def test_only_network_duckdb_errors_are_retried(self) -> None:
        self.assertTrue(_retryable_duckdb_error(duckdb.Error("SSL connect error")))
        self.assertTrue(_retryable_duckdb_error(duckdb.Error("Timeout was reached")))
        self.assertTrue(
            _retryable_duckdb_error(
                duckdb.Error("Failure when receiving data from the peer")
            )
        )
        self.assertFalse(_retryable_duckdb_error(duckdb.Error("decimal overflow")))


if __name__ == "__main__":
    unittest.main()
