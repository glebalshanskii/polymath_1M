from __future__ import annotations

import unittest

import torch

from polymath_1M.historical.binance import (
    BinanceKlines,
    _epoch_seconds,
    load_binance_context_config,
)
from polymath_1M.screening.time_chart import TimeChartError, _match_decision_opens


class BinanceContextTest(unittest.TestCase):
    def test_frozen_config_is_visualization_only(self) -> None:
        config = load_binance_context_config(
            "cfg/datasets/binance_btcusdt_1m_202604_202605.json"
        )
        self.assertEqual(config.symbol, "BTCUSDT")
        self.assertEqual(config.interval, "1m")
        self.assertTrue(config.context_only)
        self.assertEqual(len(config.files), 14)
        self.assertFalse(any("2026-05-14" in item.url for item in config.files))

    def test_epoch_seconds_accepts_post_2025_microseconds_and_milliseconds(
        self,
    ) -> None:
        self.assertEqual(_epoch_seconds(1_776_816_000_000_000), 1_776_816_000)
        self.assertEqual(_epoch_seconds(1_776_816_000_000), 1_776_816_000)

    def test_decision_price_uses_exact_minute_open(self) -> None:
        klines = BinanceKlines(
            open_time_s=torch.tensor([120, 180, 240], dtype=torch.int64),
            open=torch.tensor([10.0, 11.0, 12.0], dtype=torch.float64),
            high=torch.tensor([10.5, 11.5, 12.5], dtype=torch.float64),
            low=torch.tensor([9.5, 10.5, 11.5], dtype=torch.float64),
            close=torch.tensor([10.2, 11.2, 12.2], dtype=torch.float64),
        )
        actual = _match_decision_opens(
            torch.tensor([120, 240], dtype=torch.int64), klines
        )
        torch.testing.assert_close(
            actual, torch.tensor([10.0, 12.0], dtype=torch.float64)
        )
        with self.assertRaises(TimeChartError):
            _match_decision_opens(torch.tensor([121], dtype=torch.int64), klines)


if __name__ == "__main__":
    unittest.main()
