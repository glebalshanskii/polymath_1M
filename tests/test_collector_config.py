from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymath_1M.collector.config import load_collector_config


class CollectorConfigTest(unittest.TestCase):
    def test_committed_config_has_complete_series_grid(self) -> None:
        config = load_collector_config("cfg/collectors/stage2_polymarket.json")

        self.assertEqual(len(config.series), 12)
        self.assertEqual(config.duration_seconds, 86_400)
        self.assertEqual(
            {(item.asset, item.duration) for item in config.series},
            {
                (asset, duration)
                for asset in ("BTC", "ETH", "SOL", "XRP")
                for duration in ("5m", "15m", "1h")
            },
        )

    def test_duration_override_is_validated(self) -> None:
        config = load_collector_config("cfg/collectors/stage2_polymarket.json")
        self.assertEqual(config.with_duration(12).duration_seconds, 12)
        with self.assertRaisesRegex(ValueError, "positive"):
            config.with_duration(0)

    def test_rejects_incomplete_series_grid(self) -> None:
        source = Path("cfg/collectors/stage2_polymarket.json")
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["series"].pop()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "series grid"):
                load_collector_config(path)


if __name__ == "__main__":
    unittest.main()
