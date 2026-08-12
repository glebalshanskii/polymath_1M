from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch

from .analysis import analyze_account_db
from .client import PolymarketClient
from .config import AuditConfig, load_config
from .http import JsonTransport, UrllibJsonTransport
from .storage import AuditStorage


def run_profile_audit(
    config_path: str | Path,
    output_root: str | Path = "outputs/profile_audit",
    transport: JsonTransport | None = None,
    resume_run: str | Path | None = None,
) -> Path:
    config_file = Path(config_path)
    config = load_config(config_file)
    run_started = datetime.now(UTC)
    run_dir = (
        Path(resume_run)
        if resume_run is not None
        else Path(output_root)
        / f"{run_started.strftime('%Y%m%dT%H%M%SZ')}_{config.audit_id}"
    )
    if resume_run is not None:
        if not run_dir.is_dir():
            raise FileNotFoundError(f"resume run does not exist: {run_dir}")
        _validate_resume_config(run_dir, config_file)
    else:
        run_dir.mkdir(parents=True)
    if resume_run is not None and (failure := run_dir / "failure.json").exists():
        failure.rename(
            run_dir / f"failure_attempt_{run_started.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
    storage = AuditStorage(run_dir)
    client = PolymarketClient(
        transport or UrllibJsonTransport(),
        storage.archive,
        progress=lambda message: print(message, flush=True),
    )

    account_summaries: list[dict[str, Any]] = []
    all_windows: list[dict[str, Any]] = []
    behavior: dict[str, Any] = {}
    try:
        for account in config.accounts:
            print(f"{account.id}: collecting profile and activity", flush=True)
            profile = client.profile(account.id, account.source_address)
            storage.store_profile(account.id, account.source_address, profile)
            activity_rows = _table_count(storage, "activity", account.id)
            if not storage.collection_complete(account.id, "activity"):
                for part in client.activity_parts(
                    account.id,
                    account.source_address,
                    config.start_epoch,
                    config.end_epoch,
                ):
                    storage.store_rows("activity", account.id, part)
                activity_rows = _table_count(storage, "activity", account.id)
                storage.mark_collection_complete(account.id, "activity", activity_rows)
            print(f"{account.id}: activity rows={activity_rows}", flush=True)
            trade_rows = _table_count(storage, "trades", account.id)
            if (
                config.fetch_trades
                and account.fetch_trade_crosscheck
                and not storage.collection_complete(account.id, "trades")
            ):
                for part in client.trade_parts(
                    account.id,
                    account.source_address,
                    config.start_epoch,
                    config.end_epoch,
                ):
                    storage.store_rows("trades", account.id, part)
                trade_rows = _table_count(storage, "trades", account.id)
                storage.mark_collection_complete(account.id, "trades", trade_rows)
            print(f"{account.id}: trade API rows={trade_rows}", flush=True)
            closed_count = _table_count(storage, "closed_positions", account.id)
            if config.fetch_closed_positions and not storage.collection_complete(
                account.id, "closed_positions"
            ):
                for part in client.closed_position_parts(
                    account.id,
                    account.source_address,
                    config.start_epoch,
                    config.end_epoch,
                ):
                    storage.store_rows("closed_positions", account.id, part)
                closed_count = _table_count(storage, "closed_positions", account.id)
                storage.mark_collection_complete(
                    account.id, "closed_positions", closed_count
                )
            print(f"{account.id}: closed positions={closed_count}", flush=True)
            summary, windows, account_behavior = analyze_account_db(
                config, account, profile, storage.connection
            )
            account_summaries.append(summary)
            all_windows.extend(windows)
            behavior[account.id] = account_behavior
            print(f"{account.id}: analysis complete", flush=True)

        storage.write_csv("window_comparison.csv", all_windows)
        storage.write_json("behavior_summary.json", behavior)
        summary_payload = {
            "audit_id": config.audit_id,
            "run_started_at": run_started.isoformat(),
            "run_completed_at": datetime.now(UTC).isoformat(),
            "status": _overall_status(account_summaries),
            "accounts": account_summaries,
        }
        storage.write_json("audit_summary.json", summary_payload)
        storage.finalize_manifest(_run_metadata(config_file, config, run_started))
    except BaseException as exc:
        storage.write_json(
            "failure.json",
            {
                "run_started_at": run_started.isoformat(),
                "failed_at": datetime.now(UTC).isoformat(),
                "exception_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        storage.finalize_manifest(_run_metadata(config_file, config, run_started))
        raise
    finally:
        storage.close()
    return run_dir


def _overall_status(accounts: list[dict[str, Any]]) -> str:
    if accounts and all(
        account["claim_status"] == "matched_public_ledger" for account in accounts
    ):
        return "matched_public_ledger"
    if any(account["claim_status"] == "matched_public_ledger" for account in accounts):
        return "partially_matched"
    return "not_reconstructable"


def _validate_resume_config(run_dir: Path, config_path: Path) -> None:
    manifest_path = run_dir / "raw_manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    previous_hash = manifest.get("metadata", {}).get("config_sha256")
    current_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    if previous_hash and previous_hash != current_hash:
        raise ValueError(
            "resume config differs from the config recorded in raw_manifest.json"
        )


def _run_metadata(
    config_path: Path, config: AuditConfig, started: datetime
) -> dict[str, Any]:
    config_bytes = config_path.read_bytes()
    return {
        "config_path": str(config_path),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "config": {
            **asdict(config),
            "period_start": config.period_start.isoformat(),
            "period_end": config.period_end.isoformat(),
            "accounts": [asdict(account) for account in config.accounts],
        },
        "commit": _git_commit(),
        "started_at": started.isoformat(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "device": "cpu",
        "dtype": "torch.float64",
    }


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return "unknown"


def _table_count(storage: AuditStorage, table: str, account_id: str) -> int:
    if table not in {"activity", "trades", "closed_positions"}:
        raise ValueError(f"unsupported table: {table}")
    return int(
        storage.connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE account_id = ?", (account_id,)
        ).fetchone()[0]
    )
