from __future__ import annotations

import unittest

import torch

from polymath_1M.screening.calibration import (
    GridMetrics,
    ParameterGrid,
    _eligible,
    _neighbor_floor,
)
from polymath_1M.screening.calibration_config import load_calibration_config
from polymath_1M.screening.holdout import _test_gate, load_selected_config


class CalibrationTest(unittest.TestCase):
    def test_frozen_config_loads(self) -> None:
        config = load_calibration_config(
            "cfg/experiments/stage4b_signal_calibration.json"
        )
        self.assertEqual(config.device, "cuda")
        self.assertEqual(len(config.strategy_configs), 5)
        self.assertEqual(config.persistence_train_quantiles, (0.0, 0.25, 0.5, 0.75))
        self.assertGreater(
            config.stress_extra_cost_per_share,
            config.selection_extra_cost_per_share,
        )

    def test_selected_config_loads_without_opening_test(self) -> None:
        config = load_selected_config("cfg/experiments/stage4b_selected.json")
        self.assertEqual(config.selected["strategy_id"], "multi_asset_short_5m")
        self.assertEqual(config.selected["grid_indices"], [3, 0, 0, 0, 0])

    def test_neighbor_floor_ignores_only_missing_grid_neighbors(self) -> None:
        grid = ParameterGrid(
            indices=torch.tensor(
                [[0, 0, 0, 0, 0], [1, 0, 0, 0, 0], [2, 0, 0, 0, 0]],
                dtype=torch.int64,
            ),
            minimum_ask=torch.tensor([0.1, 0.2, 0.3], dtype=torch.float64),
            maximum_ask=torch.tensor([0.9, 0.9, 0.9], dtype=torch.float64),
            minimum_net_edge=torch.zeros(3, dtype=torch.float64),
            minimum_persistence=torch.zeros(3, dtype=torch.float64),
            minimum_support=torch.ones(3, dtype=torch.int64),
            persistence_values=torch.tensor([0.0], dtype=torch.float64),
        )
        floor = _neighbor_floor(
            grid, torch.tensor([4.0, 3.0, -1.0], dtype=torch.float64)
        )
        torch.testing.assert_close(
            floor, torch.tensor([3.0, -1.0, 3.0], dtype=torch.float64)
        )

    def test_eligibility_keeps_profitability_and_exposure_gates(self) -> None:
        config = load_calibration_config(
            "cfg/experiments/stage4b_signal_calibration.json"
        )
        metrics = GridMetrics(
            fill_count=torch.tensor([20, 19, 20]),
            fill_fraction=torch.tensor([0.2, 0.19, 0.2], dtype=torch.float64),
            net_pnl=torch.tensor([10.0, 10.0, -1.0], dtype=torch.float64),
            profit_factor=torch.tensor([1.2, 1.2, 1.2], dtype=torch.float64),
            max_drawdown=torch.tensor([5.0, 5.0, 5.0], dtype=torch.float64),
            half1_pnl=torch.tensor([4.0, 4.0, 4.0], dtype=torch.float64),
            half2_pnl=torch.tensor([6.0, 6.0, 6.0], dtype=torch.float64),
            stress_fill_count=torch.tensor([20, 19, 20]),
            stress_net_pnl=torch.tensor([3.0, 3.0, 3.0], dtype=torch.float64),
        )
        eligible = _eligible(
            metrics,
            torch.tensor([6.0, 6.0, 6.0], dtype=torch.float64),
            config,
            markets=100,
        )
        self.assertEqual(eligible.tolist(), [True, False, False])

    def test_holdout_gate_checks_exposure_before_profit(self) -> None:
        config = load_calibration_config(
            "cfg/experiments/stage4b_signal_calibration.json"
        )
        summary = {
            "markets": 100,
            "valid_snapshots": 100,
            "max_drawdown_usdc": 10.0,
            "maximum_positive_day_share": 0.5,
        }
        metrics = {
            "fills": 19,
            "net_pnl_usdc": -10.0,
            "profit_factor": 0.0,
            "profit_factor_infinite": False,
            "half1_pnl_usdc": -5.0,
            "half2_pnl_usdc": -5.0,
            "stress_fills": 0,
            "stress_net_pnl_usdc": -10.0,
        }
        status, adequacy, failures = _test_gate(summary, metrics, config)
        self.assertEqual(status, "inconclusive")
        self.assertEqual(adequacy, ["minimum_test_fills:20"])
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
