from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from .walkforward_config import FoldSpec


class PlateauConfigError(ValueError):
    """The frozen Stage 4d uniform-plateau contract is invalid."""


def _time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise PlateauConfigError(f"{name} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise PlateauConfigError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _uniform(
    values: tuple[float, ...], name: str, *, step: float
) -> None:
    if len(values) < 2:
        raise PlateauConfigError(f"{name} must contain at least two values")
    if any(right <= left for left, right in pairwise(values)):
        raise PlateauConfigError(f"{name} must be strictly increasing")
    if any(abs((right - left) - step) > 1e-12 for left, right in pairwise(values)):
        raise PlateauConfigError(f"{name} must use uniform step {step}")


@dataclass(frozen=True)
class PlateauConfig:
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
    minimum_persistence_grid: tuple[float, ...]
    minimum_support: int
    selection_extra_cost_per_share: float
    stress_extra_cost_per_share: float
    minimum_fold_fills: int
    minimum_fold_fill_fraction: float
    minimum_pooled_fills: int
    minimum_pooled_fill_fraction: float
    minimum_positive_folds: int
    minimum_positive_stress_folds: int
    minimum_pooled_profit_factor: float
    minimum_plateau_median_pnl_usdc: float
    minimum_plateau_median_stress_pnl_usdc: float
    minimum_plateau_median_positive_folds: int
    minimum_plateau_median_positive_stress_folds: int
    seed: int
    device: str
    dtype: str
    config_sha256: str


def load_plateau_config(path: str | Path) -> PlateauConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = set(PlateauConfig.__dataclass_fields__) - {"config_sha256"}
    if payload.keys() != required:
        raise PlateauConfigError(
            f"Stage 4d fields differ: missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    if payload["schema_version"] != 1 or len(payload["strategy_configs"]) != 5:
        raise PlateauConfigError("Stage 4d requires schema 1 and five families")
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
    if len(folds) != 4 or len({fold.fold_id for fold in folds}) != 4:
        raise PlateauConfigError("Stage 4d requires four unique folds")
    first_start = folds[0].train_start
    for index, fold in enumerate(folds):
        if (
            fold.train_start != first_start
            or fold.train_end_exclusive != fold.validation_start
            or fold.validation_end_exclusive <= fold.validation_start
            or (
                index
                and folds[index - 1].validation_end_exclusive
                != fold.validation_start
            )
        ):
            raise PlateauConfigError("folds must be contiguous expanding windows")
    holdout_start = _time(payload["holdout_start"], "holdout_start")
    holdout_end = _time(
        payload["holdout_end_exclusive"], "holdout_end_exclusive"
    )
    if folds[-1].validation_end_exclusive != holdout_start:
        raise PlateauConfigError("holdout must start after final validation")
    if holdout_end <= holdout_start:
        raise PlateauConfigError("holdout end must follow its start")
    minimum_ask = tuple(float(value) for value in payload["minimum_ask_grid"])
    maximum_ask = tuple(float(value) for value in payload["maximum_ask_grid"])
    minimum_edge = tuple(
        float(value) for value in payload["minimum_net_edge_grid"]
    )
    minimum_persistence = tuple(
        float(value) for value in payload["minimum_persistence_grid"]
    )
    _uniform(minimum_ask, "minimum_ask_grid", step=0.05)
    _uniform(maximum_ask, "maximum_ask_grid", step=0.05)
    _uniform(minimum_edge, "minimum_net_edge_grid", step=0.01)
    _uniform(minimum_persistence, "minimum_persistence_grid", step=0.02)
    if (
        minimum_ask != tuple(index / 100 for index in range(20, 81, 5))
        or maximum_ask != tuple(index / 100 for index in range(50, 96, 5))
        or minimum_edge != tuple(index / 100 for index in range(9))
        or minimum_persistence
        != tuple(index / 100 for index in range(10, 91, 2))
    ):
        raise PlateauConfigError("Stage 4d grids differ from the frozen domains")
    fractions = (
        float(payload["minimum_fold_fill_fraction"]),
        float(payload["minimum_pooled_fill_fraction"]),
    )
    if any(not 0 < value <= 1 for value in fractions):
        raise PlateauConfigError("fill fractions must be in (0, 1]")
    positive = (
        "selection_extra_cost_per_share",
        "stress_extra_cost_per_share",
        "minimum_fold_fills",
        "minimum_pooled_fills",
        "minimum_positive_folds",
        "minimum_positive_stress_folds",
        "minimum_pooled_profit_factor",
        "minimum_plateau_median_positive_folds",
        "minimum_plateau_median_positive_stress_folds",
    )
    if any(float(payload[name]) <= 0 for name in positive):
        raise PlateauConfigError("cost, exposure and fold gates must be positive")
    fold_gates = (
        int(payload["minimum_positive_folds"]),
        int(payload["minimum_positive_stress_folds"]),
        int(payload["minimum_plateau_median_positive_folds"]),
        int(payload["minimum_plateau_median_positive_stress_folds"]),
    )
    if any(value > len(folds) for value in fold_gates):
        raise PlateauConfigError("positive-fold gate exceeds fold count")
    if int(payload["minimum_support"]) != 1:
        raise PlateauConfigError("Stage 4d fixes minimum support at one")
    if float(payload["stress_extra_cost_per_share"]) <= float(
        payload["selection_extra_cost_per_share"]
    ):
        raise PlateauConfigError("stress cost must exceed primary cost")
    if payload["device"] not in {"cpu", "cuda"} or payload["dtype"] != "float64":
        raise PlateauConfigError("Stage 4d requires cpu|cuda and float64")
    assets = tuple(str(value).upper() for value in payload["assets"])
    if assets != ("BTC", "ETH", "SOL", "XRP"):
        raise PlateauConfigError("Stage 4d requires the four frozen assets")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    normalized: dict[str, Any] = dict(payload)
    normalized.update(
        {
            "assets": assets,
            "strategy_configs": tuple(
                str(value) for value in payload["strategy_configs"]
            ),
            "folds": folds,
            "holdout_start": holdout_start,
            "holdout_end_exclusive": holdout_end,
            "minimum_ask_grid": minimum_ask,
            "maximum_ask_grid": maximum_ask,
            "minimum_net_edge_grid": minimum_edge,
            "minimum_persistence_grid": minimum_persistence,
            "config_sha256": hashlib.sha256(canonical).hexdigest(),
        }
    )
    return PlateauConfig(**normalized)
