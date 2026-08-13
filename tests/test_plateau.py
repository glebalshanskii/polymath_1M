from __future__ import annotations

import unittest

import torch

from polymath_1M.screening.plateau import (
    _masked_median,
    _plateau_members,
    _uniform_grid,
)
from polymath_1M.screening.plateau_config import load_plateau_config


class PlateauTest(unittest.TestCase):
    def test_frozen_config_has_uniform_absolute_grids(self) -> None:
        config = load_plateau_config("cfg/experiments/stage4d_uniform_plateau.json")
        self.assertEqual(config.minimum_support, 1)
        self.assertEqual(len(config.minimum_persistence_grid), 41)
        self.assertEqual(config.minimum_persistence_grid[:3], (0.10, 0.12, 0.14))
        self.assertEqual(config.holdout_start.isoformat(), "2026-05-14T00:00:00+00:00")

    def test_grid_has_expected_cells_and_local_members(self) -> None:
        config = load_plateau_config("cfg/experiments/stage4d_uniform_plateau.json")
        grid = _uniform_grid(config, torch.device("cpu"))
        members, exists = _plateau_members(grid)
        self.assertEqual(len(grid), 37_638)
        self.assertEqual(members.shape, (37_638, 9))
        self.assertGreaterEqual(int(exists.sum(dim=1).amin().item()), 5)
        self.assertEqual(int(exists.sum(dim=1).amax().item()), 9)
        interior = torch.nonzero(exists.sum(dim=1) == 9, as_tuple=False)[0].item()
        center = grid.indices[interior]
        neighbors = grid.indices[members[interior, 1:]]
        distances = torch.abs(neighbors - center).sum(dim=1)
        self.assertTrue(torch.equal(distances, torch.ones_like(distances)))

    def test_masked_median_averages_middle_pair(self) -> None:
        values = torch.tensor(
            [[1.0, 2.0, 10.0, 20.0], [4.0, 1.0, 3.0, 2.0]],
            dtype=torch.float64,
        )
        exists = torch.tensor(
            [[True, True, True, False], [True, True, True, True]]
        )
        self.assertEqual(_masked_median(values, exists).tolist(), [2.0, 2.5])


if __name__ == "__main__":
    unittest.main()
