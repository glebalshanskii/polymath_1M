from __future__ import annotations

import unittest

import pyarrow as pa
import torch

from polymath_1M.screening.practical_chain import (
    ChainDataset,
    _top_rows,
    _rebuild_depths,
    apply_execution,
    evaluate_signals,
    fit_raw_chain,
    load_practical_chain_config,
)


EDGES = torch.tensor(
    (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.000001),
    dtype=torch.float64,
)


def _dataset(mid: torch.Tensor, outcome: torch.Tensor) -> ChainDataset:
    count = mid.shape[0]
    asks_up = torch.clamp(mid + 0.01, max=0.99)
    asks_down = torch.clamp(1 - mid + 0.01, max=0.99)
    bids_up = torch.clamp(mid - 0.01, min=0.01)
    bids_down = torch.clamp(1 - mid - 0.01, min=0.01)
    return ChainDataset(
        condition_ids=tuple(f"market-{index}" for index in range(count)),
        token_up=tuple(f"up-{index}" for index in range(count)),
        token_down=tuple(f"down-{index}" for index in range(count)),
        market_start_ms=torch.arange(count, dtype=torch.int64) * 300_000,
        market_end_ms=(torch.arange(count, dtype=torch.int64) + 1) * 300_000,
        outcome_up=outcome,
        fee_rate=torch.zeros(count, dtype=torch.float64),
        fee_exponent=torch.ones(count, dtype=torch.float64),
        mid_up=mid,
        bids=torch.stack((bids_up, bids_down), dim=2),
        asks=torch.stack((asks_up, asks_down), dim=2),
        receive_timestamp_ms=torch.zeros((count, 8, 2), dtype=torch.int64),
        valid=torch.ones((count, 8), dtype=torch.bool),
    )


class PracticalChainTest(unittest.TestCase):
    def test_config_freezes_unsmoothed_120_second_contract(self) -> None:
        config = load_practical_chain_config(
            "cfg/experiments/stage4h_practical_chain.json"
        )
        self.assertEqual(
            config.state_seconds_before_end, (120, 105, 90, 75, 60, 45, 30, 15)
        )
        self.assertEqual(config.minimum_transition_support, 100)
        self.assertEqual(config.minimum_terminal_support, 100)
        self.assertEqual(config.primary_extra_cost_per_share, 0)

    def test_raw_chain_has_no_pseudocount_and_is_time_inhomogeneous(self) -> None:
        path = torch.tensor(
            [0.0625, 0.1875, 0.3125, 0.4375, 0.5625, 0.6875, 0.8125, 0.9375],
            dtype=torch.float64,
        )
        mid = path.repeat(4, 1)
        outcomes = torch.tensor((1.0, 1.0, 1.0, 0.0), dtype=torch.float64)
        model = fit_raw_chain(
            mid,
            torch.ones_like(mid, dtype=torch.bool),
            outcomes,
            EDGES,
            minimum_transition_support=1,
            minimum_terminal_support=1,
        )
        self.assertEqual(model.transition_counts[0, 0, 1].item(), 4)
        self.assertEqual(model.transition_counts[1, 1, 2].item(), 4)
        self.assertEqual(model.transition_matrix[0, 0, 1].item(), 1.0)
        self.assertEqual(model.transition_matrix[0, 0, 0].item(), 0.0)
        self.assertEqual(model.terminal_up[7].item(), 0.75)
        self.assertAlmostEqual(model.value_up[0, 0].item(), 0.75)

    def test_missing_raw_support_makes_forecast_unavailable(self) -> None:
        mid = torch.tensor(
            (
                (0.0625,) * 7 + (0.0625,),
                (0.0625,) * 7 + (0.1875,),
            ),
            dtype=torch.float64,
        )
        model = fit_raw_chain(
            mid,
            torch.ones_like(mid, dtype=torch.bool),
            torch.tensor((0.0, 1.0), dtype=torch.float64),
            EDGES,
            minimum_transition_support=2,
            minimum_terminal_support=2,
        )
        self.assertFalse(model.available[-1].any().item())
        self.assertFalse(model.available[0, 0].item())

    def test_chain_signal_uses_terminal_probability_and_first_checkpoint(self) -> None:
        train_mid = torch.full((4, 8), 0.5625, dtype=torch.float64)
        model = fit_raw_chain(
            train_mid,
            torch.ones_like(train_mid, dtype=torch.bool),
            torch.ones(4, dtype=torch.float64),
            EDGES,
            minimum_transition_support=1,
            minimum_terminal_support=1,
        )
        validation = _dataset(
            torch.full((1, 8), 0.5625, dtype=torch.float64),
            torch.ones(1, dtype=torch.float64),
        )
        result = evaluate_signals(validation, model, minimum_net_edge=0.02)
        self.assertTrue(result.signal.all().item())
        self.assertEqual(result.order_checkpoint.item(), 0)
        self.assertEqual(result.side[0, 0].item(), 0)
        self.assertEqual(result.probability_up[0, 0].item(), 1.0)

    def test_top_rows_forward_fill_only_from_receive_time(self) -> None:
        condition = "0x" + "ab" * 32
        market = {
            "condition_id": condition,
            "token_up": "up",
            "token_down": "down",
        }
        table = pa.Table.from_pylist(
            [
                {
                    "condition_id": condition,
                    "outcome": side,
                    "checkpoint_index": checkpoint,
                    "receive_timestamp_ms": timestamp,
                    "best_bid": bid,
                    "best_ask": ask,
                }
                for checkpoint, timestamp, values in (
                    (0, 1000, ((0.4, 0.5), (0.5, 0.6))),
                    (2, 3000, ((0.6, 0.7), (0.3, 0.4))),
                )
                for side, (bid, ask) in zip(("Up", "Down"), values, strict=True)
            ]
        )
        row, reason = _top_rows(table, market)
        self.assertEqual(reason, "valid")
        self.assertEqual(row["mid_up"][1], 0.45)
        self.assertAlmostEqual(row["mid_up"][2], 0.65)
        self.assertEqual(row["receive_timestamp_ms"][1][0], 1000)

    def test_execution_walk_respects_limit_and_fee(self) -> None:
        config = load_practical_chain_config(
            "cfg/experiments/stage4h_practical_chain.json"
        )
        order = {
            "variant": "practical_chain",
            "fold_id": "fold_0",
            "condition_id": "market",
            "market_start_ms": 0,
            "market_end_ms": 300_000,
            "outcome_up": 1.0,
            "fee_rate": 0.0,
            "fee_exponent": 1.0,
            "token_id": "up",
            "other_token_id": "down",
            "side": "Up",
            "checkpoint": 0,
            "seconds_before_end": 120,
            "decision_ms": 180_000,
            "arrival_ms": 181_000,
            "probability_up": 0.80,
            "probability_side": 0.80,
            "decision_ask": 0.70,
            "decision_edge": 0.10,
            "price_limit": 0.78,
        }
        rows = apply_execution(
            [order],
            {("market", "practical_chain"): ([0.70, 0.78, 0.79], [10.0, 10.0, 100.0])},
            config,
            torch.device("cpu"),
        )
        self.assertTrue(rows[0]["filled"])
        self.assertLessEqual(rows[0]["fill_cost"], 10.0)
        self.assertLessEqual(rows[0]["fill_vwap"], 0.78)
        self.assertGreater(rows[0]["primary_pnl"], 0)

    def test_execution_depth_rebuilds_direct_and_complement_books(self) -> None:
        snapshots = pa.Table.from_pylist(
            [
                {
                    "condition_id": "direct",
                    "variant": "practical_chain",
                    "levels": '[["0.40","2"],["0.50","3"]]',
                    "is_direct": True,
                },
                {
                    "condition_id": "complement",
                    "variant": "practical_chain",
                    "levels": '[["0.60","4"],["0.50","5"]]',
                    "is_direct": False,
                },
            ]
        )
        changes = pa.Table.from_pylist(
            [
                {
                    "condition_id": "direct",
                    "variant": "practical_chain",
                    "price": 0.40,
                    "size": 0.0,
                },
                {
                    "condition_id": "complement",
                    "variant": "practical_chain",
                    "price": 0.40,
                    "size": 7.0,
                },
            ]
        )
        depths = _rebuild_depths(snapshots, changes)
        self.assertEqual(depths[("direct", "practical_chain")], ([0.5], [3.0]))
        self.assertEqual(
            depths[("complement", "practical_chain")],
            ([0.4, 0.5], [7.0, 5.0]),
        )


if __name__ == "__main__":
    unittest.main()
