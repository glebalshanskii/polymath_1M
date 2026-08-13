from __future__ import annotations

import math
import platform
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import torch

from polymath_1M.strategy.model import fit_lookup_model
from polymath_1M.strategy.parameters import StrategyConfig, load_strategy_config

from .calibration import (
    GridMetrics,
    ParameterGrid,
    _base_evaluation,
    _funnel,
    _git,
    _grid_metrics,
    _neighbor_floor,
    _parameter_grid,
    _sha256,
    _write_json,
)
from .config import load_screening_config
from .kacho_gamma import load_kacho_gamma_data, select_strategy_data
from .run import ScreeningData
from .walkforward_config import FoldSpec, WalkForwardConfig, load_walkforward_config


class WalkForwardRunError(RuntimeError):
    """Stage 4c cannot satisfy its frozen walk-forward contract."""


@dataclass(frozen=True)
class PooledMetrics:
    fold_fill_count: torch.Tensor
    fold_net_pnl: torch.Tensor
    fold_stress_pnl: torch.Tensor
    fold_max_drawdown: torch.Tensor
    pooled_fill_count: torch.Tensor
    pooled_fill_fraction: torch.Tensor
    pooled_net_pnl: torch.Tensor
    pooled_profit_factor: torch.Tensor
    pooled_stress_pnl: torch.Tensor
    positive_folds: torch.Tensor
    positive_stress_folds: torch.Tensor


def _timestamp(value: datetime) -> int:
    return int(value.timestamp())


def _window(data: ScreeningData, start: datetime, end: datetime) -> ScreeningData:
    starts = data.batch.market_start_s
    indices = torch.nonzero(
        (starts >= _timestamp(start)) & (starts < _timestamp(end)), as_tuple=False
    ).flatten()
    if indices.numel() == 0:
        raise WalkForwardRunError(f"empty market window: {start} to {end}")
    return data.index(indices)


def _pool(metrics: list[GridMetrics], markets: list[int]) -> PooledMetrics:
    fill_count = torch.stack([item.fill_count for item in metrics])
    net_pnl = torch.stack([item.net_pnl for item in metrics])
    stress_pnl = torch.stack([item.stress_net_pnl for item in metrics])
    drawdown = torch.stack([item.max_drawdown for item in metrics])
    gross_profit = torch.stack([item.gross_profit for item in metrics]).sum(dim=0)
    gross_loss = torch.stack([item.gross_loss for item in metrics]).sum(dim=0)
    profit_factor = torch.where(
        gross_loss > 0,
        gross_profit / gross_loss,
        torch.where(
            gross_profit > 0,
            torch.full_like(gross_profit, torch.inf),
            torch.zeros_like(gross_profit),
        ),
    )
    pooled_fills = fill_count.sum(dim=0)
    return PooledMetrics(
        fold_fill_count=fill_count,
        fold_net_pnl=net_pnl,
        fold_stress_pnl=stress_pnl,
        fold_max_drawdown=drawdown,
        pooled_fill_count=pooled_fills,
        pooled_fill_fraction=pooled_fills.to(net_pnl.dtype) / sum(markets),
        pooled_net_pnl=net_pnl.sum(dim=0),
        pooled_profit_factor=profit_factor,
        pooled_stress_pnl=stress_pnl.sum(dim=0),
        positive_folds=(net_pnl > 0).sum(dim=0),
        positive_stress_folds=(stress_pnl > 0).sum(dim=0),
    )


def _neighbor_positive_folds(
    grid: ParameterGrid, fold_pnl: torch.Tensor
) -> torch.Tensor:
    keys = [tuple(int(value) for value in row) for row in grid.indices.cpu().tolist()]
    lookup = {key: index for index, key in enumerate(keys)}
    neighbors = torch.full(
        (len(grid), 10), -1, dtype=torch.int64, device=grid.indices.device
    )
    for row_index, key in enumerate(keys):
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
    gathered = fold_pnl[:, torch.clamp(neighbors, min=0)].permute(1, 2, 0)
    counts = (gathered > 0).sum(dim=2)
    counts = torch.where(
        exists,
        counts,
        torch.full_like(counts, fold_pnl.shape[0]),
    )
    return counts.amin(dim=1)


def _eligible(
    pooled: PooledMetrics,
    neighbor_floor: torch.Tensor,
    neighbor_positive_folds: torch.Tensor,
    config: WalkForwardConfig,
    fold_markets: list[int],
) -> torch.Tensor:
    gates = _gate_masks(
        pooled,
        neighbor_floor,
        neighbor_positive_folds,
        config,
        fold_markets,
    )
    return torch.stack(tuple(gates.values())).all(dim=0)


def _gate_masks(
    pooled: PooledMetrics,
    neighbor_floor: torch.Tensor,
    neighbor_positive_folds: torch.Tensor,
    config: WalkForwardConfig,
    fold_markets: list[int],
) -> dict[str, torch.Tensor]:
    fold_exposure = torch.stack(
        [
            pooled.fold_fill_count[index]
            >= max(
                config.minimum_fold_fills,
                math.ceil(config.minimum_fold_fill_fraction * markets),
            )
            for index, markets in enumerate(fold_markets)
        ]
    ).all(dim=0)
    total_markets = sum(fold_markets)
    minimum_pooled = max(
        config.minimum_pooled_fills,
        math.ceil(config.minimum_pooled_fill_fraction * total_markets),
    )
    maximum_pooled = math.floor(config.maximum_pooled_fill_fraction * total_markets)
    return {
        "fold_exposure": fold_exposure,
        "pooled_minimum_exposure": pooled.pooled_fill_count >= minimum_pooled,
        "pooled_maximum_exposure": pooled.pooled_fill_count <= maximum_pooled,
        "pooled_positive_pnl": pooled.pooled_net_pnl > 0,
        "pooled_profit_factor": (
            pooled.pooled_profit_factor >= config.minimum_pooled_profit_factor
        ),
        "positive_fold_count": pooled.positive_folds >= config.minimum_positive_folds,
        "pooled_positive_stress_pnl": pooled.pooled_stress_pnl > 0,
        "positive_stress_fold_count": (
            pooled.positive_stress_folds >= config.minimum_positive_stress_folds
        ),
        "fold_drawdown": (
            pooled.fold_max_drawdown <= config.maximum_fold_drawdown_usdc
        ).all(dim=0),
        "positive_neighbor_floor": neighbor_floor > 0,
        "neighbor_pnl_fraction": (
            neighbor_floor
            >= config.neighbor_minimum_pnl_fraction * pooled.pooled_net_pnl
        ),
        "neighbor_positive_fold_count": (
            neighbor_positive_folds >= config.minimum_neighbor_positive_folds
        ),
    }


def _optional(value: torch.Tensor) -> float | None:
    scalar = float(value.item())
    return scalar if math.isfinite(scalar) else None


def _record(
    index: int,
    strategy_path: str,
    strategy: StrategyConfig,
    grids: list[ParameterGrid],
    pooled: PooledMetrics,
    neighbor_floor: torch.Tensor,
    neighbor_positive_folds: torch.Tensor,
    config: WalkForwardConfig,
) -> dict[str, Any]:
    grid = grids[0]
    indices = [int(value) for value in grid.indices[index].detach().cpu().tolist()]
    fold_rows = []
    for fold_index, fold in enumerate(config.folds):
        fold_rows.append(
            {
                "fold_id": fold.fold_id,
                "persistence_threshold": float(
                    grids[fold_index].minimum_persistence[index].item()
                ),
                "fills": int(pooled.fold_fill_count[fold_index, index].item()),
                "net_pnl_usdc": float(
                    pooled.fold_net_pnl[fold_index, index].item()
                ),
                "stress_net_pnl_usdc": float(
                    pooled.fold_stress_pnl[fold_index, index].item()
                ),
                "max_drawdown_usdc": float(
                    pooled.fold_max_drawdown[fold_index, index].item()
                ),
            }
        )
    return {
        "strategy_id": strategy.strategy_id,
        "strategy_config": strategy_path,
        "assets": list(strategy.assets),
        "duration": strategy.duration,
        "require_market_favorite": strategy.require_market_favorite,
        "target_notional_usdc": strategy.target_notional_usdc,
        "minimum_ask": float(grid.minimum_ask[index].item()),
        "maximum_ask": float(grid.maximum_ask[index].item()),
        "minimum_net_edge": float(grid.minimum_net_edge[index].item()),
        "persistence_quantile": config.persistence_train_quantiles[indices[3]],
        "minimum_support": int(grid.minimum_support[index].item()),
        "grid_indices": indices,
        "walkforward": {
            "folds": fold_rows,
            "pooled_fills": int(pooled.pooled_fill_count[index].item()),
            "pooled_fill_fraction": float(
                pooled.pooled_fill_fraction[index].item()
            ),
            "pooled_net_pnl_usdc": float(pooled.pooled_net_pnl[index].item()),
            "pooled_profit_factor": _optional(pooled.pooled_profit_factor[index]),
            "pooled_profit_factor_infinite": bool(
                torch.isinf(pooled.pooled_profit_factor[index]).item()
            ),
            "pooled_stress_pnl_usdc": float(
                pooled.pooled_stress_pnl[index].item()
            ),
            "positive_folds": int(pooled.positive_folds[index].item()),
            "positive_stress_folds": int(
                pooled.positive_stress_folds[index].item()
            ),
            "minimum_fold_pnl_usdc": float(
                pooled.fold_net_pnl[:, index].amin().item()
            ),
            "neighbor_floor_pnl_usdc": float(neighbor_floor[index].item()),
            "neighbor_minimum_positive_folds": int(
                neighbor_positive_folds[index].item()
            ),
        },
    }


def _selection_key(record: dict[str, Any]) -> tuple[Any, ...]:
    metrics = record["walkforward"]
    return (
        -metrics["positive_folds"],
        -metrics["minimum_fold_pnl_usdc"],
        -metrics["pooled_stress_pnl_usdc"],
        -metrics["pooled_net_pnl_usdc"],
        metrics["pooled_fill_fraction"],
        record["strategy_id"],
        tuple(record["grid_indices"]),
    )


def _diagnostic_key(record: dict[str, Any]) -> tuple[Any, ...]:
    metrics = record["walkforward"]
    diagnostic = record["diagnostic"]
    return (
        -diagnostic["passed_gates"],
        -metrics["positive_stress_folds"],
        -metrics["positive_folds"],
        -metrics["pooled_stress_pnl_usdc"],
        -metrics["pooled_net_pnl_usdc"],
        metrics["pooled_fill_fraction"],
        record["strategy_id"],
        tuple(record["grid_indices"]),
    )


def _fit_fold(
    data: ScreeningData,
    fold: FoldSpec,
    strategy: StrategyConfig,
    config: WalkForwardConfig,
    edges: torch.Tensor,
    terminal_alpha: float,
    transition_alpha: float,
    device: torch.device,
) -> tuple[ParameterGrid, GridMetrics, dict[str, Any], int]:
    train = _window(data, fold.train_start, fold.train_end_exclusive).to(device)
    validation = _window(
        data, fold.validation_start, fold.validation_end_exclusive
    ).to(device)
    model = fit_lookup_model(
        train.batch,
        edges,
        terminal_alpha=terminal_alpha,
        transition_alpha=transition_alpha,
    )
    train_base = _base_evaluation(
        train, model, strategy, cost=config.selection_extra_cost_per_share
    )
    primary = _base_evaluation(
        validation, model, strategy, cost=config.selection_extra_cost_per_share
    )
    stress = _base_evaluation(
        validation, model, strategy, cost=config.stress_extra_cost_per_share
    )
    grid = _parameter_grid(config, train, train_base)
    metrics = _grid_metrics(validation, primary, stress, grid)
    original_funnel = _funnel(
        validation,
        primary,
        strategy,
        minimum_support=20,
        minimum_ask=strategy.minimum_ask,
        maximum_ask=strategy.maximum_ask,
        minimum_persistence=strategy.minimum_persistence,
        minimum_net_edge=strategy.minimum_net_edge,
    )
    return grid, metrics, original_funnel, len(validation.batch)


def _selected_funnels(
    data: ScreeningData,
    strategy: StrategyConfig,
    selected: dict[str, Any],
    config: WalkForwardConfig,
    edges: torch.Tensor,
    terminal_alpha: float,
    transition_alpha: float,
    device: torch.device,
) -> list[dict[str, Any]]:
    result = []
    for fold_index, fold in enumerate(config.folds):
        train = _window(data, fold.train_start, fold.train_end_exclusive).to(device)
        validation = _window(
            data, fold.validation_start, fold.validation_end_exclusive
        ).to(device)
        model = fit_lookup_model(
            train.batch,
            edges,
            terminal_alpha=terminal_alpha,
            transition_alpha=transition_alpha,
        )
        base = _base_evaluation(
            validation,
            model,
            strategy,
            cost=config.selection_extra_cost_per_share,
        )
        result.append(
            {
                "fold_id": fold.fold_id,
                **_funnel(
                    validation,
                    base,
                    strategy,
                    minimum_support=selected["minimum_support"],
                    minimum_ask=selected["minimum_ask"],
                    maximum_ask=selected["maximum_ask"],
                    minimum_persistence=selected["walkforward"]["folds"][
                        fold_index
                    ]["persistence_threshold"],
                    minimum_net_edge=selected["minimum_net_edge"],
                ),
            }
        )
    return result


def run_stage4c_calibration(
    config_path: str | Path,
    output_root: str | Path = "outputs/calibration",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    source_dirty = bool(_git(["status", "--porcelain"]))
    config = load_walkforward_config(config_path)
    data_config = load_screening_config(config.data_config)
    if tuple(config.strategy_configs) != tuple(data_config.strategy_configs):
        raise WalkForwardRunError("Stage 4c family lists differ")
    if data_config.period_start != config.folds[0].train_start:
        raise WalkForwardRunError("data start differs from first train start")
    if data_config.period_end_exclusive != config.holdout_end_exclusive:
        raise WalkForwardRunError("data end differs from holdout end")
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise WalkForwardRunError("frozen CUDA device is unavailable")
    torch.manual_seed(config.seed)
    holdout_start_s = _timestamp(config.holdout_start)
    all_data, provenance = load_kacho_gamma_data(
        config,
        data_config,
        end_exclusive_s=holdout_start_s,
    )
    edges = torch.tensor(data_config.price_bin_edges, dtype=torch.float64, device=device)
    family_results: list[dict[str, Any]] = []
    all_eligible: list[dict[str, Any]] = []
    for strategy_path in config.strategy_configs:
        strategy = load_strategy_config(strategy_path)
        data = select_strategy_data(all_data, strategy)
        grids: list[ParameterGrid] = []
        metrics: list[GridMetrics] = []
        fold_markets: list[int] = []
        original_funnels: list[dict[str, Any]] = []
        for fold in config.folds:
            grid, fold_metrics, original, market_count = _fit_fold(
                data,
                fold,
                strategy,
                config,
                edges,
                data_config.terminal_alpha,
                data_config.transition_alpha,
                device,
            )
            if grids and not torch.equal(grid.indices, grids[0].indices):
                raise WalkForwardRunError("parameter grid indices changed across folds")
            grids.append(grid)
            metrics.append(fold_metrics)
            fold_markets.append(market_count)
            original_funnels.append({"fold_id": fold.fold_id, **original})
        pooled = _pool(metrics, fold_markets)
        neighbor_floor = _neighbor_floor(grids[0], pooled.pooled_net_pnl)
        neighbor_positive = _neighbor_positive_folds(
            grids[0], pooled.fold_net_pnl
        )
        gates = _gate_masks(
            pooled,
            neighbor_floor,
            neighbor_positive,
            config,
            fold_markets,
        )
        eligible = torch.stack(tuple(gates.values())).all(dim=0)
        eligible_indices = (
            torch.nonzero(eligible, as_tuple=False).flatten().detach().cpu().tolist()
        )
        records = [
            _record(
                index,
                strategy_path,
                strategy,
                grids,
                pooled,
                neighbor_floor,
                neighbor_positive,
                config,
            )
            for index in eligible_indices
        ]
        records.sort(key=_selection_key)
        all_eligible.extend(records)
        passed_gates = torch.stack(tuple(gates.values())).sum(dim=0)
        maximum_passed_gates = int(passed_gates.max().item())
        near_indices = (
            torch.nonzero(
                passed_gates == maximum_passed_gates, as_tuple=False
            )
            .flatten()
            .detach()
            .cpu()
            .tolist()
        )
        near_records = []
        for index in near_indices:
            record = _record(
                index,
                strategy_path,
                strategy,
                grids,
                pooled,
                neighbor_floor,
                neighbor_positive,
                config,
            )
            record["diagnostic"] = {
                "passed_gates": int(passed_gates[index].item()),
                "total_gates": len(gates),
                "failed_gates": [
                    name for name, mask in gates.items() if not bool(mask[index].item())
                ],
            }
            near_records.append(record)
        near_records.sort(key=_diagnostic_key)
        family_results.append(
            {
                "strategy_id": strategy.strategy_id,
                "strategy_config": strategy_path,
                "grid_cells": len(grids[0]),
                "eligible_cells": len(records),
                "independent_gate_pass_cells": {
                    name: int(mask.sum().item()) for name, mask in gates.items()
                },
                "maximum_passed_gates": maximum_passed_gates,
                "top_near_miss": near_records[:10],
                "fold_validation_markets": fold_markets,
                "persistence_thresholds_by_fold": [
                    grid.persistence_values.detach().cpu().tolist() for grid in grids
                ],
                "original_funnels": original_funnels,
                "top_eligible": records[:25],
            }
        )
    all_eligible.sort(key=_selection_key)
    selected = all_eligible[0] if all_eligible else None
    if selected is not None:
        strategy = load_strategy_config(selected["strategy_config"])
        data = select_strategy_data(all_data, strategy)
        selected["selected_funnels"] = _selected_funnels(
            data,
            strategy,
            selected,
            config,
            edges,
            data_config.terminal_alpha,
            data_config.transition_alpha,
            device,
        )
        development = _window(
            data, config.folds[0].train_start, config.holdout_start
        ).to(device)
        final_model = fit_lookup_model(
            development.batch,
            edges,
            terminal_alpha=data_config.terminal_alpha,
            transition_alpha=data_config.transition_alpha,
        )
        final_base = _base_evaluation(
            development,
            final_model,
            strategy,
            cost=config.selection_extra_cost_per_share,
        )
        valid = development.batch.snapshot_valid & torch.isfinite(
            final_base.persistence
        )
        selected["development_markets"] = len(development.batch)
        selected["development_persistence_threshold"] = float(
            torch.quantile(
                final_base.persistence[valid],
                torch.tensor(
                    selected["persistence_quantile"],
                    dtype=torch.float64,
                    device=device,
                ),
            ).item()
        )
    proposal = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "status": "selected" if selected is not None else "inconclusive_no_candidate",
        "walkforward_config_sha256": config.config_sha256,
        "data_config_sha256": data_config.config_sha256,
        "data_contract_sha256": data_config.data_contract_sha256,
        "dataset_config_sha256": provenance.dataset_config_sha256,
        "dataset_revision": provenance.dataset_revision,
        "dataset_manifest_sha256": provenance.dataset_manifest_sha256,
        "gamma_universe_manifest_sha256": provenance.gamma_universe_manifest_sha256,
        "gamma_universe_sha256": provenance.gamma_universe_sha256,
        "development_rows_loaded": provenance.loaded_markets,
        "development_valid_rows": provenance.valid_markets,
        "holdout_rows_loaded": 0,
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
        _write_json(
            run_dir / "selected_proposal.json",
            {
                "schema_version": 1,
                "experiment_id": f"{config.experiment_id}_selected",
                "walkforward_config": str(config_path),
                "walkforward_config_sha256": config.config_sha256,
                "data_config": config.data_config,
                "data_config_sha256": data_config.config_sha256,
                "dataset_config": config.dataset_config,
                "dataset_config_sha256": provenance.dataset_config_sha256,
                "dataset_manifest": provenance.dataset_manifest,
                "dataset_manifest_sha256": provenance.dataset_manifest_sha256,
                "gamma_universe_manifest": provenance.gamma_universe_manifest,
                "gamma_universe_manifest_sha256": (
                    provenance.gamma_universe_manifest_sha256
                ),
                "gamma_universe_sha256": provenance.gamma_universe_sha256,
                "proposal": str(proposal_path),
                "proposal_sha256": proposal_sha256,
                "selected": selected,
            },
        )
    _write_json(
        run_dir / "run_record.json",
        {
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
        },
    )
    return run_dir
