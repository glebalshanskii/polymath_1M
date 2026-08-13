from __future__ import annotations

import unittest

import torch

from polymath_1M.screening.calibration import GridMetrics, ParameterGrid
from polymath_1M.screening.walkforward import (
    _eligible,
    _gate_masks,
    _neighbor_positive_folds,
    _pool,
)
from polymath_1M.screening.walkforward_config import load_walkforward_config


def _metrics(fills: list[int], pnl: list[float], stress: list[float]) -> list[GridMetrics]:
    result = []
    for fold_fills, fold_pnl, fold_stress in zip(fills, pnl, stress, strict=True):
        net = torch.tensor([fold_pnl], dtype=torch.float64)
        profit = torch.tensor([max(fold_pnl + 10.0, 1.0)], dtype=torch.float64)
        loss = profit - net
        result.append(
            GridMetrics(
                fill_count=torch.tensor([fold_fills]),
                fill_fraction=torch.tensor([fold_fills / 1000], dtype=torch.float64),
                net_pnl=net,
                gross_profit=profit,
                gross_loss=loss,
                profit_factor=profit / loss,
                max_drawdown=torch.tensor([5.0], dtype=torch.float64),
                half1_pnl=net / 2,
                half2_pnl=net / 2,
                stress_fill_count=torch.tensor([fold_fills]),
                stress_net_pnl=torch.tensor([fold_stress], dtype=torch.float64),
            )
        )
    return result


class WalkForwardTest(unittest.TestCase):
    def test_frozen_config_has_contiguous_folds_and_new_holdout(self) -> None:
        config = load_walkforward_config("cfg/experiments/stage4c_walkforward.json")
        self.assertEqual(len(config.folds), 4)
        self.assertEqual(config.holdout_start.isoformat(), "2026-05-14T00:00:00+00:00")
        self.assertEqual(config.holdout_end_exclusive.isoformat(), "2026-05-18T00:00:00+00:00")
        self.assertGreater(config.folds[0].train_start.timestamp(), 1_774_310_400)

    def test_pool_preserves_fold_metrics(self) -> None:
        pooled = _pool(
            _metrics([25] * 4, [5.0, 4.0, 3.0, -1.0], [2.0] * 4),
            [1000] * 4,
        )
        self.assertEqual(pooled.pooled_fill_count.tolist(), [100])
        self.assertEqual(pooled.positive_folds.tolist(), [3])
        torch.testing.assert_close(
            pooled.pooled_net_pnl, torch.tensor([11.0], dtype=torch.float64)
        )

    def test_neighbor_fold_count_uses_existing_neighbors(self) -> None:
        grid = ParameterGrid(
            indices=torch.tensor(
                [[0, 0, 0, 0, 0], [1, 0, 0, 0, 0], [2, 0, 0, 0, 0]],
                dtype=torch.int64,
            ),
            minimum_ask=torch.tensor([0.2, 0.3, 0.4], dtype=torch.float64),
            maximum_ask=torch.tensor([0.9, 0.9, 0.9], dtype=torch.float64),
            minimum_net_edge=torch.zeros(3, dtype=torch.float64),
            minimum_persistence=torch.zeros(3, dtype=torch.float64),
            minimum_support=torch.ones(3, dtype=torch.int64),
            persistence_values=torch.tensor([0.0], dtype=torch.float64),
        )
        fold_pnl = torch.tensor(
            [
                [1.0, 1.0, -1.0],
                [1.0, 1.0, -1.0],
                [1.0, -1.0, 1.0],
                [1.0, -1.0, 1.0],
                [-1.0, -1.0, 1.0],
            ],
            dtype=torch.float64,
        )
        self.assertEqual(_neighbor_positive_folds(grid, fold_pnl).tolist(), [2, 3, 2])

    def test_eligibility_requires_three_profitable_folds_and_stress(self) -> None:
        config = load_walkforward_config("cfg/experiments/stage4c_walkforward.json")
        pooled = _pool(
            _metrics([25] * 4, [5.0, 4.0, 3.0, -1.0], [2.0] * 4),
            [1000] * 4,
        )
        eligible = _eligible(
            pooled,
            neighbor_floor=torch.tensor([10.0], dtype=torch.float64),
            neighbor_positive_folds=torch.tensor([3]),
            config=config,
            fold_markets=[1000] * 4,
        )
        self.assertEqual(eligible.tolist(), [True])
        gates = _gate_masks(
            pooled,
            neighbor_floor=torch.tensor([10.0], dtype=torch.float64),
            neighbor_positive_folds=torch.tensor([3]),
            config=config,
            fold_markets=[1000] * 4,
        )
        self.assertEqual(len(gates), 12)
        self.assertTrue(all(mask.tolist() == [True] for mask in gates.values()))


if __name__ == "__main__":
    unittest.main()
