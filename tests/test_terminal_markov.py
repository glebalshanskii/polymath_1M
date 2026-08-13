from __future__ import annotations

import unittest

import torch

from polymath_1M.domain import DecisionBatch
from polymath_1M.screening.terminal_markov import (
    TerminalMarkovConfig,
    TerminalModel,
    TerminalVariant,
    evaluate_terminal_markov,
    fit_terminal_model,
    load_terminal_markov_config,
)


class TerminalMarkovTest(unittest.TestCase):
    def test_config_freezes_market_anchor_ablation_and_candidate(self) -> None:
        config = load_terminal_markov_config(
            "cfg/experiments/stage4g_terminal_markov.json"
        )
        self.assertIsInstance(config, TerminalMarkovConfig)
        self.assertEqual(
            tuple(item.variant_id for item in config.variants),
            (
                "market_anchor_control",
                "current_state_control",
                "transition_pair_candidate",
            ),
        )
        self.assertEqual(config.minimum_persistence, 0.87)
        self.assertEqual(config.minimum_net_edge, 0.02)
        self.assertEqual(config.terminal_residual_prior_strength, 100.0)

    def test_fit_learns_terminal_residual_not_next_state_probability(self) -> None:
        batch = self._batch(
            previous_up=(0.20, 0.20),
            current_up=(0.20, 0.20),
            outcome_up=(1.0, 0.0),
        )
        edges = torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64)
        model = fit_terminal_model(
            batch,
            edges,
            self._variant("current_price_bin_terminal_residual"),
            transition_pseudocount=1.0,
            residual_prior_strength=2.0,
            maximum_absolute_residual=0.5,
        )
        self.assertEqual(model.state_support.tolist(), [2, 2])
        torch.testing.assert_close(
            model.residual,
            torch.tensor([0.15, -0.15], dtype=torch.float64),
        )
        torch.testing.assert_close(
            model.absorption_at_state_mean.sum(dim=1),
            torch.ones(2, dtype=torch.float64),
        )

    def test_transition_pair_distinguishes_paths_with_same_current_state(self) -> None:
        batch = self._batch(
            previous_up=(0.20, 0.70),
            current_up=(0.30, 0.30),
            outcome_up=(1.0, 0.0),
        )
        edges = torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64)
        model = fit_terminal_model(
            batch,
            edges,
            self._variant("previous_current_price_bin_terminal_residual"),
            transition_pseudocount=1.0,
            residual_prior_strength=1.0,
            maximum_absolute_residual=0.5,
        )
        self.assertEqual(model.state_support.tolist(), [1, 1, 1, 1])
        self.assertAlmostEqual(model.residual[0].item(), 0.35)
        self.assertAlmostEqual(model.residual[2].item(), -0.15)

    def test_market_anchor_does_not_create_fake_longshot_edge(self) -> None:
        batch = self._batch(
            previous_up=(0.08,),
            current_up=(0.08,),
            outcome_up=(0.0,),
            ask_up=0.08,
            ask_down=0.93,
        )
        model = self._model(residual=(0.0, 0.0))
        result = self._evaluate(
            batch,
            model,
            self._variant("market_mid_no_learned_residual", support=False),
        )
        self.assertFalse(result.signal.item())
        self.assertFalse(result.filled.item())
        self.assertAlmostEqual(result.forecast_probability.item(), 0.08)
        self.assertAlmostEqual(result.net_edge.item(), 0.0)

    def test_arrival_level_must_preserve_terminal_net_edge(self) -> None:
        batch = self._batch(
            previous_up=(0.75,),
            current_up=(0.75,),
            outcome_up=(1.0,),
            ask_up=0.75,
            ask_down=0.95,
            arrival_up=0.79,
        )
        model = self._model(residual=(0.0, 0.05))
        result = self._evaluate(
            batch,
            model,
            self._variant("current_price_bin_terminal_residual"),
        )
        self.assertTrue(result.signal.item())
        self.assertFalse(result.filled.item())
        self.assertAlmostEqual(result.forecast_probability.item(), 0.80)
        self.assertAlmostEqual(result.net_edge.item(), 0.05)

    def test_fill_level_and_pnl_use_terminal_payout(self) -> None:
        batch = self._batch(
            previous_up=(0.75,),
            current_up=(0.75,),
            outcome_up=(1.0,),
            ask_up=0.75,
            ask_down=0.95,
            arrival_up=0.77,
        )
        model = self._model(residual=(0.0, 0.05))
        result = self._evaluate(
            batch,
            model,
            self._variant("current_price_bin_terminal_residual"),
        )
        self.assertTrue(result.signal.item())
        self.assertTrue(result.filled.item())
        self.assertAlmostEqual(result.maximum_executed_price.item(), 0.77)
        self.assertGreater(result.primary_pnl.item(), 0)
        self.assertGreater(result.primary_pnl.item(), result.stress_pnl.item())

    @staticmethod
    def _variant(state_policy: str, *, support: bool = True) -> TerminalVariant:
        return TerminalVariant(
            variant_id="unit",
            state_policy=state_policy,
            apply_support_gate=support,
        )

    @staticmethod
    def _model(*, residual: tuple[float, float]) -> TerminalModel:
        transition = torch.tensor([[0.90, 0.10], [0.10, 0.90]], dtype=torch.float64)
        mean = torch.tensor([0.25, 0.75], dtype=torch.float64)
        correction = torch.tensor(residual, dtype=torch.float64)
        probability = torch.clamp(mean + correction, 0, 1)
        return TerminalModel(
            edges=torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64),
            state_policy="current_price_bin_terminal_residual",
            state_support=torch.tensor([100, 100], dtype=torch.int64),
            residual_sum=correction * 200,
            residual=correction,
            state_mean_mid=mean,
            absorption_at_state_mean=torch.stack((probability, 1 - probability), dim=1),
            transition_counts=torch.tensor([[90, 10], [10, 90]], dtype=torch.int64),
            transition_matrix=transition,
            persistence=torch.diagonal(transition),
        )

    def _evaluate(
        self,
        batch: DecisionBatch,
        model: TerminalModel,
        variant: TerminalVariant,
    ):
        if model.state_policy != variant.state_policy:
            model = TerminalModel(
                edges=model.edges,
                state_policy=variant.state_policy,
                state_support=model.state_support,
                residual_sum=model.residual_sum,
                residual=model.residual,
                state_mean_mid=model.state_mean_mid,
                absorption_at_state_mean=model.absorption_at_state_mean,
                transition_counts=model.transition_counts,
                transition_matrix=model.transition_matrix,
                persistence=model.persistence,
            )
        return evaluate_terminal_markov(
            batch,
            model,
            variant,
            minimum_state_support=1,
            minimum_probability=0.01,
            maximum_probability=0.99,
            minimum_ask=0.01,
            maximum_ask=0.99,
            minimum_persistence=0.87,
            minimum_net_edge=0.02,
            target_notional_usdc=10.0,
            fee_rate=0.0,
            fee_exponent=1.0,
            fee_round_decimals=4,
            primary_extra_cost_per_share=0.0,
            stress_extra_cost_per_share=0.01,
        )

    @staticmethod
    def _batch(
        *,
        previous_up: tuple[float, ...],
        current_up: tuple[float, ...],
        outcome_up: tuple[float, ...],
        ask_up: float = 0.20,
        ask_down: float = 0.81,
        arrival_up: float | None = None,
    ) -> DecisionBatch:
        count = len(previous_up)
        current = torch.tensor(current_up, dtype=torch.float64)
        starts = torch.arange(count, dtype=torch.int64) * 300
        asks = torch.tensor([[ask_up, ask_down]] * count, dtype=torch.float64)
        arrival = asks.clone()
        if arrival_up is not None:
            arrival[:, 0] = arrival_up
        return DecisionBatch(
            condition_ids=tuple(f"market-{index}" for index in range(count)),
            assets=("BTC",) * count,
            market_start_s=starts,
            market_end_s=starts + 300,
            decision_s=starts + 240,
            previous_s=starts + 180,
            outcome_up=torch.tensor(outcome_up, dtype=torch.float64),
            n_ticks=torch.full((count,), 300, dtype=torch.int64),
            previous_mid_up=torch.tensor(previous_up, dtype=torch.float64),
            current_mid_up=current,
            current_mid=torch.stack((current, 1 - current), dim=1),
            bids=asks - 0.01,
            asks=asks,
            ask_depth_prices=arrival.unsqueeze(2),
            ask_depth_sizes=torch.full((count, 2, 1), 20.0, dtype=torch.float64),
            snapshot_valid=torch.ones(count, dtype=torch.bool),
            label_source="unit_test",
        )


if __name__ == "__main__":
    unittest.main()
