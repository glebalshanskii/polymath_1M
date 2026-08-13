from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polymath_1M.historical.trent import (
    TrentDataError,
    _local_record,
    load_trent_config,
)


class TrentAdapterTest(unittest.TestCase):
    def test_local_record_accepts_only_nonempty_atomic_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "steps.parquet"
            self.assertIsNone(_local_record("market/steps.parquet", target))
            target.write_bytes(b"")
            self.assertIsNone(_local_record("market/steps.parquet", target))
            target.write_bytes(b"complete")
            record = _local_record("market/steps.parquet", target)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record["path"], "market/steps.parquet")
            self.assertEqual(record["bytes"], 8)
            self.assertEqual(len(record["sha256"]), 64)

    def test_frozen_config_pins_source_and_early_period(self) -> None:
        config = load_trent_config("cfg/datasets/trent_btc5m_steps_stage4e.json")
        self.assertEqual(config.expected_files, 8_803)
        self.assertEqual(config.license, "CC-BY-SA-4.0")
        self.assertEqual(len(config.revision), 40)
        self.assertLess(config.period_start_s, config.period_end_exclusive_s)

    def test_config_rejects_unpinned_revision(self) -> None:
        source = Path("cfg/datasets/trent_btc5m_steps_stage4e.json")
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["revision"] = "main"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(TrentDataError, "audited early"):
                load_trent_config(path)


if __name__ == "__main__":
    unittest.main()
