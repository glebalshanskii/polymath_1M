from __future__ import annotations

import csv
import hashlib
import json
import platform
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow
import torch

from polymath_1M.historical.config import load_kacho_dataset_config
from polymath_1M.domain import DecisionBatch
from polymath_1M.historical.kacho import load_kacho_decision_batch

from .config import BacktestConfig, load_backtest_config
from .model import (
    STATUS_NAMES,
    Evaluation,
    LookupModel,
    evaluate_batch,
    fit_lookup_model,
)
from .parameters import StrategyConfig, load_strategy_config


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git(args: list[str]) -> str | None:
    try:
        return subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _split_indices(
    count: int, train_fraction: float, validation_fraction: float
) -> dict[str, torch.Tensor]:
    train_end = int(count * train_fraction)
    validation_end = int(count * (train_fraction + validation_fraction))
    if train_end == 0 or validation_end <= train_end or validation_end >= count:
        raise ValueError("chronological split contains an empty partition")
    all_indices = torch.arange(count, dtype=torch.int64)
    return {
        "train": all_indices[:train_end],
        "validation": all_indices[train_end:validation_end],
        "test": all_indices[validation_end:],
    }


def _evaluate(
    batch: DecisionBatch,
    model: LookupModel,
    config: BacktestConfig,
    strategy: StrategyConfig,
) -> Evaluation:
    return evaluate_batch(
        batch,
        model,
        minimum_support=config.minimum_support,
        minimum_persistence=strategy.minimum_persistence,
        minimum_ask=strategy.minimum_ask,
        maximum_ask=strategy.maximum_ask,
        minimum_net_edge=strategy.minimum_net_edge,
        target_notional_usdc=strategy.target_notional_usdc,
        platform_fee_rate=config.platform_fee_rate,
        platform_fee_round_decimals=config.platform_fee_round_decimals,
        extra_cost_per_share=config.extra_cost_per_share,
        require_market_favorite=strategy.require_market_favorite,
    )


def _optional_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _summary(batch: DecisionBatch, result: Evaluation) -> dict[str, Any]:
    filled_pnl = result.net_pnl[result.filled]
    positive = filled_pnl[filled_pnl > 0].sum().item()
    negative = -filled_pnl[filled_pnl < 0].sum().item()
    cash_cost = (
        (result.fill_cost + result.platform_fee + result.extra_cost)[result.filled]
        .sum()
        .item()
    )
    cumulative = torch.cumsum(result.net_pnl, dim=0)
    running_peak = torch.cummax(
        torch.cat(
            (
                torch.zeros(1, dtype=cumulative.dtype, device=cumulative.device),
                cumulative,
            )
        ),
        dim=0,
    ).values[1:]
    drawdown = running_peak - cumulative
    statuses = Counter(
        STATUS_NAMES[code] for code in result.status_code.detach().cpu().tolist()
    )
    filled_count = int(result.filled.sum().item())
    return {
        "markets": len(batch),
        "valid_snapshots": int(batch.snapshot_valid.sum().item()),
        "fills": filled_count,
        "fill_rate": filled_count / len(batch),
        "gross_pnl_usdc": result.gross_pnl.sum().item(),
        "extra_cost_usdc": result.extra_cost.sum().item(),
        "platform_fee_usdc": result.platform_fee.sum().item(),
        "net_pnl_usdc": result.net_pnl.sum().item(),
        "cash_cost_usdc": cash_cost,
        "return_on_cash_cost": _optional_ratio(result.net_pnl.sum().item(), cash_cost),
        "profit_factor": _optional_ratio(positive, negative),
        "max_drawdown_usdc": drawdown.max().item() if len(batch) else 0.0,
        "positive_fill_rate": (
            int((filled_pnl > 0).sum().item()) / filled_count if filled_count else None
        ),
        "status_counts": dict(sorted(statuses.items())),
        "market_start_min": datetime.fromtimestamp(
            int(batch.market_start_s.min().item()), tz=UTC
        ).isoformat(),
        "market_start_max": datetime.fromtimestamp(
            int(batch.market_start_s.max().item()), tz=UTC
        ).isoformat(),
    }


def _write_decisions(
    path: Path,
    split_batches: dict[str, DecisionBatch],
    evaluations: dict[str, Evaluation],
) -> None:
    columns = (
        "split",
        "condition_id",
        "asset",
        "decision_timestamp",
        "label_source",
        "outcome_up",
        "snapshot_valid",
        "state_mid_up",
        "state_bin",
        "support",
        "persistence",
        "side",
        "model_probability",
        "signal_ask",
        "fill_vwap",
        "net_edge",
        "fill_shares",
        "fill_cost_usdc",
        "platform_fee_usdc",
        "gross_pnl_usdc",
        "extra_cost_usdc",
        "net_pnl_usdc",
        "status",
    )
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for split_name in ("train", "validation", "test"):
            batch = split_batches[split_name]
            result = evaluations[split_name]
            tensors = {
                "decision": batch.decision_s.detach().cpu().tolist(),
                "outcome": batch.outcome_up.detach().cpu().tolist(),
                "valid": batch.snapshot_valid.detach().cpu().tolist(),
                "mid": batch.current_mid_up.detach().cpu().tolist(),
                "bin": result.state_bin.detach().cpu().tolist(),
                "support": result.support.detach().cpu().tolist(),
                "persistence": result.persistence.detach().cpu().tolist(),
                "side": result.side.detach().cpu().tolist(),
                "probability": result.probability.detach().cpu().tolist(),
                "signal_ask": result.signal_ask.detach().cpu().tolist(),
                "fill_vwap": result.fill_vwap.detach().cpu().tolist(),
                "edge": result.net_edge.detach().cpu().tolist(),
                "shares": result.fill_shares.detach().cpu().tolist(),
                "cost": result.fill_cost.detach().cpu().tolist(),
                "fee": result.platform_fee.detach().cpu().tolist(),
                "gross": result.gross_pnl.detach().cpu().tolist(),
                "extra": result.extra_cost.detach().cpu().tolist(),
                "net": result.net_pnl.detach().cpu().tolist(),
                "status": result.status_code.detach().cpu().tolist(),
            }
            for index, condition_id in enumerate(batch.condition_ids):
                writer.writerow(
                    {
                        "split": split_name,
                        "condition_id": condition_id,
                        "asset": batch.assets[index],
                        "decision_timestamp": datetime.fromtimestamp(
                            tensors["decision"][index], tz=UTC
                        ).isoformat(),
                        "label_source": batch.label_source,
                        "outcome_up": tensors["outcome"][index],
                        "snapshot_valid": tensors["valid"][index],
                        "state_mid_up": tensors["mid"][index],
                        "state_bin": tensors["bin"][index],
                        "support": tensors["support"][index],
                        "persistence": tensors["persistence"][index],
                        "side": "Up" if tensors["side"][index] == 0 else "Down",
                        "model_probability": tensors["probability"][index],
                        "signal_ask": tensors["signal_ask"][index],
                        "fill_vwap": tensors["fill_vwap"][index],
                        "net_edge": tensors["edge"][index],
                        "fill_shares": tensors["shares"][index],
                        "fill_cost_usdc": tensors["cost"][index],
                        "platform_fee_usdc": tensors["fee"][index],
                        "gross_pnl_usdc": tensors["gross"][index],
                        "extra_cost_usdc": tensors["extra"][index],
                        "net_pnl_usdc": tensors["net"][index],
                        "status": STATUS_NAMES[tensors["status"][index]],
                    }
                )


def run_kacho_backtest(
    config_path: str | Path,
    output_root: str | Path = "outputs/backtests",
) -> Path:
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    source_dirty = bool(_git(["status", "--porcelain"]))
    config = load_backtest_config(config_path)
    strategy = load_strategy_config(config.strategy_config)
    if strategy.duration != "5m" or not set(config.assets) <= set(strategy.assets):
        raise ValueError("Kacho adapter assets/duration violate the strategy universe")
    dataset_config = load_kacho_dataset_config(config.dataset_config)
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("configured CUDA device is not available")
    dtype = torch.float64
    torch.manual_seed(config.seed)
    batch = load_kacho_decision_batch(
        dataset_config,
        config.dataset_root,
        assets=config.assets,
        max_markets=config.max_markets,
        decision_seconds_before_end=config.decision_seconds_before_end,
        transition_horizon_seconds=config.transition_horizon_seconds,
        label_policy=config.label_policy,
    ).to(device, dtype)
    indices = _split_indices(
        len(batch), config.train_fraction, config.validation_fraction
    )
    split_batches = {
        name: batch.index(index.to(device)) for name, index in indices.items()
    }
    edges = torch.tensor(config.price_bin_edges, dtype=dtype, device=device)
    model = fit_lookup_model(
        split_batches["train"],
        edges,
        terminal_alpha=config.terminal_alpha,
        transition_alpha=config.transition_alpha,
    )
    evaluations = {
        name: _evaluate(split_batch, model, config, strategy)
        for name, split_batch in split_batches.items()
    }

    run_dir = Path(output_root) / (
        f"{started_at.strftime('%Y%m%dT%H%M%SZ')}_{config.experiment_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    config_payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    _write_json(run_dir / "effective_config.json", config_payload)
    dataset_dir = dataset_config.dataset_dir(config.dataset_root)
    dataset_manifest_path = dataset_dir / "manifest.json"
    dataset_manifest = json.loads(dataset_manifest_path.read_text(encoding="utf-8"))
    _write_json(run_dir / "dataset_manifest.json", dataset_manifest)
    _write_json(
        run_dir / "model.json",
        {
            "schema_version": 1,
            "model": "smoothed_empirical_terminal_lookup",
            "price_bin_edges": model.edges.detach().cpu().tolist(),
            "probability_up": model.probability_up.detach().cpu().tolist(),
            "support": model.support.detach().cpu().tolist(),
            "transition_matrix": model.transition_matrix.detach().cpu().tolist(),
            "persistence": model.persistence.detach().cpu().tolist(),
            "prior_up": model.prior_up.item(),
            "fit_split": "train_only",
        },
    )
    _write_decisions(run_dir / "decisions.csv", split_batches, evaluations)
    summaries = {
        name: _summary(split_batches[name], evaluations[name])
        for name in ("train", "validation", "test")
    }
    summary = {
        "schema_version": 1,
        "status": "development_smoke_only",
        "scientific_acceptance": "not_applicable",
        "reason": (
            "Kacho labels are inferred, full ask depth is absent, and the historical "
            "fee schedule is replaced by the declared proxy"
        ),
        "fee_source": config.fee_source,
        "platform_fee_rate": config.platform_fee_rate,
        "experiment_id": config.experiment_id,
        "strategy_id": strategy.strategy_id,
        "splits": summaries,
    }
    _write_json(run_dir / "summary.json", summary)
    ended_at = datetime.now(UTC)
    _write_json(
        run_dir / "run_manifest.json",
        {
            "schema_version": 1,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "runtime_seconds": time.monotonic() - started_monotonic,
            "source_commit": source_commit,
            "source_worktree_dirty": source_dirty,
            "config_sha256": config.config_sha256,
            "strategy_config_sha256": strategy.config_sha256,
            "dataset_config_sha256": dataset_config.config_sha256,
            "dataset_manifest_sha256": _sha256(dataset_manifest_path),
            "seed": config.seed,
            "device": str(device),
            "dtype": config.dtype,
            "python": sys.version,
            "platform": platform.platform(),
            "torch": torch.__version__,
            "pyarrow": pyarrow.__version__,
            "artifacts": {
                path.name: {"bytes": path.stat().st_size, "sha256": _sha256(path)}
                for path in sorted(run_dir.iterdir())
                if path.is_file()
            },
        },
    )
    return run_dir
