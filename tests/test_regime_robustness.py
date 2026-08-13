from __future__ import annotations

import unittest

from polymath_1M.screening.regime_robustness import (
    load_early_robustness_config,
)


class RegimeRobustnessTest(unittest.TestCase):
    def test_frozen_early_folds_are_contiguous(self) -> None:
        config = load_early_robustness_config(
            "cfg/experiments/stage4e_trent_robustness.json"
        )
        self.assertEqual(len(config.folds), 3)
        self.assertEqual(
            config.variants,
            ("m0_coarse_lookup_10c", "m6_chainlink_regime_decay_lagged"),
        )
        self.assertEqual(
            config.folds[-1].validation_end_exclusive_s,
            config.period_end_exclusive_s,
        )


if __name__ == "__main__":
    unittest.main()
