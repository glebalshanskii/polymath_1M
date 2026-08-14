from __future__ import annotations

import unittest

from polymath_1M.screening.cost_sensitivity import (
    load_cost_sensitivity_config,
    summarize_zero_extra_cost,
)


class CostSensitivityTest(unittest.TestCase):
    def test_frozen_config_preserves_signals_and_removes_only_extra_cost(self) -> None:
        config = load_cost_sensitivity_config(
            "cfg/experiments/stage4j_zero_extra_cost.json"
        )
        self.assertEqual(config.extra_cost_per_share, 0.0)
        self.assertEqual(config.minimum_final_fills, 20)
        self.assertTrue(config.preserve_platform_fee)
        self.assertTrue(config.preserve_signals_and_fills)

    def test_summary_keeps_fee_and_satisfies_zero_cost_identities(self) -> None:
        rows = [
            self._row(1.0, 0.6, 0.02, 10.0),
            self._row(0.0, 0.4, 0.01, 10.0),
            self._row(1.0, 0.5, 0.01, 10.0, filled=False),
        ]
        summary = summarize_zero_extra_cost(rows)
        self.assertEqual(summary["fills"], 2)
        self.assertAlmostEqual(summary["zero_extra_cost_pnl_usdc"], -0.03)
        self.assertAlmostEqual(summary["primary_1c_pnl_usdc"], -0.23)
        self.assertAlmostEqual(summary["stress_2c_pnl_usdc"], -0.43)
        self.assertAlmostEqual(summary["cash_turnover_usdc"], 10.03)

    @staticmethod
    def _row(
        payout: float,
        fill_vwap: float,
        platform_fee: float,
        shares: float,
        *,
        filled: bool = True,
    ) -> dict[str, str]:
        fill_cost = fill_vwap * shares if filled else 0.0
        gross = payout * shares - fill_cost if filled else 0.0
        fee = platform_fee if filled else 0.0
        extra = 0.01 * shares if filled else 0.0
        primary = gross - fee - extra
        stress = gross - fee - 0.02 * shares
        return {
            "filled": str(filled),
            "gross_pnl": str(gross),
            "platform_fee": str(fee),
            "fill_shares": str(shares if filled else 0.0),
            "fill_cost": str(fill_cost),
            "extra_cost": str(extra),
            "net_pnl": str(primary),
            "stress_2c_net_pnl": str(stress),
        }


if __name__ == "__main__":
    unittest.main()
