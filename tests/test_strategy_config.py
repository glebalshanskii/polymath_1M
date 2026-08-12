from __future__ import annotations

import unittest
from pathlib import Path

from polymath_1M.strategy.parameters import load_strategy_config


class StrategyConfigTest(unittest.TestCase):
    def test_all_committed_configs_are_executable_and_unique(self) -> None:
        paths = sorted(Path("cfg/strategies").glob("*.json"))
        strategies = [load_strategy_config(path) for path in paths]
        identifiers = [strategy.strategy_id for strategy in strategies]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertTrue(
            {
                "favorite_hourly",
                "directional_mid_15m",
                "directional_mid_1h",
                "multi_asset_short_5m",
                "multi_asset_short_15m",
            }.issubset(identifiers)
        )


if __name__ == "__main__":
    unittest.main()
