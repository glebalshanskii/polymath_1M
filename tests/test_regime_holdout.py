from __future__ import annotations

import unittest

from polymath_1M.screening.regime_holdout import (
    HoldoutGate,
    _gate,
    load_selected_regime_config,
)


class RegimeHoldoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = HoldoutGate(
            minimum_fills=50,
            minimum_profit_factor=1.10,
            require_positive_net_pnl=True,
            require_positive_stress_2c_pnl=True,
        )

    def test_selected_config_freezes_candidate_and_gate(self) -> None:
        config = load_selected_regime_config(
            "cfg/experiments/stage4e_selected.json"
        )
        self.assertEqual(
            config.selected_variant, "m6_chainlink_regime_decay_lagged"
        )
        self.assertEqual(config.holdout_gate, self.gate)

    def test_gate_requires_every_condition(self) -> None:
        passing = {
            "fills": 50,
            "net_pnl_usdc": 1.0,
            "profit_factor": 1.10,
            "profit_factor_infinite": False,
            "stress_2c_net_pnl_usdc": 0.01,
        }
        status, checks = _gate(passing, self.gate)
        self.assertEqual(status, "pass")
        self.assertTrue(all(checks.values()))

        for key, value, failed_check in (
            ("fills", 49, "minimum_fills"),
            ("net_pnl_usdc", 0.0, "positive_net_pnl"),
            ("profit_factor", 1.099, "minimum_profit_factor"),
            ("stress_2c_net_pnl_usdc", 0.0, "positive_stress_2c_pnl"),
        ):
            with self.subTest(key=key):
                failing = {**passing, key: value}
                status, checks = _gate(failing, self.gate)
                self.assertEqual(status, "fail")
                self.assertFalse(checks[failed_check])


if __name__ == "__main__":
    unittest.main()
