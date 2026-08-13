from __future__ import annotations

import unittest

import torch

from polymath_1M.domain import DecisionBatch
from polymath_1M.historical.polymarket_chainlink import PolymarketChainlinkSeries
from polymath_1M.screening.regime_models import (
    _diagnostic_rows,
    _evaluate,
    _uniform_edges,
    chainlink_features,
    fit_logistic_model,
    load_regime_config,
    predict_logistic,
)
from polymath_1M.strategy.model import Evaluation, fit_lookup_model


class RegimeModelTest(unittest.TestCase):
    def test_frozen_config_has_contiguous_development_folds(self) -> None:
        config = load_regime_config("cfg/experiments/stage4e_regime_models.json")
        self.assertEqual(len(config.folds), 6)
        self.assertEqual(
            config.folds[-1].validation_end_exclusive_s,
            config.development_end_exclusive_s,
        )
        self.assertLess(
            config.development_end_exclusive_s, config.holdout_end_exclusive_s
        )

    def test_logistic_forecast_is_monotonic_in_market_logit(self) -> None:
        features = torch.tensor(
            [[-2.0], [-1.0], [-0.5], [0.5], [1.0], [2.0]],
            dtype=torch.float64,
        )
        outcomes = torch.tensor([0, 0, 0, 1, 1, 1], dtype=torch.float64)
        model = fit_logistic_model(
            features,
            outcomes,
            torch.ones(6, dtype=torch.float64),
            ridge=0.001,
            maximum_iterations=30,
        )
        prediction = predict_logistic(model, features)
        self.assertGreaterEqual(model.coefficients[1].item(), 0)
        self.assertTrue(bool((prediction[1:] >= prediction[:-1]).all().item()))
        self.assertLess(prediction[0].item(), 0.5)
        self.assertGreater(prediction[-1].item(), 0.5)

    def test_chainlink_features_use_only_start_through_decision(self) -> None:
        start = 60
        path = torch.tensor([100.0, 101.0, 100.5, 102.0, 103.0, 999.0])
        series = PolymarketChainlinkSeries(
            timestamp_s=torch.arange(start, start + 360, 60, dtype=torch.int64),
            value=path.to(torch.float64),
        )
        batch = DecisionBatch(
            condition_ids=("one",),
            assets=("BTC",),
            market_start_s=torch.tensor([start], dtype=torch.int64),
            market_end_s=torch.tensor([start + 300], dtype=torch.int64),
            decision_s=torch.tensor([start + 240], dtype=torch.int64),
            previous_s=torch.tensor([start + 180], dtype=torch.int64),
            outcome_up=torch.tensor([1.0], dtype=torch.float64),
            n_ticks=torch.tensor([300], dtype=torch.int64),
            previous_mid_up=torch.tensor([0.55], dtype=torch.float64),
            current_mid_up=torch.tensor([0.60], dtype=torch.float64),
            current_mid=torch.tensor([[0.60, 0.40]], dtype=torch.float64),
            bids=torch.tensor([[0.59, 0.39]], dtype=torch.float64),
            asks=torch.tensor([[0.61, 0.41]], dtype=torch.float64),
            ask_depth_prices=torch.tensor([[[0.61], [0.41]]], dtype=torch.float64),
            ask_depth_sizes=torch.tensor([[[20.0], [20.0]]], dtype=torch.float64),
            snapshot_valid=torch.tensor([True]),
            label_source="unit_test",
        )
        actual = chainlink_features(batch, series)
        self.assertEqual(actual.shape, (1, 3))
        self.assertAlmostEqual(
            actual[0, 0].item(), torch.log(torch.tensor(103 / 100)).item()
        )
        modified = PolymarketChainlinkSeries(
            series.timestamp_s, path.to(torch.float64).clone()
        )
        modified.value[-1] = 1.0
        torch.testing.assert_close(chainlink_features(batch, modified), actual)

    def test_lagged_chainlink_features_do_not_read_decision_minute(self) -> None:
        start = 120
        timestamps = torch.arange(60, 480, 60, dtype=torch.int64)
        original = PolymarketChainlinkSeries(
            timestamps,
            torch.tensor([99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0]),
        )
        batch = self._one_market(start)
        first = chainlink_features(batch, original, causal_lag_seconds=60)
        changed = original.value.clone()
        changed[-2:] = torch.tensor([1_000.0, 2_000.0])
        second = chainlink_features(
            batch,
            PolymarketChainlinkSeries(timestamps, changed),
            causal_lag_seconds=60,
        )
        torch.testing.assert_close(first, second)

    def test_diagnostic_side_label_is_not_overwritten_by_numeric_code(self) -> None:
        batch = self._one_market(120)
        vector_float = torch.tensor([0.5], dtype=torch.float64)
        vector_int = torch.tensor([0], dtype=torch.int64)
        result = Evaluation(
            state_bin=vector_int,
            probability=vector_float,
            support=vector_int,
            persistence=vector_float,
            side=vector_int,
            signal_ask=vector_float,
            fill_vwap=vector_float,
            fill_cost=vector_float,
            platform_fee=vector_float,
            net_edge=vector_float,
            fill_shares=vector_float,
            gross_pnl=vector_float,
            extra_cost=vector_float,
            net_pnl=vector_float,
            filled=torch.tensor([True]),
            status_code=vector_int,
        )
        row = _diagnostic_rows(
            "unit",
            "fold",
            batch,
            result,
            torch.zeros((1, 1), dtype=torch.float64),
            ("feature",),
        )[0]
        self.assertEqual(row["side"], "UP")
        self.assertEqual(row["side_code"], 0)

    def test_nonfinite_forecast_marks_snapshot_invalid(self) -> None:
        batch = self._one_market(120)
        config = load_regime_config("cfg/experiments/stage4e_regime_models.json")
        model = fit_lookup_model(
            batch,
            _uniform_edges(config.coarse_price_bin_width, torch.device("cpu")),
            terminal_alpha=config.terminal_alpha,
            transition_alpha=config.transition_alpha,
        )
        result = _evaluate(
            batch,
            model,
            torch.tensor([float("nan")], dtype=torch.float64),
            config,
        )
        self.assertFalse(result.filled.item())
        self.assertEqual(result.status_code.item(), 1)

    @staticmethod
    def _one_market(start: int) -> DecisionBatch:
        return DecisionBatch(
            condition_ids=("one",),
            assets=("BTC",),
            market_start_s=torch.tensor([start], dtype=torch.int64),
            market_end_s=torch.tensor([start + 300], dtype=torch.int64),
            decision_s=torch.tensor([start + 240], dtype=torch.int64),
            previous_s=torch.tensor([start + 180], dtype=torch.int64),
            outcome_up=torch.tensor([1.0], dtype=torch.float64),
            n_ticks=torch.tensor([300], dtype=torch.int64),
            previous_mid_up=torch.tensor([0.55], dtype=torch.float64),
            current_mid_up=torch.tensor([0.60], dtype=torch.float64),
            current_mid=torch.tensor([[0.60, 0.40]], dtype=torch.float64),
            bids=torch.tensor([[0.59, 0.39]], dtype=torch.float64),
            asks=torch.tensor([[0.61, 0.41]], dtype=torch.float64),
            ask_depth_prices=torch.tensor([[[0.61], [0.41]]], dtype=torch.float64),
            ask_depth_sizes=torch.tensor([[[20.0], [20.0]]], dtype=torch.float64),
            snapshot_valid=torch.tensor([True]),
            label_source="unit_test",
        )


if __name__ == "__main__":
    unittest.main()
