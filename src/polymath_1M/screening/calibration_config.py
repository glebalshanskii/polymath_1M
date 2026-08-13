from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CalibrationConfigError(ValueError):
    """The frozen Stage 4b calibration contract is invalid."""


def _strictly_increasing(values: tuple[float, ...], name: str) -> None:
    if not values or any(right <= left for left, right in zip(values, values[1:])):
        raise CalibrationConfigError(f"{name} must be nonempty and increasing")


@dataclass(frozen=True)
class CalibrationConfig:
    schema_version: int
    experiment_id: str
    stage4_config: str
    strategy_configs: tuple[str, ...]
    minimum_ask_grid: tuple[float, ...]
    maximum_ask_grid: tuple[float, ...]
    minimum_net_edge_grid: tuple[float, ...]
    persistence_train_quantiles: tuple[float, ...]
    minimum_support_grid: tuple[int, ...]
    selection_extra_cost_per_share: float
    stress_extra_cost_per_share: float
    minimum_validation_fills: int
    minimum_fill_fraction: float
    maximum_fill_fraction: float
    minimum_profit_factor: float
    maximum_drawdown_usdc: float
    minimum_half_pnl_usdc: float
    minimum_stress_pnl_usdc: float
    neighbor_minimum_pnl_fraction: float
    test_minimum_fills: int
    test_minimum_fill_fraction: float
    test_minimum_profit_factor: float
    test_maximum_drawdown_usdc: float
    test_minimum_half_pnl_usdc: float
    test_minimum_stress_pnl_usdc: float
    test_maximum_single_day_profit_share: float
    seed: int
    device: str
    dtype: str
    config_sha256: str


def load_calibration_config(path: str | Path) -> CalibrationConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = set(CalibrationConfig.__dataclass_fields__) - {"config_sha256"}
    if payload.keys() != required:
        raise CalibrationConfigError(
            f"calibration config fields differ: missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    if payload["schema_version"] != 1 or len(payload["strategy_configs"]) != 5:
        raise CalibrationConfigError("Stage 4b requires schema 1 and five families")
    minimum_ask = tuple(float(value) for value in payload["minimum_ask_grid"])
    maximum_ask = tuple(float(value) for value in payload["maximum_ask_grid"])
    minimum_edge = tuple(
        float(value) for value in payload["minimum_net_edge_grid"]
    )
    quantiles = tuple(
        float(value) for value in payload["persistence_train_quantiles"]
    )
    support = tuple(int(value) for value in payload["minimum_support_grid"])
    for values, name in (
        (minimum_ask, "minimum_ask_grid"),
        (maximum_ask, "maximum_ask_grid"),
        (minimum_edge, "minimum_net_edge_grid"),
        (quantiles, "persistence_train_quantiles"),
        (tuple(float(value) for value in support), "minimum_support_grid"),
    ):
        _strictly_increasing(values, name)
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
        raise CalibrationConfigError("grid values are outside supported domains")
    fractions = (
        float(payload["minimum_fill_fraction"]),
        float(payload["maximum_fill_fraction"]),
        float(payload["neighbor_minimum_pnl_fraction"]),
        float(payload["test_minimum_fill_fraction"]),
        float(payload["test_maximum_single_day_profit_share"]),
    )
    if any(not 0 < value <= 1 for value in fractions):
        raise CalibrationConfigError("fractional gates must be in (0, 1]")
    if fractions[0] >= fractions[1]:
        raise CalibrationConfigError("minimum fill fraction must be below maximum")
    positive = (
        "selection_extra_cost_per_share",
        "stress_extra_cost_per_share",
        "minimum_validation_fills",
        "minimum_profit_factor",
        "maximum_drawdown_usdc",
        "test_minimum_fills",
        "test_minimum_profit_factor",
        "test_maximum_drawdown_usdc",
    )
    if any(float(payload[name]) <= 0 for name in positive):
        raise CalibrationConfigError("cost, exposure and risk gates must be positive")
    if float(payload["stress_extra_cost_per_share"]) <= float(
        payload["selection_extra_cost_per_share"]
    ):
        raise CalibrationConfigError("stress cost must exceed selection cost")
    if payload["device"] not in {"cpu", "cuda"} or payload["dtype"] != "float64":
        raise CalibrationConfigError("Stage 4b requires cpu|cuda and float64")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    normalized: dict[str, Any] = dict(payload)
    normalized.update(
        {
            "strategy_configs": tuple(
                str(value) for value in payload["strategy_configs"]
            ),
            "minimum_ask_grid": minimum_ask,
            "maximum_ask_grid": maximum_ask,
            "minimum_net_edge_grid": minimum_edge,
            "persistence_train_quantiles": quantiles,
            "minimum_support_grid": support,
            "config_sha256": hashlib.sha256(canonical).hexdigest(),
        }
    )
    return CalibrationConfig(**normalized)
