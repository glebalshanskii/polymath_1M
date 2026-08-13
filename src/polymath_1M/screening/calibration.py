from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import torch

from polymath_1M.strategy.model import Evaluation, LookupModel, evaluate_batch, fit_lookup_model
from polymath_1M.strategy.parameters import StrategyConfig, load_strategy_config

from .calibration_config import CalibrationConfig, load_calibration_config
from .config import ScreeningConfig, load_screening_config
from .run import (
    ScreeningData,
    _load_rows,
    _strategy_data,
    chronological_market_splits,
)


class CalibrationRunError(RuntimeError):
    """Stage 4b calibration cannot satisfy its frozen execution contract."""


@dataclass(frozen=True)
class ParameterGrid:
    indices: torch.Tensor
    minimum_ask: torch.Tensor
    maximum_ask: torch.Tensor
    minimum_net_edge: torch.Tensor
    minimum_persistence: torch.Tensor
    minimum_support: torch.Tensor
    persistence_values: torch.Tensor

    def __len__(self) -> int:
        return self.indices.shape[0]


@dataclass(frozen=True)
class GridMetrics:
    fill_count: torch.Tensor
    fill_fraction: torch.Tensor
    net_pnl: torch.Tensor
    profit_factor: torch.Tensor
    max_drawdown: torch.Tensor
    half1_pnl: torch.Tensor
    half2_pnl: torch.Tensor
    stress_fill_count: torch.Tensor
    stress_net_pnl: torch.Tensor


def _git(args: list[str]) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _base_evaluation(
    data: ScreeningData,
    model: LookupModel,
    strategy: StrategyConfig,
    *,
    cost: float,
) -> Evaluation:
    if data.batch.ask_depth_prices.shape[2] != 1:
        raise CalibrationRunError("Stage 4b requires the frozen single-top fill contract")
    return evaluate_batch(
        data.batch,
        model,
        minimum_support=0,
        minimum_persistence=0.0,
        minimum_ask=0.0,
        maximum_ask=1.0,
        minimum_net_edge=float("-inf"),
        target_notional_usdc=strategy.target_notional_usdc,
        platform_fee_rate=data.fee_rate,
        platform_fee_round_decimals=5,
        extra_cost_per_share=cost,
        require_market_favorite=strategy.require_market_favorite,
        apply_support_gate=False,
        apply_persistence_gate=False,
        apply_edge_gate=False,
    )


def _parameter_grid(
    config: CalibrationConfig,
    train: ScreeningData,
    train_base: Evaluation,
) -> ParameterGrid:
    device = train.batch.current_mid.device
    dtype = train.batch.current_mid.dtype
    valid_persistence = train_base.persistence[
        train.batch.snapshot_valid & torch.isfinite(train_base.persistence)
    ]
    if valid_persistence.numel() == 0:
        raise CalibrationRunError("train has no valid persistence observations")
    quantiles = torch.tensor(
        config.persistence_train_quantiles, dtype=dtype, device=device
    )
    persistence_values = torch.quantile(valid_persistence, quantiles)
    dimensions = (
        torch.arange(len(config.minimum_ask_grid), device=device),
        torch.arange(len(config.maximum_ask_grid), device=device),
        torch.arange(len(config.minimum_net_edge_grid), device=device),
        torch.arange(len(config.persistence_train_quantiles), device=device),
        torch.arange(len(config.minimum_support_grid), device=device),
    )
    mesh = torch.meshgrid(*dimensions, indexing="ij")
    indices = torch.stack([value.reshape(-1) for value in mesh], dim=1)
    minimum_ask_values = torch.tensor(
        config.minimum_ask_grid, dtype=dtype, device=device
    )
    maximum_ask_values = torch.tensor(
        config.maximum_ask_grid, dtype=dtype, device=device
    )
    minimum_edge_values = torch.tensor(
        config.minimum_net_edge_grid, dtype=dtype, device=device
    )
    support_values = torch.tensor(config.minimum_support_grid, device=device)
    minimum_ask = minimum_ask_values[indices[:, 0]]
    maximum_ask = maximum_ask_values[indices[:, 1]]
    keep = minimum_ask < maximum_ask
    indices = indices[keep]
    return ParameterGrid(
        indices=indices,
        minimum_ask=minimum_ask[keep],
        maximum_ask=maximum_ask[keep],
        minimum_net_edge=minimum_edge_values[indices[:, 2]],
        minimum_persistence=persistence_values[indices[:, 3]],
        minimum_support=support_values[indices[:, 4]],
        persistence_values=persistence_values,
    )


def _eligibility_matrix(
    data: ScreeningData,
    primary: Evaluation,
    grid: ParameterGrid,
    *,
    edge: torch.Tensor,
    execution_filled: torch.Tensor,
) -> torch.Tensor:
    signal_ask = primary.signal_ask.unsqueeze(0)
    return (
        data.batch.snapshot_valid.unsqueeze(0)
        & (primary.support.unsqueeze(0) >= grid.minimum_support.unsqueeze(1))
        & (signal_ask >= grid.minimum_ask.unsqueeze(1))
        & (signal_ask <= grid.maximum_ask.unsqueeze(1))
        & (
            primary.persistence.unsqueeze(0)
            >= grid.minimum_persistence.unsqueeze(1)
        )
        & (edge.unsqueeze(0) >= grid.minimum_net_edge.unsqueeze(1))
        & execution_filled.unsqueeze(0)
        & (primary.fill_vwap.unsqueeze(0) <= grid.maximum_ask.unsqueeze(1))
    )


def _grid_metrics(
    data: ScreeningData,
    primary: Evaluation,
    stress: Evaluation,
    grid: ParameterGrid,
) -> GridMetrics:
    if not torch.equal(primary.side, stress.side):
        raise CalibrationRunError("uniform cost changed side selection")
    mask = _eligibility_matrix(
        data,
        primary,
        grid,
        edge=primary.net_edge,
        execution_filled=primary.filled,
    )
    pnl = mask * primary.net_pnl.unsqueeze(0)
    fills = mask.sum(dim=1)
    net = pnl.sum(dim=1)
    positive = torch.clamp(pnl, min=0).sum(dim=1)
    negative = -torch.clamp(pnl, max=0).sum(dim=1)
    profit_factor = torch.where(
        negative > 0,
        positive / negative,
            torch.where(
                positive > 0,
                torch.full_like(positive, torch.inf),
                torch.zeros_like(positive),
            ),
    )
    cumulative = torch.cumsum(pnl, dim=1)
    peaks = torch.cummax(
        torch.cat(
            (
                torch.zeros((len(grid), 1), dtype=pnl.dtype, device=pnl.device),
                cumulative,
            ),
            dim=1,
        ),
        dim=1,
    ).values[:, 1:]
    maximum_drawdown = (peaks - cumulative).amax(dim=1)
    half = data.batch.market_start_s.numel() // 2
    stress_mask = _eligibility_matrix(
        data,
        stress,
        grid,
        edge=stress.net_edge,
        execution_filled=stress.filled,
    )
    stress_pnl = stress_mask * stress.net_pnl.unsqueeze(0)
    return GridMetrics(
        fill_count=fills,
        fill_fraction=fills.to(dtype=pnl.dtype) / data.batch.market_start_s.numel(),
        net_pnl=net,
        profit_factor=profit_factor,
        max_drawdown=maximum_drawdown,
        half1_pnl=pnl[:, :half].sum(dim=1),
        half2_pnl=pnl[:, half:].sum(dim=1),
        stress_fill_count=stress_mask.sum(dim=1),
        stress_net_pnl=stress_pnl.sum(dim=1),
    )


def _neighbor_floor(grid: ParameterGrid, net_pnl: torch.Tensor) -> torch.Tensor:
    cpu_indices = [
        tuple(int(value) for value in row) for row in grid.indices.cpu().tolist()
    ]
    lookup = {value: index for index, value in enumerate(cpu_indices)}
    neighbors = torch.full(
        (len(grid), 10), -1, dtype=torch.int64, device=grid.indices.device
    )
    for row_index, key in enumerate(cpu_indices):
        slot = 0
        for dimension in range(5):
            for delta in (-1, 1):
                candidate = list(key)
                candidate[dimension] += delta
                found = lookup.get(tuple(candidate))
                if found is not None:
                    neighbors[row_index, slot] = found
                slot += 1
    exists = neighbors >= 0
    gathered = net_pnl[torch.clamp(neighbors, min=0)]
    gathered = torch.where(exists, gathered, torch.full_like(gathered, torch.inf))
    return gathered.amin(dim=1)


def _finite(value: torch.Tensor) -> float | None:
    scalar = float(value.item())
    return scalar if math.isfinite(scalar) else None


def _parameter_record(
    index: int,
    grid: ParameterGrid,
    metrics: GridMetrics,
    neighbor_floor: torch.Tensor,
    strategy: StrategyConfig,
    config: CalibrationConfig,
) -> dict[str, Any]:
    robust = torch.minimum(
        torch.minimum(metrics.net_pnl[index], 2 * metrics.half1_pnl[index]),
        torch.minimum(2 * metrics.half2_pnl[index], metrics.stress_net_pnl[index]),
    )
    grid_indices = grid.indices[index].detach().cpu().tolist()
    return {
        "strategy_id": strategy.strategy_id,
        "strategy_config": next(
            path
            for path in config.strategy_configs
            if load_strategy_config(path).strategy_id == strategy.strategy_id
        ),
        "assets": list(strategy.assets),
        "duration": strategy.duration,
        "require_market_favorite": strategy.require_market_favorite,
        "target_notional_usdc": strategy.target_notional_usdc,
        "minimum_ask": float(grid.minimum_ask[index].item()),
        "maximum_ask": float(grid.maximum_ask[index].item()),
        "minimum_net_edge": float(grid.minimum_net_edge[index].item()),
        "minimum_persistence": float(grid.minimum_persistence[index].item()),
        "minimum_support": int(grid.minimum_support[index].item()),
        "persistence_quantile": config.persistence_train_quantiles[grid_indices[3]],
        "grid_indices": grid_indices,
        "validation": {
            "fills": int(metrics.fill_count[index].item()),
            "fill_fraction": float(metrics.fill_fraction[index].item()),
            "net_pnl_usdc": float(metrics.net_pnl[index].item()),
            "profit_factor": _finite(metrics.profit_factor[index]),
            "profit_factor_infinite": bool(
                torch.isinf(metrics.profit_factor[index]).item()
            ),
            "max_drawdown_usdc": float(metrics.max_drawdown[index].item()),
            "half1_pnl_usdc": float(metrics.half1_pnl[index].item()),
            "half2_pnl_usdc": float(metrics.half2_pnl[index].item()),
            "stress_fills": int(metrics.stress_fill_count[index].item()),
            "stress_net_pnl_usdc": float(metrics.stress_net_pnl[index].item()),
            "neighbor_floor_pnl_usdc": float(neighbor_floor[index].item()),
            "robust_score": float(robust.item()),
        },
    }


def _eligible(
    metrics: GridMetrics,
    neighbor_floor: torch.Tensor,
    config: CalibrationConfig,
    markets: int,
) -> torch.Tensor:
    minimum_fills = max(
        config.minimum_validation_fills,
        math.ceil(config.minimum_fill_fraction * markets),
    )
    maximum_fills = math.floor(config.maximum_fill_fraction * markets)
    return (
        (metrics.fill_count >= minimum_fills)
        & (metrics.fill_count <= maximum_fills)
        & (metrics.net_pnl > 0)
        & (metrics.profit_factor >= config.minimum_profit_factor)
        & (metrics.max_drawdown <= config.maximum_drawdown_usdc)
        & (metrics.half1_pnl > config.minimum_half_pnl_usdc)
        & (metrics.half2_pnl > config.minimum_half_pnl_usdc)
        & (metrics.stress_fill_count > 0)
        & (metrics.stress_net_pnl > config.minimum_stress_pnl_usdc)
        & (neighbor_floor > 0)
        & (
            neighbor_floor
            >= config.neighbor_minimum_pnl_fraction * metrics.net_pnl
        )
    )


def _selection_key(record: dict[str, Any]) -> tuple[Any, ...]:
    validation = record["validation"]
    profit_factor = (
        math.inf if validation["profit_factor_infinite"] else validation["profit_factor"]
    )
    return (
        -validation["robust_score"],
        -validation["net_pnl_usdc"],
        -profit_factor,
        validation["fill_fraction"],
        record["strategy_id"],
        tuple(record["grid_indices"]),
    )


def _funnel(
    data: ScreeningData,
    base: Evaluation,
    strategy: StrategyConfig,
    *,
    minimum_support: int,
    minimum_ask: float,
    maximum_ask: float,
    minimum_persistence: float,
    minimum_net_edge: float,
) -> dict[str, int]:
    eligible = data.batch.snapshot_valid.clone()
    result = {"markets": len(data.batch), "snapshot_valid": int(eligible.sum().item())}
    side_policy = base.status_code != 7
    if strategy.require_market_favorite:
        side_policy &= base.side == torch.argmax(data.batch.current_mid, dim=1)
    eligible &= side_policy
    result["side_policy_pass"] = int(eligible.sum().item())
    eligible &= base.support >= minimum_support
    result["support_pass"] = int(eligible.sum().item())
    eligible &= (base.signal_ask >= minimum_ask) & (base.signal_ask <= maximum_ask)
    result["range_pass"] = int(eligible.sum().item())
    eligible &= base.persistence >= minimum_persistence
    result["persistence_pass"] = int(eligible.sum().item())
    eligible &= base.net_edge >= minimum_net_edge
    result["edge_pass"] = int(eligible.sum().item())
    eligible &= base.filled & (base.fill_vwap <= maximum_ask)
    result["execution_liquidity"] = int(eligible.sum().item())
    result["filled"] = result["execution_liquidity"]
    return result


def run_stage4b_calibration(
    config_path: str | Path,
    output_root: str | Path = "outputs/calibration",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    source_dirty = bool(_git(["status", "--porcelain"]))
    config = load_calibration_config(config_path)
    stage4 = load_screening_config(config.stage4_config)
    if tuple(config.strategy_configs) != tuple(stage4.strategy_configs):
        raise CalibrationRunError("Stage 4b family list differs from Stage 4")
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise CalibrationRunError("frozen CUDA device is unavailable")
    torch.manual_seed(config.seed)
    rows, dataset_manifest = _load_rows(stage4)
    dataset_manifest_path = Path(stage4.data_root) / "pmxt_dataset_manifest.json"
    dataset_manifest_sha256 = _sha256(dataset_manifest_path)
    edges = torch.tensor(stage4.price_bin_edges, dtype=torch.float64, device=device)
    family_results: list[dict[str, Any]] = []
    all_eligible: list[dict[str, Any]] = []
    for strategy_path in config.strategy_configs:
        strategy = load_strategy_config(strategy_path)
        data = _strategy_data(rows, strategy, stage4)
        indices = chronological_market_splits(
            data.batch.market_start_s, stage4.train_fraction, stage4.validation_fraction
        )
        train = data.index(indices["train"]).to(device)
        validation = data.index(indices["validation"]).to(device)
        model = fit_lookup_model(
            train.batch,
            edges,
            terminal_alpha=stage4.terminal_alpha,
            transition_alpha=stage4.transition_alpha,
        )
        train_primary = _base_evaluation(
            train, model, strategy, cost=config.selection_extra_cost_per_share
        )
        validation_primary = _base_evaluation(
            validation, model, strategy, cost=config.selection_extra_cost_per_share
        )
        validation_stress = _base_evaluation(
            validation, model, strategy, cost=config.stress_extra_cost_per_share
        )
        grid = _parameter_grid(config, train, train_primary)
        metrics = _grid_metrics(
            validation, validation_primary, validation_stress, grid
        )
        neighbor_floor = _neighbor_floor(grid, metrics.net_pnl)
        eligible_mask = _eligible(metrics, neighbor_floor, config, len(validation.batch))
        eligible_indices = (
            torch.nonzero(eligible_mask, as_tuple=False).flatten().tolist()
        )
        records = [
            _parameter_record(
                index, grid, metrics, neighbor_floor, strategy, config
            )
            for index in eligible_indices
        ]
        records.sort(key=_selection_key)
        all_eligible.extend(records)
        original_train_funnel = _funnel(
            train,
            train_primary,
            strategy,
            minimum_support=stage4.minimum_support,
            minimum_ask=strategy.minimum_ask,
            maximum_ask=strategy.maximum_ask,
            minimum_persistence=strategy.minimum_persistence,
            minimum_net_edge=strategy.minimum_net_edge,
        )
        original_validation_funnel = _funnel(
            validation,
            validation_primary,
            strategy,
            minimum_support=stage4.minimum_support,
            minimum_ask=strategy.minimum_ask,
            maximum_ask=strategy.maximum_ask,
            minimum_persistence=strategy.minimum_persistence,
            minimum_net_edge=strategy.minimum_net_edge,
        )
        family_results.append(
            {
                "strategy_id": strategy.strategy_id,
                "strategy_config": strategy_path,
                "train_markets": len(train.batch),
                "validation_markets": len(validation.batch),
                "grid_cells": len(grid),
                "persistence_thresholds": grid.persistence_values.cpu().tolist(),
                "eligible_cells": len(records),
                "original_funnel": {
                    "train": original_train_funnel,
                    "validation": original_validation_funnel,
                },
                "top_eligible": records[:25],
            }
        )
    all_eligible.sort(key=_selection_key)
    selected = all_eligible[0] if all_eligible else None
    if selected is not None:
        selected_strategy = load_strategy_config(selected["strategy_config"])
        selected_data = _strategy_data(rows, selected_strategy, stage4)
        selected_indices = chronological_market_splits(
            selected_data.batch.market_start_s,
            stage4.train_fraction,
            stage4.validation_fraction,
        )
        selected_train = selected_data.index(selected_indices["train"]).to(device)
        selected_validation = selected_data.index(
            selected_indices["validation"]
        ).to(device)
        selected_model = fit_lookup_model(
            selected_train.batch,
            edges,
            terminal_alpha=stage4.terminal_alpha,
            transition_alpha=stage4.transition_alpha,
        )
        selected["selected_funnel"] = {
            "train": _funnel(
                selected_train,
                _base_evaluation(
                    selected_train,
                    selected_model,
                    selected_strategy,
                    cost=config.selection_extra_cost_per_share,
                ),
                selected_strategy,
                minimum_support=selected["minimum_support"],
                minimum_ask=selected["minimum_ask"],
                maximum_ask=selected["maximum_ask"],
                minimum_persistence=selected["minimum_persistence"],
                minimum_net_edge=selected["minimum_net_edge"],
            ),
            "validation": _funnel(
                selected_validation,
                _base_evaluation(
                    selected_validation,
                    selected_model,
                    selected_strategy,
                    cost=config.selection_extra_cost_per_share,
                ),
                selected_strategy,
                minimum_support=selected["minimum_support"],
                minimum_ask=selected["minimum_ask"],
                maximum_ask=selected["maximum_ask"],
                minimum_persistence=selected["minimum_persistence"],
                minimum_net_edge=selected["minimum_net_edge"],
            ),
        }
    proposal = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "status": "selected" if selected is not None else "inconclusive_no_candidate",
        "calibration_config_sha256": config.config_sha256,
        "stage4_config_sha256": stage4.config_sha256,
        "data_contract_sha256": stage4.data_contract_sha256,
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "dataset_market_count": dataset_manifest["market_count"],
        "source_commit": source_commit,
        "families": family_results,
        "selected": selected,
    }
    run_dir = Path(output_root) / (
        f"{started.strftime('%Y%m%dT%H%M%SZ')}_{config.experiment_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    proposal_path = run_dir / "proposal.json"
    _write_json(proposal_path, proposal)
    proposal_sha256 = _sha256(proposal_path)
    if selected is not None:
        selected_proposal = {
            "schema_version": 1,
            "experiment_id": f"{config.experiment_id}_selected",
            "calibration_config": str(config_path),
            "calibration_config_sha256": config.config_sha256,
            "stage4_config": config.stage4_config,
            "stage4_config_sha256": stage4.config_sha256,
            "dataset_manifest": str(dataset_manifest_path),
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "proposal": str(proposal_path),
            "proposal_sha256": proposal_sha256,
            "selected": selected,
        }
        _write_json(run_dir / "selected_proposal.json", selected_proposal)
    record = {
        "schema_version": 1,
        "started_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "runtime_seconds": time.monotonic() - started_monotonic,
        "source_commit": source_commit,
        "source_dirty": source_dirty,
        "config_path": str(config_path),
        "config_sha256": config.config_sha256,
        "proposal_sha256": proposal_sha256,
        "device": str(device),
        "dtype": config.dtype,
        "torch_version": torch.__version__,
        "cuda_version": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(device) if device.type == "cuda" else None,
        "pyarrow_version": pa.__version__,
        "python": platform.python_version(),
    }
    _write_json(run_dir / "run_record.json", record)
    return run_dir
