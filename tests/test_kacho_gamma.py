from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from polymath_1M.screening.kacho_gamma import _load_gamma_rows


class KachoGammaTest(unittest.TestCase):
    def test_gamma_scan_excludes_holdout_labels_before_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.parquet"
            pq.write_table(
                pa.table(
                    {
                        "condition_id": ["development", "holdout", "wrong-duration"],
                        "asset": ["BTC", "BTC", "BTC"],
                        "duration": ["5m", "5m", "15m"],
                        "market_start_ms": [10_000, 100_000, 10_000],
                        "outcome_up": [1.0, 0.0, 1.0],
                        "fee_rate": [0.07, 0.07, 0.07],
                    }
                ),
                path,
            )
            rows = _load_gamma_rows(
                path,
                assets=("BTC",),
                period_start_s=0,
                end_exclusive_s=100,
            )

        self.assertEqual(tuple(rows), ("development",))
        self.assertEqual(rows["development"]["outcome_up"], 1.0)


if __name__ == "__main__":
    unittest.main()
