from __future__ import annotations

import unittest

import torch

from polymath_1M.domain import DecisionBatch
from polymath_1M.screening.literal_markov import (
    SourceVariant,
    TransitionModel,
    evaluate_literal_markov,
    fit_transition_model,
    load_literal_markov_config,
)


class LiteralMarkovTest(unittest.TestCase):
    def test_config_preserves_both_conflicting_source_thresholds(self) -> None:
        config = load_literal_markov_config(
            "cfg/experiments/stage4f_literal_markov.json"
        )
        self.assertEqual(
            tuple(item.variant_id for item in config.variants),
            (
                "core_tau87",
                "b27_tau75",
            ),
        )
        self.assertEqual(config.variants[0].minimum_destination_persistence, 0.87)
        self.assertEqual(config.variants[1].minimum_destination_persistence, 0.75)
        self.assertEqual(len(config.state_bin_edges) - 1, 8)

    def test_transition_matrix_uses_both_binary_sides_without_smoothing(self) -> None:
        batch = self._batch(
            previous_up=(0.10, 0.10, 0.30),
            current_up=(0.10, 0.30, 0.30),
        )
        edges = torch.tensor([0.0, 0.25, 0.5, 0.75, 1.000001], dtype=torch.float64)
        model = fit_transition_model(batch, edges, pseudocount=0.0)
        self.assertEqual(model.row_support.tolist(), [2, 1, 1, 2])
        torch.testing.assert_close(
            model.matrix,
            torch.tensor(
                [
                    [0.5, 0.5, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.5, 0.5],
                ],
                dtype=torch.float64,
            ),
        )

    def test_entry_uses_destination_not_current_state_persistence(self) -> None:
        batch = self._batch(previous_up=(0.10,), current_up=(0.10,))
        edges = torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64)
        matrix = torch.tensor([[0.10, 0.90], [0.05, 0.95]], dtype=torch.float64)
        model = TransitionModel(
            edges=edges,
            counts=torch.tensor([[1, 9], [1, 19]], dtype=torch.int64),
            row_support=torch.tensor([10, 20], dtype=torch.int64),
            matrix=matrix,
            destination=torch.tensor([1, 1], dtype=torch.int64),
            maximum_probability=torch.tensor([0.90, 0.95], dtype=torch.float64),
            destination_persistence=torch.tensor([0.95, 0.95]),
        )
        result = evaluate_literal_markov(
            batch,
            model,
            self._variant(tau=0.87),
            minimum_row_transitions=1,
            target_notional_usdc=10.0,
            fee_rate=0.0,
            fee_exponent=1.0,
            fee_round_decimals=4,
            primary_extra_cost_per_share=0.0,
            stress_extra_cost_per_share=0.0,
        )
        self.assertTrue(result.signal.item())
        self.assertTrue(result.filled.item())
        self.assertEqual(result.side.item(), 0)
        self.assertAlmostEqual(result.destination_persistence.item(), 0.95)

    def test_core_gap_accepts_equality_while_b27_requires_strictly_more(self) -> None:
        batch = self._batch(previous_up=(0.10,), current_up=(0.10,))
        batch.asks[0, 0] = 0.75
        edges = torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64)
        matrix = torch.tensor([[0.875, 0.125], [0.125, 0.875]], dtype=torch.float64)
        model = TransitionModel(
            edges=edges,
            counts=torch.tensor([[9, 1], [1, 9]], dtype=torch.int64),
            row_support=torch.tensor([10, 10], dtype=torch.int64),
            matrix=matrix,
            destination=torch.tensor([0, 1], dtype=torch.int64),
            maximum_probability=torch.tensor([0.875, 0.875], dtype=torch.float64),
            destination_persistence=torch.tensor([0.875, 0.875]),
        )
        kwargs = {
            "minimum_row_transitions": 1,
            "target_notional_usdc": 10.0,
            "fee_rate": 0.0,
            "fee_exponent": 1.0,
            "fee_round_decimals": 4,
            "primary_extra_cost_per_share": 0.0,
            "stress_extra_cost_per_share": 0.0,
        }
        core = evaluate_literal_markov(
            batch,
            model,
            self._variant(tau=0.87, operator="greater_equal", gap=0.125),
            **kwargs,
        )
        b27 = evaluate_literal_markov(
            batch,
            model,
            self._variant(tau=0.75, operator="greater", gap=0.125),
            **kwargs,
        )
        self.assertTrue(core.signal.item())
        self.assertFalse(b27.signal.item())

    @staticmethod
    def _variant(
        *, tau: float, operator: str = "greater_equal", gap: float = 0.05
    ) -> SourceVariant:
        return SourceVariant(
            variant_id="unit",
            source="unit",
            minimum_ask=0.01,
            maximum_ask=0.99,
            minimum_gap=gap,
            gap_operator=operator,
            minimum_destination_persistence=tau,
        )

    @staticmethod
    def _batch(
        *, previous_up: tuple[float, ...], current_up: tuple[float, ...]
    ) -> DecisionBatch:
        count = len(previous_up)
        current = torch.tensor(current_up, dtype=torch.float64)
        starts = torch.arange(count, dtype=torch.int64) * 300
        asks_up = torch.full((count,), 0.80, dtype=torch.float64)
        asks_down = torch.full((count,), 0.90, dtype=torch.float64)
        asks = torch.stack((asks_up, asks_down), dim=1)
        return DecisionBatch(
            condition_ids=tuple(f"market-{index}" for index in range(count)),
            assets=("BTC",) * count,
            market_start_s=starts,
            market_end_s=starts + 300,
            decision_s=starts + 240,
            previous_s=starts + 180,
            outcome_up=torch.ones(count, dtype=torch.float64),
            n_ticks=torch.full((count,), 300, dtype=torch.int64),
            previous_mid_up=torch.tensor(previous_up, dtype=torch.float64),
            current_mid_up=current,
            current_mid=torch.stack((current, 1 - current), dim=1),
            bids=asks - 0.01,
            asks=asks,
            ask_depth_prices=asks.unsqueeze(2),
            ask_depth_sizes=torch.full((count, 2, 1), 20.0, dtype=torch.float64),
            snapshot_valid=torch.ones(count, dtype=torch.bool),
            label_source="unit_test",
        )


if __name__ == "__main__":
    unittest.main()
