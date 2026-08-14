from __future__ import annotations

import unittest

from polymath_1M.screening.edge_diagnostics import (
    EXPECTED_SUCCESSFUL_IDS,
    build_trade_ledger,
    load_edge_diagnostic_config,
    select_successful_configuration_ids,
    summarize_filled_rows,
)


class EdgeDiagnosticTest(unittest.TestCase):
    def test_frozen_config_uses_expected_success_rule_and_bins(self) -> None:
        config = load_edge_diagnostic_config(
            "cfg/experiments/stage4k_edge_anti_edge.json"
        )
        self.assertEqual(config.successful_configuration_ids, EXPECTED_SUCCESSFUL_IDS)
        self.assertEqual(config.cost_scenarios_per_share, (0.0, 0.01, 0.02))
        self.assertEqual(config.minimum_segment_fills, 20)
        self.assertEqual(config.minimum_transfer_fills_per_split, 10)
        self.assertTrue(config.preserve_signals_and_fills)

    def test_success_selection_is_dev_stress_or_profitable_final(self) -> None:
        results = [
            self._source_result("dev", 1.0, 0, 0.0, None),
            self._source_result("final", -1.0, 20, 0.1, 1.01),
            self._source_result("too_few", -1.0, 19, 0.1, 2.0),
            self._source_result("loss", -1.0, 30, -0.1, 0.9),
        ]
        self.assertEqual(
            select_successful_configuration_ids(results, 20), ("dev", "final")
        )

    def test_summary_separates_intrinsic_edge_and_cost_ladder(self) -> None:
        rows = [
            self._decision(
                side="UP",
                outcome=1.0,
                fill_vwap=0.6,
                fee=0.02,
                forecast=0.7,
                current_mid=0.60,
                previous_mid=0.55,
            ),
            self._decision(
                side="DOWN",
                outcome=0.0,
                fill_vwap=0.4,
                fee=0.01,
                forecast=0.5,
                current_mid=0.40,
                previous_mid=0.45,
            ),
        ]
        summary = summarize_filled_rows(rows)
        self.assertEqual(summary["fills"], 2)
        self.assertAlmostEqual(summary["zero_cost_pnl_usdc"], -0.03)
        self.assertAlmostEqual(summary["primary_1c_pnl_usdc"], -0.23)
        self.assertAlmostEqual(summary["stress_2c_pnl_usdc"], -0.43)
        self.assertAlmostEqual(summary["realized_edge_cents_per_share"], -0.15)
        self.assertAlmostEqual(summary["model_edge_cents_per_share"], 9.85)
        self.assertAlmostEqual(summary["forecast_error_cents_per_share"], -10.0)

        ledger = build_trade_ledger(rows)
        self.assertAlmostEqual(float(ledger.numeric["signed_move"][0]), 0.05)
        self.assertAlmostEqual(float(ledger.numeric["signed_move"][1]), 0.05)

    @staticmethod
    def _source_result(
        config_id: str,
        dev_stress: float,
        final_fills: int,
        final_pnl: float,
        final_pf: float | None,
    ) -> dict:
        return {
            "config_id": config_id,
            "development": {"stress_2c_net_pnl_usdc": dev_stress},
            "final_period": {
                "fills": final_fills,
                "net_pnl_usdc": final_pnl,
                "profit_factor": final_pf,
            },
        }

    @staticmethod
    def _decision(
        *,
        side: str,
        outcome: float,
        fill_vwap: float,
        fee: float,
        forecast: float,
        current_mid: float,
        previous_mid: float,
    ) -> dict[str, str]:
        shares = 10.0
        fill_cost = fill_vwap * shares
        gross = outcome * shares - fill_cost
        zero = gross - fee
        return {
            "config_id": "synthetic",
            "condition_id": f"condition-{side}",
            "split": "development_validation",
            "asset": "BTC",
            "side": side,
            "state_bin": "5",
            "filled": "True",
            "decision_s": "43200",
            "current_mid_up": str(current_mid),
            "previous_mid_up": str(previous_mid),
            "gross_pnl": str(gross),
            "platform_fee": str(fee),
            "fill_shares": str(shares),
            "fill_cost": str(fill_cost),
            "net_pnl": str(zero - 0.01 * shares),
            "stress_2c_net_pnl": str(zero - 0.02 * shares),
            "extra_cost": str(0.01 * shares),
            "outcome_side": str(outcome),
            "signal_ask": str(fill_vwap),
            "fill_vwap": str(fill_vwap),
            "forecast_probability": str(forecast),
            "net_edge": str(forecast - fill_vwap - fee / shares - 0.01),
            "persistence": "0.8",
            "support": "100",
        }


if __name__ == "__main__":
    unittest.main()
