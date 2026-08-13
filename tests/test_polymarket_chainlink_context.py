from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import torch

from polymath_1M.historical.polymarket_chainlink import (
    PolymarketChainlinkSeries,
    RequestSpec,
    _parse_response,
    _request_specs,
    load_polymarket_chainlink_config,
)
from polymath_1M.screening.time_chart import (
    TimeChartError,
    _match_chainlink_values,
)


class PolymarketChainlinkContextTest(unittest.TestCase):
    def test_config_stops_before_stage4d_holdout(self) -> None:
        config = load_polymarket_chainlink_config(
            "cfg/datasets/polymarket_chainlink_btcusd_1m_stage4d.json"
        )
        self.assertEqual(config.symbol, "BTC")
        self.assertEqual(config.variant, "hourly")
        self.assertTrue(config.context_only)
        specs = _request_specs(config)
        self.assertEqual(len(specs), 384)
        self.assertEqual(
            datetime.fromtimestamp(specs[0].start_s, tz=UTC).isoformat(),
            "2026-04-28T00:00:00+00:00",
        )
        self.assertEqual(
            datetime.fromtimestamp(specs[-1].end_s, tz=UTC).isoformat(),
            "2026-05-13T23:59:00+00:00",
        )
        self.assertLess(specs[-1].end_s, int(config.period_end_exclusive.timestamp()))

    def test_response_requires_complete_inclusive_minute_series(self) -> None:
        spec = RequestSpec(index=0, start_s=60, end_s=3_660, url="fixture")
        raw = json.dumps(
            [
                {"timestamp": timestamp * 1_000, "value": 50_000 + timestamp}
                for timestamp in range(60, 3_661, 60)
            ]
        ).encode()
        timestamps, values = _parse_response(raw, spec)
        self.assertEqual(len(timestamps), 61)
        self.assertEqual(timestamps[0], 60)
        self.assertEqual(timestamps[-1], 3_660)
        self.assertEqual(values[-1], 53_660)

    def test_config_rejects_unexpected_endpoint(self) -> None:
        source = Path("cfg/datasets/polymarket_chainlink_btcusd_1m_stage4d.json")
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["endpoint"] = "https://example.com/history"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "frozen hourly"):
                load_polymarket_chainlink_config(path)

    def test_decision_price_requires_exact_chainlink_minute(self) -> None:
        series = PolymarketChainlinkSeries(
            timestamp_s=torch.tensor([60, 120, 180], dtype=torch.int64),
            value=torch.tensor([50_001.0, 50_002.0, 50_003.0]),
        )
        actual = _match_chainlink_values(
            torch.tensor([60, 180], dtype=torch.int64), series
        )
        torch.testing.assert_close(actual, torch.tensor([50_001.0, 50_003.0]))
        with self.assertRaises(TimeChartError):
            _match_chainlink_values(torch.tensor([61], dtype=torch.int64), series)


if __name__ == "__main__":
    unittest.main()
