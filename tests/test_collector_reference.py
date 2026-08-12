from __future__ import annotations

import json
import unittest

from polymath_1M.collector.models import MarketRecord
from polymath_1M.collector.reference import ReferenceTracker


def _market(topic: str, symbol: str) -> MarketRecord:
    return MarketRecord(
        series_id="1",
        series_slug="series",
        asset="BTC",
        duration="5m",
        event_id="event",
        market_id="market",
        slug="slug",
        question="question",
        condition_id="condition",
        token_ids=("up", "down"),
        outcomes=("Up", "Down"),
        start_timestamp_ms=1_000,
        end_timestamp_ms=301_000,
        description="rules",
        resolution_source="source",
        reference_topic=topic,
        reference_symbol=symbol,
        gamma_min_tick_size="0.01",
        gamma_min_order_size="5",
        gamma_fees_enabled=True,
        gamma_fee_schedule={},
        clob_min_tick_size="0.001",
        clob_min_order_size="5",
        clob_maker_base_fee_bps=0,
        clob_taker_base_fee_bps=1000,
        clob_fee_schedule={},
        clob_order_delay_enabled=True,
    )


class CollectorReferenceTest(unittest.TestCase):
    def test_matches_exact_chainlink_twap_boundary_and_decodes_e18(self) -> None:
        tracker = ReferenceTracker()
        tracker.register(_market("crypto_prices_twap_thirty", "btc/usd"))
        tracker.apply_raw(
            json.dumps(
                {
                    "topic": "crypto_prices_twap_thirty",
                    "type": "update",
                    "timestamp": 1_001,
                    "payload": {
                        "symbol": "btc/usd",
                        "timestamp": 1_000,
                        "value": 63_000.5,
                        "full_accuracy_value": "63000500000000000000000",
                    },
                }
            )
        )

        boundary = tracker.boundaries()[0]
        self.assertTrue(boundary.exact)
        self.assertEqual(boundary.value, "63000.5")

    def test_reports_missing_instead_of_inventing_nearest_boundary(self) -> None:
        tracker = ReferenceTracker()
        tracker.register(_market("crypto_prices_twap_thirty", "btc/usd"))
        tracker.apply_raw(
            json.dumps(
                {
                    "topic": "crypto_prices_twap_thirty",
                    "type": "update",
                    "timestamp": 1_002,
                    "payload": {
                        "symbol": "btc/usd",
                        "timestamp": 1_001,
                        "full_accuracy_value": "63000500000000000000000",
                    },
                }
            )
        )
        boundary = tracker.boundaries()[0]
        self.assertIsNone(boundary.value)
        self.assertFalse(boundary.exact)


if __name__ == "__main__":
    unittest.main()
