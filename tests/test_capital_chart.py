from __future__ import annotations

import unittest

import torch

from polymath_1M.screening.capital_chart import CapitalChartError, _capital_ledger


class CapitalChartTest(unittest.TestCase):
    def test_ledger_uses_available_capital_and_peak_equity(self) -> None:
        pnl = torch.tensor([10.0, -30.0, 5.0], dtype=torch.float64)
        positions = torch.tensor([10.0, 11.0, 8.0], dtype=torch.float64)
        ledger = _capital_ledger(pnl, positions, 100.0)
        self.assertEqual(ledger.capital_before.tolist(), [100.0, 110.0, 80.0])
        self.assertEqual(ledger.equity_after.tolist(), [110.0, 80.0, 85.0])
        self.assertEqual(ledger.equity_peak.tolist(), [110.0, 110.0, 110.0])
        self.assertEqual(ledger.drawdown.tolist(), [0.0, 30.0, 25.0])
        self.assertEqual(ledger.position_fraction.tolist(), [0.1, 0.1, 0.1])
        self.assertAlmostEqual(ledger.drawdown_fraction[1].item(), 30 / 110)

    def test_ledger_rejects_a_position_larger_than_available_capital(self) -> None:
        with self.assertRaisesRegex(CapitalChartError, "position exceeds"):
            _capital_ledger(
                torch.tensor([1.0], dtype=torch.float64),
                torch.tensor([101.0], dtype=torch.float64),
                100.0,
            )

    def test_position_fraction_uses_cash_outlay_supplied_by_replay(self) -> None:
        ledger = _capital_ledger(
            torch.tensor([-2.0, 3.0], dtype=torch.float64),
            torch.tensor([9.0, 10.0], dtype=torch.float64),
            100.0,
        )
        self.assertEqual(ledger.capital_before.tolist(), [100.0, 98.0])
        self.assertAlmostEqual(ledger.position_fraction[0].item(), 0.09)
        self.assertAlmostEqual(ledger.position_fraction[1].item(), 10 / 98)


if __name__ == "__main__":
    unittest.main()
