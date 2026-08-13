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
    _base_evaluation,
    _funnel,
    _git,
    _grid_metrics,
    _sha256,
    _write_json,
)
from .config import load_screening_config
from .kacho_gamma import load_kacho_gamma_data, select_strategy_data
from .plateau_config import PlateauConfig, load_plateau_config
from .run import ScreeningData
from .walkforward import PooledMetrics, _optional, _pool, _timestamp, _window
from .walkforward_config import FoldSpec


class PlateauRunError(RuntimeError):
    """Stage 4d cannot satisfy its frozen development-only contract."""


@dataclass(frozen=True)
class UniformGrid:
    indices: torch.Tensor
    minimum_ask: torch.Tensor
    maximum_ask: torch.Tensor
    minimum_net_edge: torch.Tensor
    minimum_persistence: torch.Tensor
    minimum_support: torch.Tensor

    def __len__(self) -> int:
        return self.indices.shape[0]


@dataclass(frozen=True)
class PlateauMetrics:
    member_count: torch.Tensor
    median_pnl: torch.Tensor
    median_stress_pnl: torch.Tensor
    median_positive_folds: torch.Tensor
    median_positive_stress_folds: torch.Tensor
    profitable_fraction: torch.Tensor
    stress_profitable_fraction: torch.Tensor
    minimum_pnl: torch.Tensor
    maximum_pnl: torch.Tensor


def _uniform_grid(config: PlateauConfig, device: torch.device) -> UniformGrid:
    dimensions = (
        torch.arange(len(config.minimum_ask_grid), device=device),
        torch.arange(len(config.maximum_ask_grid), device=device),
        torch.arange(len(config.minimum_net_edge_grid), device=device),
        torch.arange(len(config.minimum_persistence_grid), device=device),
    )
    mesh = torch.meshgrid(*dimensions, indexing="ij")
    indices = torch.stack([value.reshape(-1) for value in mesh], dim=1)
    minimum_ask_values = torch.tensor(
        config.minimum_ask_grid, dtype=torch.float64, device=device
    )
    maximum_ask_values = torch.tensor(
        config.maximum_ask_grid, dtype=torch.float64, device=device
    )
    edge_values = torch.tensor(
        config.minimum_net_edge_grid, dtype=torch.float64, device=device
    )
    persistence_values = torch.tensor(
        config.minimum_persistence_grid, dtype=torch.float64, device=device
    )
    minimum_ask = minimum_ask_values[indices[:, 0]]
    maximum_ask = maximum_ask_values[indices[:, 1]]
    keep = minimum_ask < maximum_ask
    indices = indices[keep]
    return UniformGrid(
        indices=indices,
        minimum_ask=minimum_ask[keep],
        maximum_ask=maximum_ask[keep],
        minimum_net_edge=edge_values[indices[:, 2]],
        minimum_persistence=persistence_values[indices[:, 3]],
        minimum_support=torch.full(
            (indices.shape[0],),
            config.minimum_support,
            dtype=torch.int64,
            device=device,
        ),
    )


def _plateau_members(grid: UniformGrid) -> tuple[torch.Tensor, torch.Tensor]:
    keys = [tuple(int(value) for value in row) for row in grid.indices.cpu().tolist()]
    lookup = {key: index for index, key in enumerate(keys)}
    members = torch.full(
        (len(grid), 9), -1, dtype=torch.int64, device=grid.indices.device
    )
    members[:, 0] = torch.arange(len(grid), device=grid.indices.device)
    for row_index, key in enumerate(keys):
        slot = 1
        for dimension in range(4):
            for delta in (-1, 1):
                candidate = list(key)
                candidate[dimension] += delta
                found = lookup.get(tuple(candidate))
                if found is not None:
                    members[row_index, slot] = found
                slot += 1
    return members, members >= 0


def _masked_median(values: torch.Tensor, exists: torch.Tensor) -> torch.Tensor:
    masked = torch.where(exists, values, torch.full_like(values, torch.inf))
    ordered = torch.sort(masked, dim=1).values
    counts = exists.sum(dim=1)
    lower = ((counts - 1) // 2).unsqueeze(1)
    upper = (counts // 2).unsqueeze(1)
    return (
        ordered.gather(1, lower).squeeze(1)
        + ordered.gather(1, upper).squeeze(1)
    ) / 2


def _plateau_metrics(
    pooled: PooledMetrics,
    members: torch.Tensor,
    exists: torch.Tensor,
) -> PlateauMetrics:
    selected = torch.clamp(members, min=0)
    pnl = pooled.pooled_net_pnl[selected]
    stress = pooled.pooled_stress_pnl[selected]
    positive_folds = pooled.positive_folds[selected].to(torch.float64)
    positive_stress_folds = pooled.positive_stress_folds[selected].to(torch.float64)
    counts = exists.sum(dim=1)
    return PlateauMetrics(
        member_count=counts,
        median_pnl=_masked_median(pnl, exists),
        median_stress_pnl=_masked_median(stress, exists),
        median_positive_folds=_masked_median(positive_folds, exists),
        median_positive_stress_folds=_masked_median(
            positive_stress_folds, exists
        ),
        profitable_fraction=((pnl > 0) & exists).sum(dim=1).to(torch.float64)
        / counts,
        stress_profitable_fraction=((stress > 0) & exists)
        .sum(dim=1)
        .to(torch.float64)
        / counts,
        minimum_pnl=torch.where(
            exists, pnl, torch.full_like(pnl, torch.inf)
        ).amin(dim=1),
        maximum_pnl=torch.where(
            exists, pnl, torch.full_like(pnl, -torch.inf)
        ).amax(dim=1),
    )


def _base_gates(
    pooled: PooledMetrics,
    config: PlateauConfig,
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
    minimum_pooled = max(
        config.minimum_pooled_fills,
        math.ceil(config.minimum_pooled_fill_fraction * sum(fold_markets)),
    )
    return {
        "fold_exposure": fold_exposure,
        "pooled_exposure": pooled.pooled_fill_count >= minimum_pooled,
        "pooled_positive_pnl": pooled.pooled_net_pnl > 0,
        "pooled_profit_factor": (
            pooled.pooled_profit_factor >= config.minimum_pooled_profit_factor
        ),
        "positive_fold_count": pooled.positive_folds >= config.minimum_positive_folds,
        "pooled_positive_stress_pnl": pooled.pooled_stress_pnl > 0,
        "positive_stress_fold_count": (
            pooled.positive_stress_folds >= config.minimum_positive_stress_folds
        ),
    }


def _plateau_gates(
    plateau: PlateauMetrics, config: PlateauConfig
) -> dict[str, torch.Tensor]:
    return {
        "plateau_median_positive_pnl": (
            plateau.median_pnl > config.minimum_plateau_median_pnl_usdc
        ),
        "plateau_median_positive_stress_pnl": (
            plateau.median_stress_pnl
            > config.minimum_plateau_median_stress_pnl_usdc
        ),
        "plateau_median_positive_fold_count": (
            plateau.median_positive_folds
            >= config.minimum_plateau_median_positive_folds
        ),
        "plateau_median_positive_stress_fold_count": (
            plateau.median_positive_stress_folds
            >= config.minimum_plateau_median_positive_stress_folds
        ),
    }


def _fit_fold(
    data: ScreeningData,
    fold: FoldSpec,
    strategy: StrategyConfig,
    config: PlateauConfig,
    grid: UniformGrid,
    edges: torch.Tensor,
    terminal_alpha: float,
    transition_alpha: float,
    device: torch.device,
) -> tuple[GridMetrics, dict[str, Any], int]:
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
    primary = _base_evaluation(
        validation, model, strategy, cost=config.selection_extra_cost_per_share
    )
    stress = _base_evaluation(
        validation, model, strategy, cost=config.stress_extra_cost_per_share
    )
    metrics = _grid_metrics(validation, primary, stress, grid)
    funnel = _funnel(
        validation,
        primary,
        strategy,
        minimum_support=config.minimum_support,
        minimum_ask=strategy.minimum_ask,
        maximum_ask=strategy.maximum_ask,
        minimum_persistence=strategy.minimum_persistence,
        minimum_net_edge=strategy.minimum_net_edge,
    )
    return metrics, funnel, len(validation.batch)


def _record(
    index: int,
    strategy_path: str,
    strategy: StrategyConfig,
    grid: UniformGrid,
    pooled: PooledMetrics,
    plateau: PlateauMetrics,
    config: PlateauConfig,
) -> dict[str, Any]:
    fold_rows = [
        {
            "fold_id": fold.fold_id,
            "fills": int(pooled.fold_fill_count[fold_index, index].item()),
            "net_pnl_usdc": float(pooled.fold_net_pnl[fold_index, index].item()),
            "stress_net_pnl_usdc": float(
                pooled.fold_stress_pnl[fold_index, index].item()
            ),
            "max_drawdown_usdc": float(
                pooled.fold_max_drawdown[fold_index, index].item()
            ),
        }
        for fold_index, fold in enumerate(config.folds)
    ]
    return {
        "strategy_id": strategy.strategy_id,
        "strategy_config": strategy_path,
        "assets": list(strategy.assets),
        "duration": strategy.duration,
        "target_notional_usdc": strategy.target_notional_usdc,
        "minimum_ask": float(grid.minimum_ask[index].item()),
        "maximum_ask": float(grid.maximum_ask[index].item()),
        "minimum_net_edge": float(grid.minimum_net_edge[index].item()),
        "minimum_persistence": float(grid.minimum_persistence[index].item()),
        "minimum_support": config.minimum_support,
        "grid_indices": [
            int(value) for value in grid.indices[index].detach().cpu().tolist()
        ],
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
        },
        "plateau": {
            "member_count": int(plateau.member_count[index].item()),
            "median_pnl_usdc": float(plateau.median_pnl[index].item()),
            "median_stress_pnl_usdc": float(
                plateau.median_stress_pnl[index].item()
            ),
            "median_positive_folds": float(
                plateau.median_positive_folds[index].item()
            ),
            "median_positive_stress_folds": float(
                plateau.median_positive_stress_folds[index].item()
            ),
            "profitable_fraction": float(
                plateau.profitable_fraction[index].item()
            ),
            "stress_profitable_fraction": float(
                plateau.stress_profitable_fraction[index].item()
            ),
            "minimum_pnl_usdc": float(plateau.minimum_pnl[index].item()),
            "maximum_pnl_usdc": float(plateau.maximum_pnl[index].item()),
        },
    }


def _selection_key(record: dict[str, Any]) -> tuple[Any, ...]:
    plateau = record["plateau"]
    metrics = record["walkforward"]
    return (
        -plateau["median_stress_pnl_usdc"],
        -plateau["median_pnl_usdc"],
        -metrics["positive_stress_folds"],
        -metrics["positive_folds"],
        -metrics["minimum_fold_pnl_usdc"],
        -metrics["pooled_stress_pnl_usdc"],
        -metrics["pooled_net_pnl_usdc"],
        metrics["pooled_fill_fraction"],
        record["strategy_id"],
        tuple(record["grid_indices"]),
    )


def run_stage4d_calibration(
    config_path: str | Path,
    output_root: str | Path = "outputs/calibration",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    source_dirty = bool(_git(["status", "--porcelain"]))
    config = load_plateau_config(config_path)
    data_config = load_screening_config(config.data_config)
    if tuple(config.strategy_configs) != tuple(data_config.strategy_configs):
        raise PlateauRunError("Stage 4d family lists differ")
    if data_config.period_start != config.folds[0].train_start:
        raise PlateauRunError("data start differs from first train start")
    if data_config.period_end_exclusive != config.holdout_end_exclusive:
        raise PlateauRunError("data end differs from frozen holdout end")
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise PlateauRunError("frozen CUDA device is unavailable")
    torch.manual_seed(config.seed)
    holdout_start_s = _timestamp(config.holdout_start)
    all_data, provenance = load_kacho_gamma_data(
        config,
        data_config,
        end_exclusive_s=holdout_start_s,
    )
    if provenance.gamma_rows_loaded != provenance.loaded_markets:
        raise PlateauRunError("Gamma/Kacho development row counts differ")
    edges = torch.tensor(data_config.price_bin_edges, dtype=torch.float64, device=device)
    grid = _uniform_grid(config, device)
    members, member_exists = _plateau_members(grid)
    family_results: list[dict[str, Any]] = []
    all_eligible: list[dict[str, Any]] = []
    for strategy_path in config.strategy_configs:
        strategy = load_strategy_config(strategy_path)
        data = select_strategy_data(all_data, strategy)
        metrics: list[GridMetrics] = []
        fold_markets: list[int] = []
        funnels: list[dict[str, Any]] = []
        for fold in config.folds:
            fold_metrics, funnel, market_count = _fit_fold(
                data,
                fold,
                strategy,
                config,
                grid,
                edges,
                data_config.terminal_alpha,
                data_config.transition_alpha,
                device,
            )
            metrics.append(fold_metrics)
            fold_markets.append(market_count)
            funnels.append({"fold_id": fold.fold_id, **funnel})
        pooled = _pool(metrics, fold_markets)
        plateau = _plateau_metrics(pooled, members, member_exists)
        base_gates = _base_gates(pooled, config, fold_markets)
        plateau_gates = _plateau_gates(plateau, config)
        gates = {**base_gates, **plateau_gates}
        eligible = torch.stack(tuple(gates.values())).all(dim=0)
        eligible_indices = (
            torch.nonzero(eligible, as_tuple=False).flatten().detach().cpu().tolist()
        )
        records = [
            _record(
                index,
                strategy_path,
                strategy,
                grid,
                pooled,
                plateau,
                config,
            )
            for index in eligible_indices
        ]
        records.sort(key=_selection_key)
        all_eligible.extend(records)
        family_results.append(
            {
                "strategy_id": strategy.strategy_id,
                "strategy_config": strategy_path,
                "grid_cells": len(grid),
                "eligible_cells": len(records),
                "independent_gate_pass_cells": {
                    name: int(mask.sum().item()) for name, mask in gates.items()
                },
                "fold_validation_markets": fold_markets,
                "original_funnels": funnels,
                "top_eligible": records[:25],
            }
        )
    all_eligible.sort(key=_selection_key)
    selected = all_eligible[0] if all_eligible else None
    proposal = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "status": "selected" if selected is not None else "inconclusive_no_candidate",
        "config_sha256": config.config_sha256,
        "data_config_sha256": data_config.config_sha256,
        "data_contract_sha256": data_config.data_contract_sha256,
        "dataset_config_sha256": provenance.dataset_config_sha256,
        "dataset_revision": provenance.dataset_revision,
        "dataset_manifest_sha256": provenance.dataset_manifest_sha256,
        "gamma_universe_manifest_sha256": provenance.gamma_universe_manifest_sha256,
        "gamma_universe_sha256": provenance.gamma_universe_sha256,
        "gamma_rows_loaded": provenance.gamma_rows_loaded,
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
            "proposal_sha256": _sha256(proposal_path),
            "holdout_opened": False,
            "device": str(device),
            "dtype": config.dtype,
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "gpu": (
                torch.cuda.get_device_name(device) if device.type == "cuda" else None
            ),
            "pyarrow_version": pa.__version__,
            "python": platform.python_version(),
        },
    )
    return run_dir
