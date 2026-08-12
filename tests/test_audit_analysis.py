from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from polymath_1M.audit.analysis import analyze_account_db
from polymath_1M.audit.config import AccountClaim, AuditConfig
from polymath_1M.audit.storage import AuditStorage


class AuditAnalysisTest(unittest.TestCase):
    def test_joint_match_uses_same_window_and_count_definition(self) -> None:
        start = datetime(2026, 3, 1, tzinfo=UTC)
        config = AuditConfig(
            audit_id="test",
            period_start=start,
            period_end=datetime(2026, 3, 3, 23, 59, 59, tzinfo=UTC),
            window_days=2,
            pnl_tolerance_usdc=0.01,
            biggest_win_tolerance_usdc=0.01,
            fetch_trades=True,
            fetch_closed_positions=True,
            accounts=(),
        )
        claim = AccountClaim(
            id="a",
            source_address="0x" + "1" * 40,
            expected_name="A",
            claimed_pnl_usdc=13.0,
            claimed_predictions=2,
            claimed_biggest_win_usdc=10.0,
            fetch_trade_crosscheck=True,
        )
        first_day = int(start.timestamp()) + 100
        activity = [
            {
                "timestamp": first_day,
                "type": "TRADE",
                "side": "BUY",
                "price": 0.7,
                "size": 10,
                "usdcSize": 7,
                "conditionId": "c1",
                "asset": "t1",
                "outcome": "Up",
                "title": "Bitcoin Up or Down 5 Minutes",
            },
            {
                "timestamp": first_day + 1,
                "type": "TRADE",
                "side": "BUY",
                "price": 0.8,
                "size": 10,
                "usdcSize": 8,
                "conditionId": "c2",
                "asset": "t2",
                "outcome": "Up",
                "title": "Ethereum Up or Down 15 Minutes",
            },
        ]
        settlements = [
            {
                "timestamp": first_day + 2,
                "type": "REDEEM",
                "conditionId": "c1",
                "usdcSize": 17,
            },
            {
                "timestamp": first_day + 3,
                "type": "REDEEM",
                "conditionId": "c2",
                "usdcSize": 10,
            },
            {
                "timestamp": first_day + 4,
                "type": "MAKER_REBATE",
                "usdcSize": 1,
            },
        ]

        with TemporaryDirectory() as directory:
            storage = AuditStorage(Path(directory))
            storage.store_rows("activity", claim.id, activity + settlements)
            storage.store_rows("trades", claim.id, activity)
            storage.mark_collection_complete(claim.id, "activity", 5)
            storage.mark_collection_complete(claim.id, "trades", 2)
            summary, windows, behavior = analyze_account_db(
                config,
                claim,
                {"proxyWallet": claim.source_address, "name": "A"},
                storage.connection,
            )
            storage.close()

        self.assertEqual(summary["claim_status"], "matched_public_ledger")
        self.assertTrue(summary["trade_api_complete"])
        self.assertFalse(summary["closed_positions_complete"])
        self.assertTrue(windows[0]["joint_match"])
        self.assertEqual(
            windows[0]["matching_pnl_definitions"],
            "settled_market_cashflow_plus_rewards",
        )
        self.assertIn(
            "trade_activity_rows", windows[0]["matching_prediction_definitions"]
        )
        self.assertEqual(behavior["asset_counts"], {"BTC": 1, "ETH": 1})
        self.assertEqual(behavior["duration_counts"], {"15m": 1, "5m": 1})


if __name__ == "__main__":
    unittest.main()
