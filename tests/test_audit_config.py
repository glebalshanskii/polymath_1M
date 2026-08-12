from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymath_1M.audit.config import load_config


class AuditConfigTest(unittest.TestCase):
    def test_loads_and_normalizes_address(self) -> None:
        payload = {
            "audit_id": "test",
            "period_start": "2026-03-01T00:00:00Z",
            "period_end": "2026-04-01T00:00:00Z",
            "window_days": 30,
            "pnl_tolerance_usdc": 1,
            "biggest_win_tolerance_usdc": 1,
            "accounts": [
                {
                    "id": "a",
                    "source_address": "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "expected_name": "A",
                    "claimed_pnl_usdc": 1,
                    "claimed_predictions": 2,
                    "claimed_biggest_win_usdc": 3,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = load_config(path)

        self.assertEqual(config.accounts[0].source_address, "0x" + "a" * 40)
        self.assertEqual(config.start_epoch, 1_772_323_200)
        self.assertTrue(config.fetch_trades)
        self.assertTrue(config.accounts[0].fetch_trade_crosscheck)

    def test_rejects_duplicate_accounts(self) -> None:
        account = {
            "id": "same",
            "source_address": "0x" + "1" * 40,
            "expected_name": "A",
            "claimed_pnl_usdc": 1,
            "claimed_predictions": 2,
            "claimed_biggest_win_usdc": 3,
        }
        payload = {
            "audit_id": "test",
            "period_start": "2026-03-01T00:00:00Z",
            "period_end": "2026-04-01T00:00:00Z",
            "window_days": 30,
            "pnl_tolerance_usdc": 1,
            "biggest_win_tolerance_usdc": 1,
            "accounts": [account, account],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate account"):
                load_config(path)

    def test_rejects_period_shorter_than_window(self) -> None:
        payload = {
            "audit_id": "test",
            "period_start": "2026-03-01T00:00:00Z",
            "period_end": "2026-03-02T00:00:00Z",
            "window_days": 30,
            "pnl_tolerance_usdc": 1,
            "biggest_win_tolerance_usdc": 1,
            "accounts": [
                {
                    "id": "a",
                    "source_address": "0x" + "1" * 40,
                    "expected_name": "A",
                    "claimed_pnl_usdc": 1,
                    "claimed_predictions": 2,
                    "claimed_biggest_win_usdc": 3,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "complete audit window"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
