from __future__ import annotations

import unittest

import torch

from polymath_1M.domain import DecisionBatch
from polymath_1M.screening.positive_retest import (
    EXPECTED_CONFIG_IDS,
    _persistence_threshold,
    load_positive_retest_config,
    token_features,
)
from polymath_1M.strategy.model import LookupModel


class PositiveRetestTest(unittest.TestCase):
    def test_frozen_config_contains_exact_positive_model_set(self) -> None:
        config = load_positive_retest_config(
            "cfg/experiments/stage4i_pmxt_positive_retest.json"
        )
        self.assertEqual(
            tuple(item.config_id for item in config.configurations),
            EXPECTED_CONFIG_IDS,
        )
        self.assertEqual(len(config.folds), 4)
        self.assertEqual(
            config.data_config,
            "cfg/experiments/stage4i_pmxt_market_data.json",
        )
        self.assertTrue(
            all(
                fold.train_end_exclusive_s == fold.validation_start_s
                for fold in config.folds
            )
        )
        self.assertEqual(config.primary_extra_cost_per_share, 0.01)
        self.assertEqual(config.stress_extra_cost_per_share, 0.02)

    def test_token_features_are_path_aware_over_one_minute(self) -> None:
        batch = self._batch()
        features, names = token_features(batch)
        self.assertEqual(features.shape, (2, 5))
        self.assertEqual(names[1], "mid_change_1m")
        torch.testing.assert_close(
            features[:, 1], torch.tensor([0.1, -0.1], dtype=torch.float64)
        )
        torch.testing.assert_close(
            features[:, 2], torch.tensor([0.02, 0.02], dtype=torch.float64)
        )
        torch.testing.assert_close(
            features[:, 4], torch.tensor([0.0, 0.0], dtype=torch.float64)
        )

    def test_train_quantile_and_fixed_persistence_are_distinct(self) -> None:
        config = load_positive_retest_config(
            "cfg/experiments/stage4i_pmxt_positive_retest.json"
        )
        model = LookupModel(
            edges=torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64),
            probability_up=torch.tensor([0.4, 0.6], dtype=torch.float64),
            support=torch.tensor([10, 10]),
            transition_matrix=torch.tensor(
                [[0.2, 0.8], [0.4, 0.6]], dtype=torch.float64
            ),
            persistence=torch.tensor([0.2, 0.6], dtype=torch.float64),
            prior_up=torch.tensor(0.5, dtype=torch.float64),
        )
        self.assertAlmostEqual(
            _persistence_threshold(config.configurations[1], self._batch(), model),
            0.3,
        )
        self.assertAlmostEqual(
            _persistence_threshold(config.configurations[6], self._batch(), model),
            0.14,
        )

    @staticmethod
    def _batch() -> DecisionBatch:
        return DecisionBatch(
            condition_ids=("a", "b"),
            assets=("BTC", "BTC"),
            market_start_s=torch.tensor([0, 300], dtype=torch.int64),
            market_end_s=torch.tensor([300, 600], dtype=torch.int64),
            decision_s=torch.tensor([240, 540], dtype=torch.int64),
            previous_s=torch.tensor([180, 480], dtype=torch.int64),
            outcome_up=torch.tensor([1.0, 0.0], dtype=torch.float64),
            n_ticks=torch.tensor([6, 6], dtype=torch.int64),
            previous_mid_up=torch.tensor([0.2, 0.8], dtype=torch.float64),
            current_mid_up=torch.tensor([0.3, 0.7], dtype=torch.float64),
            current_mid=torch.tensor([[0.3, 0.7], [0.7, 0.3]], dtype=torch.float64),
            bids=torch.tensor([[0.29, 0.69], [0.69, 0.29]], dtype=torch.float64),
            asks=torch.tensor([[0.31, 0.71], [0.71, 0.31]], dtype=torch.float64),
            ask_depth_prices=torch.tensor(
                [[[0.31], [0.71]], [[0.71], [0.31]]], dtype=torch.float64
            ),
            ask_depth_sizes=torch.full((2, 2, 1), 20.0, dtype=torch.float64),
            snapshot_valid=torch.tensor([True, True]),
            label_source="test",
        )


if __name__ == "__main__":
    unittest.main()
