from __future__ import annotations

import unittest

import torch

from polymath_1M.screening.config import load_screening_config
from polymath_1M.screening.pmxt_dataset import _valid_top_row
from polymath_1M.screening.run import chronological_market_splits


class ScreeningTest(unittest.TestCase):
    def test_frozen_config_loads(self) -> None:
        config = load_screening_config("cfg/experiments/stage4_pmxt_screening.json")
        self.assertEqual(config.device, "cuda")
        self.assertEqual(len(config.strategy_configs), 5)
        self.assertEqual(config.cost_scenarios_per_share, (0.005, 0.01, 0.02))

    def test_split_never_separates_equal_market_start(self) -> None:
        starts = torch.tensor([1, 1, 2, 2, 3, 3, 4, 4, 5, 5], dtype=torch.int64)
        splits = chronological_market_splits(starts, 0.6, 0.2)
        memberships: dict[int, set[str]] = {}
        for name, indices in splits.items():
            for index in indices.tolist():
                memberships.setdefault(int(starts[index].item()), set()).add(name)
        self.assertTrue(all(len(names) == 1 for names in memberships.values()))
        self.assertEqual(set(splits), {"train", "validation", "test"})

    def test_causal_top_accepts_zero_bid_but_rejects_zero_ask(self) -> None:
        snapshots = {
            (cutoff, outcome): {
                "best_bid": 0.0 if outcome == "Up" else 0.4,
                "best_ask": 0.01 if outcome == "Up" else 0.5,
                "receive_timestamp_ms": index,
            }
            for index, (cutoff, outcome) in enumerate(
                (cutoff, outcome)
                for cutoff in ("previous", "signal", "execution")
                for outcome in ("Up", "Down")
            )
        }
        valid = _valid_top_row({"condition_id": "condition"}, snapshots)
        self.assertTrue(valid["snapshot_valid"])
        snapshots[("execution", "Up")]["best_ask"] = 0.0
        invalid = _valid_top_row({"condition_id": "condition"}, snapshots)
        self.assertFalse(invalid["snapshot_valid"])
        self.assertEqual(invalid["invalid_reason"], "invalid_causal_top")


if __name__ == "__main__":
    unittest.main()
