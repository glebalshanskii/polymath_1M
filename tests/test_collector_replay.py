from __future__ import annotations

import json
import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from polymath_1M.collector.book import OrderBookStore
from polymath_1M.collector.replay import replay_run
from polymath_1M.collector.storage import RawStreamWriter


class CollectorReplayTest(unittest.TestCase):
    def test_raw_messages_rebuild_same_book_digest(self) -> None:
        snapshot = json.dumps(
            {
                "asset_id": "token",
                "bids": [{"price": "0.4", "size": "2"}],
                "asks": [{"price": "0.6", "size": "3"}],
                "hash": "one",
            }
        )
        update = json.dumps(
            {
                "price_changes": [
                    {
                        "asset_id": "token",
                        "price": "0.41",
                        "size": "4",
                        "side": "BUY",
                        "hash": "two",
                    }
                ]
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            writer = RawStreamWriter(run_dir, "clob_market", 1)
            books = OrderBookStore()
            for raw in (snapshot, update):
                now = time.time_ns()
                writer.write(
                    raw,
                    receive_timestamp_ns=now,
                    receive_monotonic_ns=time.monotonic_ns(),
                    connection_id="test",
                )
                books.apply_raw(raw)
            writer.close()
            rtds_writer = RawStreamWriter(run_dir, "rtds_reference", 1)
            now = time.time_ns()
            rtds_writer.write(
                "{}",
                receive_timestamp_ns=now,
                receive_monotonic_ns=time.monotonic_ns(),
                connection_id="rtds-test",
            )
            rtds_writer.close()
            (run_dir / "live_book_summary.json").write_text(
                json.dumps(books.summary()), encoding="utf-8"
            )
            raw_inventory = []
            for path in sorted((run_dir / "raw").rglob("*")):
                if path.is_file():
                    raw_inventory.append(
                        {
                            "relative_path": str(path.relative_to(run_dir)),
                            "bytes": path.stat().st_size,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    )
            (run_dir / "manifest.json").write_text(
                json.dumps({"raw_inventory": raw_inventory}), encoding="utf-8"
            )

            output_path = replay_run(run_dir)
            result = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "match")
        self.assertEqual(result["inbound_messages"], 2)
        self.assertEqual(result["rtds_records"], 1)


if __name__ == "__main__":
    unittest.main()
