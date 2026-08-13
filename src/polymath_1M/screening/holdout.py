from __future__ import annotations

import json
import math
import platform
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import torch

from polymath_1M.strategy.model import (
    Evaluation,
    LookupModel,
    evaluate_batch,
    fit_lookup_model,
)
from polymath_1M.strategy.parameters import StrategyConfig, load_strategy_config

from .calibration import _base_evaluation, _funnel, _git, _sha256, _write_json
from .calibration_config import CalibrationConfig, load_calibration_config
from .config import ScreeningConfig, load_screening_config
from .run import (
    ScreeningData,
    _decision_rows,
    _load_rows,
    _strategy_data,
    chronological_market_splits,
    evaluation_summary,
    literal_one_step_model,
)


class HoldoutRunError(RuntimeError):
    """The one-shot Stage 4b holdout contract cannot be executed safely."""


@dataclass(frozen=True)
class SelectedConfig:
    schema_version: int
    experiment_id: str
    calibration_config: str
    calibration_config_sha256: str
    stage4_config: str
    stage4_config_sha256: str
    dataset_manifest: str
    dataset_manifest_sha256: str
    proposal: str
    proposal_sha256: str
    selected: dict[str, Any]


def load_selected_config(path: str | Path) -> SelectedConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = set(SelectedConfig.__dataclass_fields__)
    if payload.keys() != required:
        raise HoldoutRunError(
            f"selected config fields differ: missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    selected = payload["selected"]
    selected_required = {
        "strategy_id",
        "strategy_config",
        "assets",
        "duration",
        "require_market_favorite",
        "target_notional_usdc",
        "minimum_ask",
        "maximum_ask",
        "minimum_net_edge",
        "minimum_persistence",
        "minimum_support",
        "persistence_quantile",
        "grid_indices",
        "validation",
        "selected_funnel",
    }
    if payload["schema_version"] != 1 or selected.keys() != selected_required:
        raise HoldoutRunError("selected config is not the frozen Stage 4b schema")
    if not 0 < float(selected["minimum_ask"]) < float(selected["maximum_ask"]) <= 1:
        raise HoldoutRunError("selected ask interval is invalid")
    if int(selected["minimum_support"]) < 1:
        raise HoldoutRunError("selected support must be positive")
    return SelectedConfig(**payload)


def _verify_frozen_inputs(
    selected: SelectedConfig,
) -> tuple[CalibrationConfig, ScreeningConfig, dict[str, Any]]:
    calibration = load_calibration_config(selected.calibration_config)
    stage4 = load_screening_config(selected.stage4_config)
    checks = (
        (calibration.config_sha256, selected.calibration_config_sha256, "calibration"),
        (stage4.config_sha256, selected.stage4_config_sha256, "Stage 4"),
        (
            _sha256(Path(selected.dataset_manifest)),
            selected.dataset_manifest_sha256,
            "dataset manifest",
        ),
        (_sha256(Path(selected.proposal)), selected.proposal_sha256, "proposal"),
    )
    for actual, expected, label in checks:
        if actual != expected:
            raise HoldoutRunError(f"{label} SHA-256 differs from selected config")
    proposal = json.loads(Path(selected.proposal).read_text(encoding="utf-8"))
    if proposal.get("status") != "selected" or proposal.get("selected") != selected.selected:
        raise HoldoutRunError("committed selection differs from calibration proposal")
    if calibration.stage4_config != selected.stage4_config:
        raise HoldoutRunError("calibration and selected Stage 4 configs differ")
    return calibration, stage4, proposal


def _evaluate_selected(
    data: ScreeningData,
    model: LookupModel,
    strategy: StrategyConfig,
    selected: dict[str, Any],
    *,
    cost: float,
    variant: str,
) -> Evaluation:
    forced_side = None
    support_gate = True
    persistence_gate = True
    edge_gate = True
    require_favorite = strategy.require_market_favorite
    model_to_use = model
    if variant == "literal_one_step":
        model_to_use = literal_one_step_model(model)
    elif variant == "range_only":
        forced_side = torch.argmax(data.batch.current_mid, dim=1)
        support_gate = False
        persistence_gate = False
        edge_gate = False
        require_favorite = False
    elif variant != "terminal_lookup":
        raise HoldoutRunError(f"unknown Stage 4b variant: {variant}")
    return evaluate_batch(
        data.batch,
        model_to_use,
        minimum_support=int(selected["minimum_support"]),
        minimum_persistence=float(selected["minimum_persistence"]),
        minimum_ask=float(selected["minimum_ask"]),
        maximum_ask=float(selected["maximum_ask"]),
        minimum_net_edge=float(selected["minimum_net_edge"]),
        target_notional_usdc=strategy.target_notional_usdc,
        platform_fee_rate=data.fee_rate,
        platform_fee_round_decimals=5,
        extra_cost_per_share=cost,
        require_market_favorite=require_favorite,
        forced_side=forced_side,
        apply_support_gate=support_gate,
        apply_persistence_gate=persistence_gate,
        apply_edge_gate=edge_gate,
    )


def _primary_metrics(
    test: ScreeningData,
    primary: Evaluation,
    stress: Evaluation,
) -> dict[str, Any]:
    half = len(test.batch) // 2
    filled_pnl = primary.net_pnl[primary.filled]
    positive = torch.clamp(filled_pnl, min=0).sum()
    negative = -torch.clamp(filled_pnl, max=0).sum()
    has_losses = bool((negative > 0).item())
    has_profits = bool((positive > 0).item())
    profit_factor = float((positive / negative).item()) if has_losses else None
    return {
        "fills": int(primary.filled.sum().item()),
        "fill_fraction": float(primary.filled.to(torch.float64).mean().item()),
        "net_pnl_usdc": float(primary.net_pnl.sum().item()),
        "profit_factor": profit_factor,
        "profit_factor_infinite": not has_losses and has_profits,
        "half1_pnl_usdc": float(primary.net_pnl[:half].sum().item()),
        "half2_pnl_usdc": float(primary.net_pnl[half:].sum().item()),
        "stress_fills": int(stress.filled.sum().item()),
        "stress_net_pnl_usdc": float(stress.net_pnl.sum().item()),
    }


def _test_gate(
    summary: dict[str, Any],
    metrics: dict[str, Any],
    calibration: CalibrationConfig,
) -> tuple[str, list[str], list[str]]:
    markets = int(summary["markets"])
    required_fills = max(
        calibration.test_minimum_fills,
        math.ceil(calibration.test_minimum_fill_fraction * markets),
    )
    adequacy_failures: list[str] = []
    if summary["valid_snapshots"] == 0:
        adequacy_failures.append("no_valid_test_snapshots")
    if metrics["fills"] < required_fills:
        adequacy_failures.append(f"minimum_test_fills:{required_fills}")
    if adequacy_failures:
        return "inconclusive", adequacy_failures, []
    failures: list[str] = []
    if metrics["net_pnl_usdc"] <= 0:
        failures.append("positive_net_pnl")
    profit_factor_pass = metrics["profit_factor_infinite"] or (
        metrics["profit_factor"] is not None
        and metrics["profit_factor"] >= calibration.test_minimum_profit_factor
    )
    if not profit_factor_pass:
        failures.append("minimum_profit_factor")
    if summary["max_drawdown_usdc"] > calibration.test_maximum_drawdown_usdc:
        failures.append("maximum_drawdown")
    if metrics["half1_pnl_usdc"] <= calibration.test_minimum_half_pnl_usdc:
        failures.append("positive_half1_pnl")
    if metrics["half2_pnl_usdc"] <= calibration.test_minimum_half_pnl_usdc:
        failures.append("positive_half2_pnl")
    if (
        metrics["stress_fills"] == 0
        or metrics["stress_net_pnl_usdc"] <= calibration.test_minimum_stress_pnl_usdc
    ):
        failures.append("positive_stress_pnl")
    day_share = summary["maximum_positive_day_share"]
    if (
        day_share is None
        or day_share > calibration.test_maximum_single_day_profit_share
    ):
        failures.append("daily_profit_concentration")
    return ("pass" if not failures else "fail"), [], failures


def run_stage4b_test(
    selected_config_path: str | Path,
    output_root: str | Path = "outputs/screening",
) -> Path:
    selected_config = load_selected_config(selected_config_path)
    calibration, stage4, _ = _verify_frozen_inputs(selected_config)
    if _git(["status", "--porcelain"]):
        raise HoldoutRunError("commit the frozen selection and runner before opening test")
    device = torch.device(calibration.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise HoldoutRunError("frozen CUDA device is unavailable")
    run_dir = Path(output_root) / f"{selected_config.experiment_id}_test"
    if run_dir.exists():
        raise HoldoutRunError(f"canonical test artifact already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    _write_json(
        run_dir / "test_opened.json",
        {
            "opened_at": started.isoformat(),
            "source_commit": source_commit,
            "selected_config": str(selected_config_path),
            "proposal_sha256": selected_config.proposal_sha256,
            "dataset_manifest_sha256": selected_config.dataset_manifest_sha256,
        },
    )
    torch.manual_seed(calibration.seed)
    rows, dataset_manifest = _load_rows(stage4)
    strategy = load_strategy_config(selected_config.selected["strategy_config"])
    if strategy.strategy_id != selected_config.selected["strategy_id"]:
        raise HoldoutRunError("selected strategy ID differs from strategy config")
    data = _strategy_data(rows, strategy, stage4)
    split_indices = chronological_market_splits(
        data.batch.market_start_s, stage4.train_fraction, stage4.validation_fraction
    )
    train = data.index(split_indices["train"]).to(device)
    test = data.index(split_indices["test"]).to(device)
    edges = torch.tensor(stage4.price_bin_edges, dtype=torch.float64, device=device)
    model = fit_lookup_model(
        train.batch,
        edges,
        terminal_alpha=stage4.terminal_alpha,
        transition_alpha=stage4.transition_alpha,
    )
    primary = _evaluate_selected(
        test,
        model,
        strategy,
        selected_config.selected,
        cost=calibration.selection_extra_cost_per_share,
        variant="terminal_lookup",
    )
    stress = _evaluate_selected(
        test,
        model,
        strategy,
        selected_config.selected,
        cost=calibration.stress_extra_cost_per_share,
        variant="terminal_lookup",
    )
    primary_summary = evaluation_summary(test, primary)
    primary_metrics = _primary_metrics(test, primary, stress)
    status, adequacy_failures, primary_failures = _test_gate(
        primary_summary, primary_metrics, calibration
    )
    controls: dict[str, Any] = {}
    decisions = _decision_rows(
        test,
        primary,
        variant="terminal_lookup",
        cost=calibration.selection_extra_cost_per_share,
    )
    decisions.extend(
        _decision_rows(
            test,
            stress,
            variant="terminal_lookup_stress",
            cost=calibration.stress_extra_cost_per_share,
        )
    )
    for variant in ("literal_one_step", "range_only"):
        result = _evaluate_selected(
            test,
            model,
            strategy,
            selected_config.selected,
            cost=calibration.selection_extra_cost_per_share,
            variant=variant,
        )
        controls[variant] = evaluation_summary(test, result)
        decisions.extend(
            _decision_rows(
                test,
                result,
                variant=variant,
                cost=calibration.selection_extra_cost_per_share,
            )
        )
    base = _base_evaluation(
        test,
        model,
        strategy,
        cost=calibration.selection_extra_cost_per_share,
    )
    funnel = _funnel(
        test,
        base,
        strategy,
        minimum_support=int(selected_config.selected["minimum_support"]),
        minimum_ask=float(selected_config.selected["minimum_ask"]),
        maximum_ask=float(selected_config.selected["maximum_ask"]),
        minimum_persistence=float(selected_config.selected["minimum_persistence"]),
        minimum_net_edge=float(selected_config.selected["minimum_net_edge"]),
    )
    pq.write_table(
        pa.Table.from_pylist(decisions),
        run_dir / "test_decisions.parquet",
        compression="zstd",
    )
    _write_json(
        run_dir / "model.json",
        {
            "edges": model.edges.detach().cpu().tolist(),
            "probability_up": model.probability_up.detach().cpu().tolist(),
            "support": model.support.detach().cpu().tolist(),
            "transition_matrix": model.transition_matrix.detach().cpu().tolist(),
            "persistence": model.persistence.detach().cpu().tolist(),
            "prior_up": float(model.prior_up.item()),
        },
    )
    result_payload = {
        "schema_version": 1,
        "experiment_id": selected_config.experiment_id,
        "status": status,
        "adequacy_failures": adequacy_failures,
        "primary_failures": primary_failures,
        "selected": selected_config.selected,
        "test_funnel": funnel,
        "primary": primary_summary,
        "primary_gate_metrics": primary_metrics,
        "stress": evaluation_summary(test, stress),
        "diagnostic_controls": controls,
    }
    _write_json(run_dir / "result.json", result_payload)
    artifact_names = [
        "test_opened.json",
        "test_decisions.parquet",
        "model.json",
        "result.json",
    ]
    _write_json(
        run_dir / "run_record.json",
        {
            "schema_version": 1,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_seconds": time.monotonic() - started_monotonic,
            "source_commit": source_commit,
            "source_dirty": False,
            "selected_config": str(selected_config_path),
            "calibration_config_sha256": calibration.config_sha256,
            "stage4_config_sha256": stage4.config_sha256,
            "proposal_sha256": selected_config.proposal_sha256,
            "dataset_manifest_sha256": selected_config.dataset_manifest_sha256,
            "dataset_market_count": dataset_manifest["market_count"],
            "dataset_valid_count": dataset_manifest["valid_count"],
            "seed": calibration.seed,
            "device": str(device),
            "dtype": calibration.dtype,
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "gpu": (
                torch.cuda.get_device_name(device) if device.type == "cuda" else None
            ),
            "pyarrow_version": pa.__version__,
            "python": platform.python_version(),
            "artifacts": [
                {
                    "path": name,
                    "bytes": (run_dir / name).stat().st_size,
                    "sha256": _sha256(run_dir / name),
                }
                for name in artifact_names
            ],
        },
    )
    return run_dir
