from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any


class WalkForwardConfigError(ValueError):
    """The frozen Stage 4c walk-forward contract is invalid."""


def _time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise WalkForwardConfigError(f"{name} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise WalkForwardConfigError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _increasing(values: tuple[float, ...], name: str) -> None:
    if not values or any(right <= left for left, right in pairwise(values)):
        raise WalkForwardConfigError(f"{name} must be nonempty and increasing")


@dataclass(frozen=True)
class FoldSpec:
    fold_id: str
    train_start: datetime
    train_end_exclusive: datetime
    validation_start: datetime
    validation_end_exclusive: datetime


@dataclass(frozen=True)
class WalkForwardConfig:
    schema_version: int
    experiment_id: str
    data_config: str
    dataset_config: str
    dataset_root: str
    assets: tuple[str, ...]
    strategy_configs: tuple[str, ...]
    folds: tuple[FoldSpec, ...]
    holdout_start: datetime
    holdout_end_exclusive: datetime
    minimum_ask_grid: tuple[float, ...]
    maximum_ask_grid: tuple[float, ...]
    minimum_net_edge_grid: tuple[float, ...]
    persistence_train_quantiles: tuple[float, ...]
    minimum_support_grid: tuple[int, ...]
    selection_extra_cost_per_share: float
    stress_extra_cost_per_share: float
    minimum_fold_fills: int
    minimum_fold_fill_fraction: float
    minimum_pooled_fills: int
    minimum_pooled_fill_fraction: float
    maximum_pooled_fill_fraction: float
    minimum_positive_folds: int
    minimum_positive_stress_folds: int
    minimum_pooled_profit_factor: float
    maximum_fold_drawdown_usdc: float
    neighbor_minimum_pnl_fraction: float
    minimum_neighbor_positive_folds: int
    holdout_minimum_fills: int
    holdout_minimum_fill_fraction: float
    holdout_minimum_profit_factor: float
    holdout_maximum_drawdown_usdc: float
    holdout_minimum_half_pnl_usdc: float
    holdout_minimum_stress_pnl_usdc: float
    holdout_maximum_single_day_profit_share: float
    seed: int
    device: str
    dtype: str
    config_sha256: str


def load_walkforward_config(path: str | Path) -> WalkForwardConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = set(WalkForwardConfig.__dataclass_fields__) - {"config_sha256"}
    if payload.keys() != required:
        raise WalkForwardConfigError(
            f"walk-forward fields differ: missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    if payload["schema_version"] != 1 or len(payload["strategy_configs"]) != 5:
        raise WalkForwardConfigError("Stage 4c requires schema 1 and five families")
    folds = tuple(
        FoldSpec(
            fold_id=str(item["fold_id"]),
            train_start=_time(item["train_start"], "train_start"),
            train_end_exclusive=_time(
                item["train_end_exclusive"], "train_end_exclusive"
            ),
            validation_start=_time(item["validation_start"], "validation_start"),
            validation_end_exclusive=_time(
                item["validation_end_exclusive"], "validation_end_exclusive"
            ),
        )
        for item in payload["folds"]
    )
    if len(folds) != 4 or len({fold.fold_id for fold in folds}) != len(folds):
        raise WalkForwardConfigError("Stage 4c requires four unique folds")
    first_start = folds[0].train_start
    for index, fold in enumerate(folds):
        if (
            fold.train_start != first_start
            or fold.train_end_exclusive != fold.validation_start
            or fold.validation_end_exclusive <= fold.validation_start
            or (index and folds[index - 1].validation_end_exclusive != fold.validation_start)
        ):
            raise WalkForwardConfigError("folds must be contiguous expanding windows")
    holdout_start = _time(payload["holdout_start"], "holdout_start")
    holdout_end = _time(payload["holdout_end_exclusive"], "holdout_end_exclusive")
    if folds[-1].validation_end_exclusive != holdout_start or holdout_end <= holdout_start:
        raise WalkForwardConfigError("holdout must follow the final validation fold")
    minimum_ask = tuple(float(value) for value in payload["minimum_ask_grid"])
    maximum_ask = tuple(float(value) for value in payload["maximum_ask_grid"])
    minimum_edge = tuple(float(value) for value in payload["minimum_net_edge_grid"])
    quantiles = tuple(float(value) for value in payload["persistence_train_quantiles"])
    support = tuple(int(value) for value in payload["minimum_support_grid"])
    for values, name in (
        (minimum_ask, "minimum_ask_grid"),
        (maximum_ask, "maximum_ask_grid"),
        (minimum_edge, "minimum_net_edge_grid"),
        (quantiles, "persistence_train_quantiles"),
        (tuple(float(value) for value in support), "minimum_support_grid"),
    ):
        _increasing(values, name)
    if (
        minimum_ask[0] <= 0
        or minimum_ask[-1] >= 1
        or maximum_ask[0] <= 0
        or maximum_ask[-1] >= 1
        or minimum_edge[0] < 0
        or quantiles[0] < 0
        or quantiles[-1] > 1
        or support[0] < 1
    ):
        raise WalkForwardConfigError("grid values are outside supported domains")
    fractions = (
        float(payload["minimum_fold_fill_fraction"]),
        float(payload["minimum_pooled_fill_fraction"]),
        float(payload["maximum_pooled_fill_fraction"]),
        float(payload["neighbor_minimum_pnl_fraction"]),
        float(payload["holdout_minimum_fill_fraction"]),
        float(payload["holdout_maximum_single_day_profit_share"]),
    )
    if any(not 0 < value <= 1 for value in fractions):
        raise WalkForwardConfigError("fractional gates must be in (0, 1]")
    if fractions[1] >= fractions[2]:
        raise WalkForwardConfigError("pooled fill bounds are inverted")
    positive_fields = (
        "selection_extra_cost_per_share",
        "stress_extra_cost_per_share",
        "minimum_fold_fills",
        "minimum_pooled_fills",
        "minimum_positive_folds",
        "minimum_positive_stress_folds",
        "minimum_pooled_profit_factor",
        "maximum_fold_drawdown_usdc",
        "minimum_neighbor_positive_folds",
        "holdout_minimum_fills",
        "holdout_minimum_profit_factor",
        "holdout_maximum_drawdown_usdc",
    )
    if any(float(payload[name]) <= 0 for name in positive_fields):
        raise WalkForwardConfigError("exposure, cost and risk gates must be positive")
    if (
        int(payload["minimum_positive_folds"]) > len(folds)
        or int(payload["minimum_positive_stress_folds"]) > len(folds)
        or int(payload["minimum_neighbor_positive_folds"]) > len(folds)
    ):
        raise WalkForwardConfigError("positive-fold gate exceeds fold count")
    if float(payload["stress_extra_cost_per_share"]) <= float(
        payload["selection_extra_cost_per_share"]
    ):
        raise WalkForwardConfigError("stress cost must exceed selection cost")
    if payload["device"] not in {"cpu", "cuda"} or payload["dtype"] != "float64":
        raise WalkForwardConfigError("Stage 4c requires cpu|cuda and float64")
    assets = tuple(str(value).upper() for value in payload["assets"])
    if assets != ("BTC", "ETH", "SOL", "XRP"):
        raise WalkForwardConfigError("Stage 4c requires the four frozen Kacho assets")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    normalized: dict[str, Any] = dict(payload)
    normalized.update(
        {
            "strategy_configs": tuple(str(value) for value in payload["strategy_configs"]),
            "assets": assets,
            "folds": folds,
            "holdout_start": holdout_start,
            "holdout_end_exclusive": holdout_end,
            "minimum_ask_grid": minimum_ask,
            "maximum_ask_grid": maximum_ask,
            "minimum_net_edge_grid": minimum_edge,
            "persistence_train_quantiles": quantiles,
            "minimum_support_grid": support,
            "config_sha256": hashlib.sha256(canonical).hexdigest(),
        }
    )
    return WalkForwardConfig(**normalized)
