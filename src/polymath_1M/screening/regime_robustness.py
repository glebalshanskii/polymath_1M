from __future__ import annotations

import hashlib
import json
import platform
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import plotly
import torch

from polymath_1M.historical.polymarket_chainlink import (
    load_polymarket_chainlink_series,
)
from polymath_1M.historical.trent import load_trent_decision_batch

from .regime_models import (
    Fold,
    RegimeModelError,
    _git,
    _render_comparison,
    _render_variant,
    _run_variant,
    _timestamp,
    _write_csv,
    _write_json,
    chainlink_features,
    common_features,
    load_regime_config,
)


@dataclass(frozen=True)
class EarlyRobustnessConfig:
    experiment_id: str
    primary_config: str
    dataset_config: str
    chainlink_config: str
    period_start_s: int
    period_end_exclusive_s: int
    folds: tuple[Fold, ...]
    variants: tuple[str, ...]
    decision_seconds_before_end: int
    transition_horizon_seconds: int
    execution_latency_seconds: int
    execution_size_assumption: str
    outcome_policy: str
    config_sha256: str


def load_early_robustness_config(path: str | Path) -> EarlyRobustnessConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "experiment_id",
        "primary_config",
        "dataset_config",
        "chainlink_config",
        "period_start",
        "period_end_exclusive",
        "folds",
        "variants",
        "decision_seconds_before_end",
        "transition_horizon_seconds",
        "execution_latency_seconds",
        "execution_size_assumption",
        "outcome_policy",
    }
    if payload.keys() != expected:
        raise RegimeModelError(
            f"early robustness fields differ: missing={sorted(expected - payload.keys())}, "
            f"extra={sorted(payload.keys() - expected)}"
        )
    start_s = _timestamp(payload["period_start"], "period_start")
    end_s = _timestamp(payload["period_end_exclusive"], "period_end_exclusive")
    folds: list[Fold] = []
    fold_fields = {
        "fold_id",
        "train_start",
        "train_end_exclusive",
        "validation_start",
        "validation_end_exclusive",
    }
    for raw in payload["folds"]:
        if raw.keys() != fold_fields:
            raise RegimeModelError("early robustness fold fields differ")
        folds.append(
            Fold(
                fold_id=str(raw["fold_id"]),
                train_start_s=_timestamp(raw["train_start"], "train_start"),
                train_end_exclusive_s=_timestamp(
                    raw["train_end_exclusive"], "train_end_exclusive"
                ),
                validation_start_s=_timestamp(
                    raw["validation_start"], "validation_start"
                ),
                validation_end_exclusive_s=_timestamp(
                    raw["validation_end_exclusive"], "validation_end_exclusive"
                ),
            )
        )
    variants = tuple(str(value) for value in payload["variants"])
    if (
        payload["schema_version"] != 1
        or len(folds) != 3
        or variants != ("m0_coarse_lookup_10c", "m6_chainlink_regime_decay_lagged")
        or folds[0].train_start_s != start_s
        or folds[-1].validation_end_exclusive_s != end_s
        or any(
            fold.train_start_s != start_s
            or fold.train_end_exclusive_s != fold.validation_start_s
            for fold in folds
        )
        or any(
            left.validation_end_exclusive_s != right.validation_start_s
            for left, right in zip(folds[:-1], folds[1:], strict=True)
        )
        or int(payload["decision_seconds_before_end"]) != 60
        or int(payload["transition_horizon_seconds"]) != 60
        or int(payload["execution_latency_seconds"]) != 1
        or payload["execution_size_assumption"]
        != "target_notional_available_at_best_ask_only_if_total_ask_notional_covers_target"
        or payload["outcome_policy"] != "inferred_from_final_token_mid_development_only"
    ):
        raise RegimeModelError("early robustness contract differs")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return EarlyRobustnessConfig(
        experiment_id=str(payload["experiment_id"]),
        primary_config=str(payload["primary_config"]),
        dataset_config=str(payload["dataset_config"]),
        chainlink_config=str(payload["chainlink_config"]),
        period_start_s=start_s,
        period_end_exclusive_s=end_s,
        folds=tuple(folds),
        variants=variants,
        decision_seconds_before_end=60,
        transition_horizon_seconds=60,
        execution_latency_seconds=1,
        execution_size_assumption=str(payload["execution_size_assumption"]),
        outcome_policy=str(payload["outcome_policy"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def run_stage4e_early_robustness(
    config_path: str | Path,
    data_root: str | Path = "data/historical",
    output_root: str | Path = "outputs/regime_models",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    config = load_early_robustness_config(config_path)
    primary = load_regime_config(config.primary_config)
    secondary = replace(primary, folds=config.folds, variants=config.variants)
    device = torch.device(primary.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RegimeModelError("frozen CUDA device is unavailable")
    torch.manual_seed(primary.seed)
    batch, trent_provenance = load_trent_decision_batch(
        config.dataset_config,
        data_root,
        decision_seconds_before_end=config.decision_seconds_before_end,
        transition_horizon_seconds=config.transition_horizon_seconds,
        execution_latency_seconds=config.execution_latency_seconds,
        target_notional_usdc=primary.target_notional_usdc,
    )
    if (
        int(batch.market_start_s.min().item()) < config.period_start_s
        or int(batch.market_start_s.max().item()) >= config.period_end_exclusive_s
    ):
        raise RegimeModelError("Trent adapter period differs from robustness config")
    chainlink, chainlink_provenance = load_polymarket_chainlink_series(
        config.chainlink_config, data_root
    )
    timestamped = chainlink_features(batch, chainlink)
    lagged = chainlink_features(batch, chainlink, causal_lag_seconds=60)
    features, feature_names = common_features(batch, timestamped, lagged)
    run_dir = Path(output_root) / (
        f"{started.strftime('%Y%m%dT%H%M%SZ')}_{config.experiment_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    results = []
    for variant in config.variants:
        result = _run_variant(
            variant, secondary, batch, features, feature_names, device
        )
        variant_dir = run_dir / variant
        variant_dir.mkdir()
        _write_csv(variant_dir / "decisions.csv", result.rows)
        _write_csv(variant_dir / "fold_metrics.csv", result.fold_metrics)
        _write_json(variant_dir / "summary.json", result.summary)
        _render_variant(variant_dir / "pnl_diagnostics.html", result, secondary)
        results.append(result)
    comparison: list[dict[str, Any]] = []
    for result in results:
        comparison.append(
            {
                key: value
                for key, value in result.summary.items()
                if key not in {"models", "bad_regime"}
            }
        )
    _write_csv(run_dir / "comparison.csv", comparison)
    _write_json(run_dir / "comparison.json", comparison)
    _render_comparison(run_dir / "comparison.html", results)
    baseline = results[0].summary
    candidate = results[1].summary
    _write_json(
        run_dir / "robustness_result.json",
        {
            "schema_version": 1,
            "status": "source_specific_indicative",
            "period_start": datetime.fromtimestamp(
                config.period_start_s, tz=UTC
            ).isoformat(),
            "period_end_exclusive": datetime.fromtimestamp(
                config.period_end_exclusive_s, tz=UTC
            ).isoformat(),
            "baseline": baseline,
            "candidate": candidate,
            "candidate_minus_baseline": {
                "fills": candidate["fills"] - baseline["fills"],
                "net_pnl_usdc": candidate["net_pnl_usdc"] - baseline["net_pnl_usdc"],
                "max_drawdown_usdc": candidate["max_drawdown_usdc"]
                - baseline["max_drawdown_usdc"],
                "worst_rolling_24h_pnl_usdc": candidate["worst_rolling_24h_pnl_usdc"]
                - baseline["worst_rolling_24h_pnl_usdc"],
            },
            "limitations": [
                config.execution_size_assumption,
                config.outcome_policy,
                "source PnL is not pooled with Kacho primary PnL",
            ],
        },
    )
    _write_json(
        run_dir / "run_record.json",
        {
            "schema_version": 1,
            "experiment_id": config.experiment_id,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_seconds": time.monotonic() - started_monotonic,
            "source_commit": _git(["rev-parse", "HEAD"]),
            "source_dirty": bool(_git(["status", "--porcelain"])),
            "config_path": str(config_path),
            "config_sha256": config.config_sha256,
            "primary_config": config.primary_config,
            "primary_config_sha256": primary.config_sha256,
            "trent_provenance": trent_provenance,
            "chainlink_provenance": chainlink_provenance,
            "rows_loaded": len(batch),
            "valid_rows": int(batch.snapshot_valid.sum().item()),
            "device": str(device),
            "dtype": primary.dtype,
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(device),
            "plotly_version": plotly.__version__,
            "python": platform.python_version(),
        },
    )
    return run_dir
