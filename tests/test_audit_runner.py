from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from polymath_1M.audit.http import JsonResponse
from polymath_1M.audit.runner import run_profile_audit
from polymath_1M.audit.storage import build_raw_inventory
from polymath_1M.cli import main


class FixtureTransport:
    def get_json(
        self, base_url: str, path: str, params: dict[str, object]
    ) -> JsonResponse:
        if path == "/public-profile":
            payload: Any = {
                "proxyWallet": params["address"],
                "name": "Fixture",
                "createdAt": "2026-03-01T00:00:00Z",
            }
        elif path in {"/activity", "/trades"} and int(params["offset"]) == 0:
            payload = [
                {
                    "timestamp": 1_772_323_300,
                    "type": "TRADE",
                    "side": "BUY",
                    "conditionId": "c1",
                    "asset": "t1",
                    "outcome": "Up",
                    "price": 0.5,
                    "size": 2,
                    "usdcSize": 1,
                    "transactionHash": "0x1",
                }
            ]
        else:
            payload = []
        body = json.dumps(payload).encode()
        return JsonResponse(
            data=payload,
            body=body,
            status=200,
            url=f"{base_url}{path}",
            retrieved_at=datetime.now(UTC).isoformat(),
        )


class AuditRunnerTest(unittest.TestCase):
    def test_cli_without_subcommand_is_a_successful_help_smoke(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            main([])
        self.assertIn("profile-audit", output.getvalue())

    def test_writes_reproducible_artifact_set(self) -> None:
        config = {
            "audit_id": "fixture",
            "period_start": "2026-03-01T00:00:00Z",
            "period_end": "2026-03-31T23:59:59Z",
            "window_days": 30,
            "pnl_tolerance_usdc": 1,
            "biggest_win_tolerance_usdc": 1,
            "fetch_trades": True,
            "fetch_closed_positions": False,
            "accounts": [
                {
                    "id": "fixture",
                    "source_address": "0x" + "1" * 40,
                    "expected_name": "Fixture",
                    "claimed_pnl_usdc": 1,
                    "claimed_predictions": 1,
                    "claimed_biggest_win_usdc": 1,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            run_dir = run_profile_audit(
                config_path, root / "outputs", FixtureTransport()
            )

            self.assertTrue((run_dir / "audit.sqlite3").exists())
            self.assertTrue((run_dir / "audit_summary.json").exists())
            self.assertTrue((run_dir / "behavior_summary.json").exists())
            manifest = json.loads(
                (run_dir / "raw_manifest.json").read_text(encoding="utf-8")
            )
            self.assertGreaterEqual(len(manifest["requests"]), 3)
            _, inventory_summary_path = build_raw_inventory(run_dir)
            inventory_summary = json.loads(
                inventory_summary_path.read_text(encoding="utf-8")
            )
            self.assertEqual(inventory_summary["request_metadata_coverage"], 1.0)

    def test_resume_rejects_a_different_config(self) -> None:
        config = {
            "audit_id": "fixture",
            "period_start": "2026-03-01T00:00:00Z",
            "period_end": "2026-03-31T23:59:59Z",
            "window_days": 30,
            "pnl_tolerance_usdc": 1,
            "biggest_win_tolerance_usdc": 1,
            "fetch_trades": False,
            "fetch_closed_positions": False,
            "accounts": [
                {
                    "id": "fixture",
                    "source_address": "0x" + "1" * 40,
                    "expected_name": "Fixture",
                    "claimed_pnl_usdc": 1,
                    "claimed_predictions": 1,
                    "claimed_biggest_win_usdc": 1,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            run_dir = run_profile_audit(
                config_path, root / "outputs", FixtureTransport()
            )
            config["window_days"] = 29
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "resume config differs"):
                run_profile_audit(
                    config_path,
                    root / "outputs",
                    FixtureTransport(),
                    resume_run=run_dir,
                )


if __name__ == "__main__":
    unittest.main()
