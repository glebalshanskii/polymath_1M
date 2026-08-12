from __future__ import annotations

import json
import unittest

from polymath_1M.collector.book import BookDataError, OrderBookStore


class CollectorBookTest(unittest.TestCase):
    def test_legacy_snapshot_and_change_are_deterministic(self) -> None:
        snapshot = json.dumps(
            [
                {
                    "market": "condition",
                    "asset_id": "token",
                    "timestamp": "1000",
                    "hash": "h1",
                    "bids": [
                        {"price": "0.40", "size": "3"},
                        {"price": "0.41", "size": "2"},
                    ],
                    "asks": [{"price": "0.43", "size": "4"}],
                }
            ]
        )
        change = json.dumps(
            {
                "market": "condition",
                "timestamp": "1001",
                "price_changes": [
                    {
                        "asset_id": "token",
                        "price": "0.41",
                        "size": "0",
                        "side": "BUY",
                        "hash": "h2",
                    },
                    {
                        "asset_id": "token",
                        "price": "0.42",
                        "size": "5.5",
                        "side": "BUY",
                        "hash": "h3",
                    },
                ],
            }
        )
        first = OrderBookStore()
        second = OrderBookStore()
        for store in (first, second):
            store.apply_raw(snapshot)
            store.apply_raw(change)

        self.assertEqual(first.summary(), second.summary())
        asset = first.summary()["assets"]["token"]
        self.assertEqual(asset["best_bid"], 0.42)
        self.assertEqual(asset["best_ask"], 0.43)
        self.assertEqual(asset["changes"], 2)

    def test_accepts_wrapped_v2_market_event(self) -> None:
        raw = json.dumps(
            {
                "topic": "market",
                "type": "book",
                "payload": {
                    "market": "condition",
                    "tokenId": "token",
                    "timestamp": "1000",
                    "bids": [{"price": "0.1", "size": "1"}],
                    "asks": [{"price": "0.9", "size": "1"}],
                },
            }
        )
        store = OrderBookStore()
        store.apply_raw(raw)
        self.assertEqual(store.summary()["initialized_asset_count"], 1)

    def test_rejects_update_before_snapshot(self) -> None:
        raw = json.dumps(
            {
                "price_changes": [
                    {
                        "asset_id": "token",
                        "price": "0.5",
                        "size": "1",
                        "side": "BUY",
                    }
                ]
            }
        )
        with self.assertRaisesRegex(BookDataError, "before initial"):
            OrderBookStore().apply_raw(raw)

    def test_best_hints_remove_levels_from_split_atomic_update(self) -> None:
        store = OrderBookStore()
        store.apply_raw(
            json.dumps(
                {
                    "asset_id": "token",
                    "bids": [
                        {"price": "0.37", "size": "1"},
                        {"price": "0.38", "size": "1"},
                        {"price": "0.39", "size": "1"},
                    ],
                    "asks": [{"price": "0.40", "size": "1"}],
                }
            )
        )
        store.apply_raw(
            json.dumps(
                {
                    "price_changes": [
                        {
                            "asset_id": "token",
                            "price": "0.38",
                            "size": "2",
                            "side": "SELL",
                            "best_bid": "0.37",
                            "best_ask": "0.38",
                        }
                    ]
                }
            )
        )

        asset = store.summary()["assets"]["token"]
        self.assertEqual(asset["best_bid"], 0.37)
        self.assertEqual(asset["best_ask"], 0.38)
        self.assertEqual(store.crossed_states, 0)

    def test_rejects_price_beyond_supported_tick_precision(self) -> None:
        raw = json.dumps(
            {
                "asset_id": "token",
                "bids": [{"price": "0.12345", "size": "1"}],
                "asks": [],
            }
        )
        with self.assertRaisesRegex(BookDataError, "more than four"):
            OrderBookStore().apply_raw(raw)

    def test_tracks_allowed_tick_change_and_rejects_unknown_tick(self) -> None:
        store = OrderBookStore()
        store.apply_raw(
            json.dumps(
                {
                    "event_type": "tick_size_change",
                    "asset_id": "token",
                    "new_tick_size": "0.001",
                }
            )
        )
        self.assertEqual(store.summary()["tick_sizes"], {"token": "0.001"})

        with self.assertRaisesRegex(BookDataError, "unsupported tick"):
            store.apply_raw(
                json.dumps(
                    {
                        "event_type": "tick_size_change",
                        "asset_id": "token",
                        "new_tick_size": "0.005",
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
