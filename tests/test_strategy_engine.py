from __future__ import annotations

import unittest
from dataclasses import replace

import torch

from polymath_1M.domain import DecisionBatch
from polymath_1M.strategy.model import (
    calculate_platform_fee,
    evaluate_batch,
    fit_lookup_model,
)


def _batch(outcome_up: torch.Tensor | None = None) -> DecisionBatch:
    count = 4
    outcomes = (
        outcome_up
        if outcome_up is not None
        else torch.tensor([1.0, 1.0, 0.0, 0.0], dtype=torch.float64)
    )
    bids = torch.tensor(
        [[0.38, 0.58], [0.39, 0.57], [0.58, 0.38], [0.57, 0.39]],
        dtype=torch.float64,
    )
    asks = bids + 0.02
    return DecisionBatch(
        condition_ids=("a", "b", "c", "d"),
        assets=("BTC",) * count,
        market_start_s=torch.arange(count, dtype=torch.int64),
        market_end_s=torch.arange(count, dtype=torch.int64) + 300,
        decision_s=torch.arange(count, dtype=torch.int64) + 240,
        previous_s=torch.arange(count, dtype=torch.int64) + 180,
        outcome_up=outcomes,
        n_ticks=torch.full((count,), 300, dtype=torch.int64),
        previous_mid_up=torch.tensor([0.39, 0.40, 0.59, 0.58]),
        current_mid_up=(bids[:, 0] + asks[:, 0]) / 2,
        current_mid=(bids + asks) / 2,
        bids=bids,
        asks=asks,
        ask_depth_prices=torch.tensor(
            [
                [[0.40, 0.42], [0.60, 0.62]],
                [[0.41, 0.43], [0.59, 0.61]],
                [[0.60, 0.62], [0.40, 0.42]],
                [[0.59, 0.61], [0.41, 0.43]],
            ],
            dtype=torch.float64,
        ),
        ask_depth_sizes=torch.full((count, 2, 2), 10.0, dtype=torch.float64),
        snapshot_valid=torch.ones(count, dtype=torch.bool),
        label_source="unit_test",
    )


class StrategyEngineTest(unittest.TestCase):
    def test_decision_batch_rejects_inconsistent_shapes(self) -> None:
        with self.assertRaisesRegex(ValueError, "current_mid"):
            replace(_batch(), current_mid=torch.zeros((4, 3)))

    def test_platform_fee_matches_official_example(self) -> None:
        shares = torch.tensor([[[100.0]]], dtype=torch.float64)
        prices = torch.tensor([[[0.5]]], dtype=torch.float64)
        fee = calculate_platform_fee(shares, prices, 0.07, decimals=5)
        self.assertEqual(fee.shape, (1, 1))
        self.assertAlmostEqual(fee.item(), 1.75)

    def test_platform_fee_accepts_per_market_rates(self) -> None:
        shares = torch.full((2, 2, 1), 100.0, dtype=torch.float64)
        prices = torch.full((2, 2, 1), 0.5, dtype=torch.float64)
        fee = calculate_platform_fee(
            shares,
            prices,
            torch.tensor([0.07, 0.0], dtype=torch.float64),
            decimals=5,
        )
        self.assertTrue(
            torch.equal(
                fee,
                torch.tensor([[1.75, 1.75], [0.0, 0.0]], dtype=torch.float64),
            )
        )

    def test_range_control_can_force_market_favorite_and_disable_model_gates(
        self,
    ) -> None:
        train = _batch()
        model = fit_lookup_model(
            train,
            torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64),
            terminal_alpha=1.0,
            transition_alpha=1.0,
        )
        one = train.index(torch.tensor([0], dtype=torch.int64))
        result = evaluate_batch(
            one,
            model,
            minimum_support=10_000,
            minimum_persistence=1.0,
            minimum_ask=0.01,
            maximum_ask=0.99,
            minimum_net_edge=1.0,
            target_notional_usdc=5.0,
            platform_fee_rate=torch.tensor([0.07], dtype=torch.float64),
            platform_fee_round_decimals=5,
            extra_cost_per_share=0.01,
            require_market_favorite=False,
            forced_side=torch.argmax(one.current_mid, dim=1),
            apply_support_gate=False,
            apply_persistence_gate=False,
            apply_edge_gate=False,
        )
        self.assertTrue(result.filled.item())
        self.assertEqual(result.side.item(), 1)

    def test_fak_walk_fee_and_settlement_pnl_have_analytical_oracle(self) -> None:
        train = _batch()
        edges = torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64)
        model = fit_lookup_model(train, edges, terminal_alpha=1.0, transition_alpha=1.0)
        one = train.index(torch.tensor([0], dtype=torch.int64))
        result = evaluate_batch(
            one,
            model,
            minimum_support=1,
            minimum_persistence=0.0,
            minimum_ask=0.01,
            maximum_ask=0.99,
            minimum_net_edge=0.0,
            target_notional_usdc=5.0,
            platform_fee_rate=0.07,
            platform_fee_round_decimals=5,
            extra_cost_per_share=0.01,
            require_market_favorite=False,
        )
        expected_shares = 10.0 + 1.0 / 0.42
        expected_fill_cost = 5.0
        expected_fee = round(
            10.0 * 0.07 * 0.40 * 0.60 + (1.0 / 0.42) * 0.07 * 0.42 * 0.58,
            5,
        )
        expected_pnl = (
            expected_shares - expected_fill_cost - expected_fee - expected_shares * 0.01
        )
        self.assertTrue(result.filled.item())
        self.assertEqual(result.side.item(), 0)
        self.assertAlmostEqual(result.fill_shares.item(), expected_shares)
        self.assertAlmostEqual(result.fill_cost.item(), expected_fill_cost)
        self.assertAlmostEqual(result.signal_ask.item(), 0.40)
        self.assertAlmostEqual(
            result.fill_vwap.item(), expected_fill_cost / expected_shares
        )
        self.assertAlmostEqual(result.platform_fee.item(), expected_fee)
        self.assertAlmostEqual(result.net_pnl.item(), expected_pnl)

    def test_future_settlement_does_not_change_past_decision(self) -> None:
        train = _batch()
        edges = torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64)
        model = fit_lookup_model(train, edges, terminal_alpha=1.0, transition_alpha=1.0)
        observed = train.index(torch.tensor([0], dtype=torch.int64))
        counterfactual = replace(
            observed, outcome_up=torch.zeros(1, dtype=torch.float64)
        )
        arguments = {
            "minimum_support": 1,
            "minimum_persistence": 0.0,
            "minimum_ask": 0.01,
            "maximum_ask": 0.99,
            "minimum_net_edge": 0.0,
            "target_notional_usdc": 5.0,
            "platform_fee_rate": 0.07,
            "platform_fee_round_decimals": 5,
            "extra_cost_per_share": 0.01,
            "require_market_favorite": False,
        }
        first = evaluate_batch(observed, model, **arguments)
        second = evaluate_batch(counterfactual, model, **arguments)
        self.assertTrue(torch.equal(first.side, second.side))
        self.assertTrue(torch.equal(first.filled, second.filled))
        self.assertTrue(torch.equal(first.net_edge, second.net_edge))
        self.assertNotEqual(first.net_pnl.item(), second.net_pnl.item())

    def test_execution_book_does_not_choose_the_order_side(self) -> None:
        train = _batch()
        edges = torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64)
        model = fit_lookup_model(train, edges, terminal_alpha=1.0, transition_alpha=1.0)
        observed = train.index(torch.tensor([0], dtype=torch.int64))
        worse_execution = replace(
            observed,
            ask_depth_prices=torch.tensor(
                [[[0.90, 0.91], [0.02, 0.03]]], dtype=torch.float64
            ),
        )
        arguments = {
            "minimum_support": 1,
            "minimum_persistence": 0.0,
            "minimum_ask": 0.01,
            "maximum_ask": 0.99,
            "minimum_net_edge": 0.0,
            "target_notional_usdc": 5.0,
            "platform_fee_rate": 0.07,
            "platform_fee_round_decimals": 5,
            "extra_cost_per_share": 0.01,
            "require_market_favorite": False,
        }
        first = evaluate_batch(observed, model, **arguments)
        second = evaluate_batch(worse_execution, model, **arguments)
        self.assertTrue(torch.equal(first.side, second.side))
        self.assertTrue(torch.equal(first.net_edge, second.net_edge))
        self.assertNotEqual(first.fill_vwap.item(), second.fill_vwap.item())

    def test_fak_never_walks_beyond_worst_price_limit(self) -> None:
        train = _batch()
        model = fit_lookup_model(
            train,
            torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64),
            terminal_alpha=1.0,
            transition_alpha=1.0,
        )
        result = evaluate_batch(
            train.index(torch.tensor([0], dtype=torch.int64)),
            model,
            minimum_support=1,
            minimum_persistence=0.0,
            minimum_ask=0.01,
            maximum_ask=0.41,
            minimum_net_edge=0.0,
            target_notional_usdc=5.0,
            platform_fee_rate=0.07,
            platform_fee_round_decimals=5,
            extra_cost_per_share=0.01,
            require_market_favorite=False,
        )
        self.assertTrue(result.filled.item())
        self.assertAlmostEqual(result.fill_shares.item(), 10.0)
        self.assertAlmostEqual(result.fill_cost.item(), 4.0)
        self.assertAlmostEqual(result.fill_vwap.item(), 0.4)

    @unittest.skipUnless(torch.cuda.is_available(), "CUDA device is unavailable")
    def test_cpu_and_cuda_decisions_match(self) -> None:
        train = _batch()
        edges = torch.tensor([0.0, 0.5, 1.000001], dtype=torch.float64)
        cpu_model = fit_lookup_model(
            train, edges, terminal_alpha=1.0, transition_alpha=1.0
        )
        cuda_train = train.to(torch.device("cuda"), torch.float64)
        cuda_model = fit_lookup_model(
            cuda_train,
            edges.to(device="cuda"),
            terminal_alpha=1.0,
            transition_alpha=1.0,
        )
        arguments = {
            "minimum_support": 1,
            "minimum_persistence": 0.0,
            "minimum_ask": 0.01,
            "maximum_ask": 0.99,
            "minimum_net_edge": 0.0,
            "target_notional_usdc": 5.0,
            "platform_fee_rate": 0.07,
            "platform_fee_round_decimals": 5,
            "extra_cost_per_share": 0.01,
            "require_market_favorite": False,
        }
        cpu = evaluate_batch(train, cpu_model, **arguments)
        cuda = evaluate_batch(cuda_train, cuda_model, **arguments)
        self.assertTrue(torch.equal(cpu.side, cuda.side.cpu()))
        self.assertTrue(torch.equal(cpu.filled, cuda.filled.cpu()))
        self.assertTrue(torch.allclose(cpu.net_edge, cuda.net_edge.cpu(), atol=1e-12))
        self.assertTrue(torch.allclose(cpu.net_pnl, cuda.net_pnl.cpu(), atol=1e-12))


if __name__ == "__main__":
    unittest.main()
