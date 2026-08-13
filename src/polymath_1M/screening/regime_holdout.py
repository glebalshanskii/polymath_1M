from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import plotly
import pyarrow.parquet as pq
import torch

from polymath_1M.historical.config import load_kacho_dataset_config
from polymath_1M.historical.kacho import load_kacho_decision_batch
from polymath_1M.historical.polymarket_chainlink import (
    load_polymarket_chainlink_series,
)
from polymath_1M.strategy.model import fit_lookup_model

from .regime_models import (
    RegimeModelError,
    VariantResult,
    _diagnostic_rows,
    _evaluate,
    _feature_columns,
    _git,
    _metrics,
    _recency_weights,
    _render_comparison,
    _render_variant,
    _sha256,
    _uniform_edges,
    _write_csv,
    _write_json,
    chainlink_features,
    common_features,
    fit_logistic_model,
    load_regime_config,
    predict_logistic,
)


@dataclass(frozen=True)
class HoldoutGate:
    minimum_fills: int
    minimum_profit_factor: float
    require_positive_net_pnl: bool
    require_positive_stress_2c_pnl: bool


@dataclass(frozen=True)
class SelectedRegimeConfig:
    experiment_id: str
    model_config: str
    model_config_sha256: str
    dataset_manifest: str
    dataset_manifest_sha256: str
    chainlink_manifest: str
    chainlink_manifest_sha256: str
    gamma_universe: str
    gamma_universe_sha256: str
    gamma_universe_manifest: str
    gamma_universe_manifest_sha256: str
    proposal: str
    proposal_sha256: str
    proposal_source_commit: str
    selected_variant: str
    development_summary: dict[str, Any]
    holdout_gate: HoldoutGate


def load_selected_regime_config(path: str | Path) -> SelectedRegimeConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "experiment_id",
        "model_config",
        "model_config_sha256",
        "dataset_manifest",
        "dataset_manifest_sha256",
        "chainlink_manifest",
        "chainlink_manifest_sha256",
        "gamma_universe",
        "gamma_universe_sha256",
        "gamma_universe_manifest",
        "gamma_universe_manifest_sha256",
        "proposal",
        "proposal_sha256",
        "proposal_source_commit",
        "selected_variant",
        "development_summary",
        "holdout_gate",
    }
    if payload.keys() != expected:
        raise RegimeModelError(
            f"selected Stage 4e fields differ: missing={sorted(expected - payload.keys())}, "
            f"extra={sorted(payload.keys() - expected)}"
        )
    summary_fields = {
        "fills",
        "net_pnl_usdc",
        "profit_factor",
        "max_drawdown_usdc",
        "worst_rolling_24h_pnl_usdc",
        "positive_folds",
        "stress_2c_net_pnl_usdc",
    }
    gate_fields = {
        "minimum_fills",
        "minimum_profit_factor",
        "require_positive_net_pnl",
        "require_positive_stress_2c_pnl",
    }
    if (
        payload["schema_version"] != 1
        or payload["selected_variant"] != "m6_chainlink_regime_decay_lagged"
        or payload["development_summary"].keys() != summary_fields
        or payload["holdout_gate"].keys() != gate_fields
        or len(payload["proposal_source_commit"]) != 40
    ):
        raise RegimeModelError("selected Stage 4e contract differs")
    raw_gate = payload["holdout_gate"]
    gate = HoldoutGate(
        minimum_fills=int(raw_gate["minimum_fills"]),
        minimum_profit_factor=float(raw_gate["minimum_profit_factor"]),
        require_positive_net_pnl=bool(raw_gate["require_positive_net_pnl"]),
        require_positive_stress_2c_pnl=bool(
            raw_gate["require_positive_stress_2c_pnl"]
        ),
    )
    if (
        gate.minimum_fills != 50
        or gate.minimum_profit_factor != 1.10
        or not gate.require_positive_net_pnl
        or not gate.require_positive_stress_2c_pnl
    ):
        raise RegimeModelError("selected Stage 4e holdout gate differs")
    values = {key: value for key, value in payload.items() if key != "schema_version"}
    values["holdout_gate"] = gate
    return SelectedRegimeConfig(**values)


def _verify_inputs(
    selected: SelectedRegimeConfig,
) -> tuple[Any, dict[str, Any]]:
    config = load_regime_config(selected.model_config)
    checks = (
        (config.config_sha256, selected.model_config_sha256, "model config"),
        (
            _sha256(Path(selected.dataset_manifest)),
            selected.dataset_manifest_sha256,
            "Kacho manifest",
        ),
        (
            _sha256(Path(selected.chainlink_manifest)),
            selected.chainlink_manifest_sha256,
            "Chainlink manifest",
        ),
        (
            _sha256(Path(selected.gamma_universe)),
            selected.gamma_universe_sha256,
            "Gamma universe",
        ),
        (
            _sha256(Path(selected.gamma_universe_manifest)),
            selected.gamma_universe_manifest_sha256,
            "Gamma universe manifest",
        ),
        (
            _sha256(Path(selected.proposal)),
            selected.proposal_sha256,
            "development proposal",
        ),
    )
    for actual, expected, label in checks:
        if actual != expected:
            raise RegimeModelError(f"{label} SHA-256 differs from selected config")
    proposal = json.loads(Path(selected.proposal).read_text(encoding="utf-8"))
    if (
        proposal.get("status") != "selected"
        or proposal.get("selected_variant") != selected.selected_variant
        or proposal.get("holdout_opened") is not False
    ):
        raise RegimeModelError("selected config differs from development proposal")
    candidate = next(
        (
            item
            for item in proposal["candidates"]
            if item["variant"] == selected.selected_variant
        ),
        None,
    )
    if candidate is None or not candidate["eligible"]:
        raise RegimeModelError("selected candidate was not development-eligible")
    for key, expected in selected.development_summary.items():
        actual = candidate["summary"][key]
        if isinstance(expected, float):
            if abs(float(actual) - expected) > 1e-9:
                raise RegimeModelError(f"development metric differs: {key}")
        elif actual != expected:
            raise RegimeModelError(f"development metric differs: {key}")
    return config, proposal


def _overlay_gamma_labels(
    batch: Any,
    universe_path: str | Path,
    *,
    holdout_start_s: int,
) -> tuple[Any, dict[str, Any]]:
    rows = pq.read_table(
        universe_path,
        columns=["condition_id", "asset", "duration", "outcome_up", "fee_rate"],
        filters=[("asset", "=", "BTC"), ("duration", "=", "5m")],
    ).to_pylist()
    gamma = {str(row["condition_id"]): row for row in rows}
    outcomes = batch.outcome_up.clone()
    covered = 0
    mismatches = 0
    holdout_missing: list[str] = []
    for index, condition_id in enumerate(batch.condition_ids):
        row = gamma.get(condition_id)
        if row is None:
            if int(batch.market_start_s[index].item()) >= holdout_start_s:
                holdout_missing.append(condition_id)
            continue
        authoritative = float(row["outcome_up"])
        if authoritative not in {0.0, 1.0}:
            raise RegimeModelError("Gamma outcome is not binary")
        covered += 1
        mismatches += int(float(outcomes[index].item()) != authoritative)
        outcomes[index] = authoritative
    if holdout_missing:
        raise RegimeModelError(
            f"Gamma is missing {len(holdout_missing)} holdout conditions"
        )
    return (
        replace(
            batch,
            outcome_up=outcomes,
            label_source="gamma_authoritative_holdout_with_kacho_early_train",
        ),
        {
            "gamma_rows": len(gamma),
            "batch_rows_covered": covered,
            "kacho_gamma_label_mismatches": mismatches,
            "holdout_rows_missing": 0,
        },
    )


def _gate(metrics: dict[str, Any], gate: HoldoutGate) -> tuple[str, dict[str, bool]]:
    profit_factor = (
        float("inf")
        if metrics["profit_factor_infinite"]
        else float(metrics["profit_factor"] or 0)
    )
    checks = {
        "minimum_fills": metrics["fills"] >= gate.minimum_fills,
        "positive_net_pnl": (
            metrics["net_pnl_usdc"] > 0 if gate.require_positive_net_pnl else True
        ),
        "minimum_profit_factor": profit_factor >= gate.minimum_profit_factor,
        "positive_stress_2c_pnl": (
            metrics["stress_2c_net_pnl_usdc"] > 0
            if gate.require_positive_stress_2c_pnl
            else True
        ),
    }
    return ("pass" if all(checks.values()) else "fail"), checks


def run_stage4e_holdout(
    selected_config_path: str | Path,
    data_root: str | Path = "data/historical",
    output_root: str | Path = "outputs/holdout",
) -> Path:
    selected = load_selected_regime_config(selected_config_path)
    config, _ = _verify_inputs(selected)
    if _git(["status", "--porcelain"]):
        raise RegimeModelError("commit the selected config and runner before holdout")
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RegimeModelError("frozen CUDA device is unavailable")
    run_dir = Path(output_root) / f"{selected.experiment_id}_holdout"
    if run_dir.exists():
        raise RegimeModelError(f"canonical Stage 4e holdout exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    _write_json(
        run_dir / "holdout_opened.json",
        {
            "opened_at": started.isoformat(),
            "source_commit": _git(["rev-parse", "HEAD"]),
            "selected_config": str(selected_config_path),
            "proposal_sha256": selected.proposal_sha256,
        },
    )
    torch.manual_seed(config.seed)
    dataset_config = load_kacho_dataset_config(config.dataset_config)
    batch = load_kacho_decision_batch(
        dataset_config,
        data_root,
        assets=("BTC",),
        max_markets=0,
        decision_seconds_before_end=60,
        transition_horizon_seconds=60,
        label_policy="kacho_inferred_development_only",
        execution_latency_seconds=1,
        period_start_s=config.period_start_s,
        period_end_exclusive_s=config.holdout_end_exclusive_s,
    )
    order = torch.argsort(batch.market_start_s, stable=True)
    batch = batch.index(order)
    batch, label_audit = _overlay_gamma_labels(
        batch,
        selected.gamma_universe,
        holdout_start_s=config.development_end_exclusive_s,
    )
    train_indices = torch.nonzero(
        batch.market_start_s < config.development_end_exclusive_s,
        as_tuple=False,
    ).flatten()
    holdout_indices = torch.nonzero(
        (batch.market_start_s >= config.development_end_exclusive_s)
        & (batch.market_start_s < config.holdout_end_exclusive_s),
        as_tuple=False,
    ).flatten()
    if train_indices.numel() == 0 or holdout_indices.numel() == 0:
        raise RegimeModelError("Stage 4e train or holdout is empty")
    train = batch.index(train_indices).to(device, torch.float64)
    holdout = batch.index(holdout_indices).to(device, torch.float64)
    chainlink, chainlink_provenance = load_polymarket_chainlink_series(
        config.chainlink_config, data_root
    )
    timestamped = chainlink_features(batch, chainlink)
    lagged = chainlink_features(batch, chainlink, causal_lag_seconds=60)
    features, feature_names = common_features(batch, timestamped, lagged)
    train_features = features.index_select(0, train_indices).to(device)
    holdout_features = features.index_select(0, holdout_indices).to(device)
    coarse = fit_lookup_model(
        train,
        _uniform_edges(config.coarse_price_bin_width, device),
        terminal_alpha=config.terminal_alpha,
        transition_alpha=config.transition_alpha,
    )
    columns = _feature_columns(selected.selected_variant)
    column_index = torch.tensor(columns, dtype=torch.int64, device=device)
    logistic = fit_logistic_model(
        train_features.index_select(1, column_index),
        train.outcome_up,
        _recency_weights(
            train, config.development_end_exclusive_s, config.decay_half_life_days
        ),
        ridge=config.logistic_ridge,
        maximum_iterations=config.logistic_max_iterations,
    )
    candidate_probability = torch.clamp(
        predict_logistic(logistic, holdout_features.index_select(1, column_index)),
        config.minimum_probability,
        config.maximum_probability,
    )
    variants: list[VariantResult] = []
    for variant, probability in (
        ("m0_coarse_lookup_10c", None),
        (selected.selected_variant, candidate_probability),
    ):
        evaluation = _evaluate(holdout, coarse, probability, config)
        rows = _diagnostic_rows(
            variant,
            "one_shot_holdout",
            holdout,
            evaluation,
            holdout_features,
            feature_names,
        )
        summary = _metrics(rows)
        summary["variant"] = variant
        summary["label_source"] = holdout.label_source
        result = VariantResult(
            variant=variant,
            rows=rows,
            fold_metrics=[{"fold_id": "one_shot_holdout", **summary}],
            summary=summary,
        )
        variant_dir = run_dir / variant
        variant_dir.mkdir()
        _write_csv(variant_dir / "decisions.csv", rows)
        _write_json(variant_dir / "summary.json", summary)
        _render_variant(variant_dir / "pnl_diagnostics.html", result, config)
        variants.append(result)
    _render_comparison(run_dir / "comparison.html", variants)
    comparison = [result.summary for result in variants]
    _write_csv(run_dir / "comparison.csv", comparison)
    _write_json(run_dir / "comparison.json", comparison)
    candidate_summary = variants[1].summary
    status, checks = _gate(candidate_summary, selected.holdout_gate)
    _write_json(
        run_dir / "result.json",
        {
            "schema_version": 1,
            "status": status,
            "selected_variant": selected.selected_variant,
            "gate_checks": checks,
            "baseline": variants[0].summary,
            "candidate": candidate_summary,
            "holdout_rows": len(holdout),
            "label_audit": label_audit,
            "model": {
                "feature_names": [feature_names[index] for index in columns],
                "mean": logistic.mean.detach().cpu().tolist(),
                "scale": logistic.scale.detach().cpu().tolist(),
                "coefficients": logistic.coefficients.detach().cpu().tolist(),
                "iterations": logistic.iterations,
            },
        },
    )
    _write_json(
        run_dir / "run_record.json",
        {
            "schema_version": 1,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_seconds": time.monotonic() - started_monotonic,
            "source_commit": _git(["rev-parse", "HEAD"]),
            "selected_config": str(selected_config_path),
            "model_config_sha256": config.config_sha256,
            "proposal_sha256": selected.proposal_sha256,
            "dataset_manifest_sha256": selected.dataset_manifest_sha256,
            "chainlink_manifest_sha256": selected.chainlink_manifest_sha256,
            "gamma_universe_sha256": selected.gamma_universe_sha256,
            "development_rows": len(train),
            "holdout_rows": len(holdout),
            "chainlink_provenance": chainlink_provenance,
            "label_audit": label_audit,
            "device": str(device),
            "dtype": config.dtype,
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(device),
            "plotly_version": plotly.__version__,
            "python": platform.python_version(),
        },
    )
    return run_dir
