from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from polymath_1M.historical.config import load_kacho_dataset_config
from polymath_1M.historical.download import download_kacho_dataset
from polymath_1M.historical.kacho import load_kacho_decision_batch


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class KachoAdapterTest(unittest.TestCase):
    def test_maps_exact_causal_snapshots_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = "a" * 40
            dataset_dir = root / "fixture" / revision
            dataset_dir.mkdir(parents=True)
            starts = [
                datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * i)
                for i in range(5)
            ]
            ends = [value + timedelta(minutes=5) for value in starts]
            ids = [f"condition-{index}" for index in range(5)]
            markets_path = dataset_dir / "btc_markets.parquet"
            pq.write_table(
                pa.table(
                    {
                        "condition_id": ids,
                        "market_start": starts,
                        "market_end": ends,
                        "outcome": ["Up", "Down", "Up", "Down", "Up"],
                        "n_ticks": [300] * 5,
                    }
                ),
                markets_path,
            )
            ticks: list[dict[str, object]] = []
            for condition_id, end in zip(ids, ends, strict=True):
                for seconds_before_end, up_bid in ((120, 0.39), (60, 0.41)):
                    ticks.append(
                        {
                            "condition_id": condition_id,
                            "t": int(end.timestamp()) - seconds_before_end,
                            "bu": up_bid,
                            "au": up_bid + 0.02,
                            "bd": 0.57,
                            "ad": 0.59,
                            "sau": 20.0,
                            "sad": 30.0,
                        }
                    )
            ticks_path = dataset_dir / "btc_ticks.parquet"
            pq.write_table(pa.Table.from_pylist(ticks), ticks_path)
            file_specs = []
            for kind, path in (("markets", markets_path), ("ticks", ticks_path)):
                file_specs.append(
                    {
                        "asset": "BTC",
                        "kind": kind,
                        "path": path.name,
                        "bytes": path.stat().st_size,
                        "sha256": _hash(path),
                    }
                )
            config_payload = {
                "schema_version": 1,
                "dataset_id": "fixture",
                "revision": revision,
                "license": "test",
                "base_url": "https://example.invalid",
                "files": file_specs,
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config_payload), encoding="utf-8")
            config = load_kacho_dataset_config(config_path)
            manifest = {
                "schema_version": 1,
                "dataset_id": "fixture",
                "revision": revision,
                "license": "test",
                "config_sha256": config.config_sha256,
                "files": file_specs,
            }
            (dataset_dir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            download_kacho_dataset(config_path, root, assets=["BTC"])
            stable_manifest = (dataset_dir / "manifest.json").read_bytes()
            download_kacho_dataset(config_path, root, assets=["BTC"])
            self.assertEqual(
                (dataset_dir / "manifest.json").read_bytes(), stable_manifest
            )

            batch = load_kacho_decision_batch(
                config,
                root,
                assets=["BTC"],
                max_markets=5,
                decision_seconds_before_end=60,
                transition_horizon_seconds=60,
                label_policy="kacho_inferred_development_only",
            )

        self.assertEqual(len(batch), 5)
        self.assertTrue(batch.snapshot_valid.all().item())
        self.assertEqual(batch.current_mid_up.tolist(), [0.42] * 5)
        self.assertEqual(batch.previous_mid_up.tolist(), [0.4] * 5)
        self.assertEqual(batch.ask_depth_prices.shape, (5, 2, 1))
        self.assertEqual(batch.outcome_up.tolist(), [1.0, 0.0, 1.0, 0.0, 1.0])


if __name__ == "__main__":
    unittest.main()
