from __future__ import annotations

import unittest

import pyarrow as pa

from polymath_1M.historical.pmxt import replay_pmxt_book


class PmxtAdapterTest(unittest.TestCase):
    def test_replay_uses_receive_time_and_updates_full_depth(self) -> None:
        condition = "0x" + "ab" * 32
        rows = [
            {
                "condition_id": condition,
                "receive_timestamp_ms": 1000,
                "source_timestamp_ms": 900,
                "event_type": "book",
                "asset_id": "up",
                "bids": '[["0.40","2"]]',
                "asks": '[["0.50","3"],["0.51","4"]]',
                "price": None,
                "size": None,
                "side": None,
                "best_bid": None,
                "best_ask": None,
            },
            {
                "condition_id": condition,
                "receive_timestamp_ms": 1000,
                "source_timestamp_ms": 900,
                "event_type": "book",
                "asset_id": "down",
                "bids": '[["0.48","2"]]',
                "asks": '[["0.52","3"]]',
                "price": None,
                "size": None,
                "side": None,
                "best_bid": None,
                "best_ask": None,
            },
            {
                "condition_id": condition,
                "receive_timestamp_ms": 1100,
                "source_timestamp_ms": 1050,
                "event_type": "price_change",
                "asset_id": "up",
                "bids": None,
                "asks": None,
                "price": 0.50,
                "size": 0.0,
                "side": "SELL",
                "best_bid": 0.40,
                "best_ask": 0.51,
            },
            {
                "condition_id": condition,
                "receive_timestamp_ms": 1100,
                "source_timestamp_ms": 1050,
                "event_type": "price_change",
                "asset_id": "up",
                "bids": None,
                "asks": None,
                "price": 0.51,
                "size": 4.0,
                "side": "SELL",
                "best_bid": 0.40,
                "best_ask": 0.51,
            },
            {
                "condition_id": condition,
                "receive_timestamp_ms": 1300,
                "source_timestamp_ms": 950,
                "event_type": "price_change",
                "asset_id": "up",
                "bids": None,
                "asks": None,
                "price": 0.51,
                "size": 0.0,
                "side": "SELL",
                "best_bid": 0.40,
                "best_ask": 0.52,
            },
        ]
        table = pa.Table.from_pylist(rows)
        snapshot = replay_pmxt_book(
            table,
            condition_id=condition,
            token_ids=("up", "down"),
            decision_timestamp_ms=1200,
        )
        self.assertTrue(snapshot.initialized)
        self.assertEqual(snapshot.event_rows, 4)
        self.assertEqual(snapshot.ignored_pre_snapshot_rows, 0)
        self.assertEqual(snapshot.initial_snapshot_received_ms, 1000)
        self.assertAlmostEqual(snapshot.asks[0].item(), 0.51)
        self.assertAlmostEqual(snapshot.ask_depth_sizes[0, 0].item(), 4.0)
        self.assertAlmostEqual(snapshot.asks[1].item(), 0.52)


if __name__ == "__main__":
    unittest.main()
