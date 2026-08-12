from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ScreeningConfigError(ValueError):
    """The frozen Stage 4 screening contract is invalid."""


def _datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ScreeningConfigError(f"{name} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ScreeningConfigError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class ScreeningConfig:
    schema_version: int
    experiment_id: str
    collector_config: str
    pmxt_config: str
    data_root: str
    period_start: datetime
    period_end_exclusive: datetime
    decision_seconds_before_end: int
    transition_horizon_seconds: int
    execution_latency_ms: int
    train_fraction: float
    validation_fraction: float
    price_bin_edges: tuple[float, ...]
    terminal_alpha: float
    transition_alpha: float
    minimum_support: int
    cost_scenarios_per_share: tuple[float, ...]
    selection_cost_per_share: float
    minimum_validation_fills: int
    minimum_test_fills: int
    initial_bankroll_usdc: float
    maximum_drawdown_fraction: float
    minimum_profit_factor: float
    maximum_week_profit_share: float
    minimum_test_calendar_days_for_concentration: int
    neighbor_edge_delta: float
    neighbor_persistence_delta: float
    seed: int
    device: str
    dtype: str
    pmxt_workers: int
    strategy_configs: tuple[str, ...]
    openmarket_revision: str
    openmarket_table: str
    config_sha256: str

    @property
    def period_start_ms(self) -> int:
        return int(self.period_start.timestamp() * 1_000)

    @property
    def period_end_ms(self) -> int:
        return int(self.period_end_exclusive.timestamp() * 1_000)


def load_screening_config(path: str | Path) -> ScreeningConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = set(ScreeningConfig.__dataclass_fields__) - {"config_sha256"}
    if payload.keys() != required:
        raise ScreeningConfigError(
            f"screening config fields differ: missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    if payload["schema_version"] != 1:
        raise ScreeningConfigError("only screening schema_version=1 is supported")
    start = _datetime(payload["period_start"], "period_start")
    end = _datetime(payload["period_end_exclusive"], "period_end_exclusive")
    if end <= start:
        raise ScreeningConfigError("screening period must be positive")
    train = float(payload["train_fraction"])
    validation = float(payload["validation_fraction"])
    if not 0 < train < 1 or not 0 < validation < 1 or train + validation >= 1:
        raise ScreeningConfigError("split fractions must leave nonempty test")
    edges = tuple(float(value) for value in payload["price_bin_edges"])
    if (
        len(edges) < 3
        or edges[0] != 0
        or edges[-1] <= 1
        or any(right <= left for left, right in zip(edges[:-1], edges[1:], strict=True))
    ):
        raise ScreeningConfigError("price bins must strictly cover [0, 1]")
    costs = tuple(float(value) for value in payload["cost_scenarios_per_share"])
    if not costs or any(value < 0 for value in costs):
        raise ScreeningConfigError("cost scenarios must be nonempty and nonnegative")
    selection_cost = float(payload["selection_cost_per_share"])
    if selection_cost not in costs:
        raise ScreeningConfigError("selection cost must be one of cost scenarios")
    if len(payload["strategy_configs"]) != 5:
        raise ScreeningConfigError("exactly five Stage 4 candidates are required")
    positive = (
        "decision_seconds_before_end",
        "transition_horizon_seconds",
        "execution_latency_ms",
        "terminal_alpha",
        "transition_alpha",
        "minimum_validation_fills",
        "minimum_test_fills",
        "initial_bankroll_usdc",
        "maximum_drawdown_fraction",
        "minimum_profit_factor",
        "maximum_week_profit_share",
        "minimum_test_calendar_days_for_concentration",
        "neighbor_edge_delta",
        "neighbor_persistence_delta",
        "pmxt_workers",
    )
    if any(float(payload[name]) <= 0 for name in positive):
        raise ScreeningConfigError(f"screening values must be positive: {positive}")
    if int(payload["minimum_support"]) < 0:
        raise ScreeningConfigError("minimum support must be nonnegative")
    if payload["device"] not in {"cpu", "cuda"} or payload["dtype"] != "float64":
        raise ScreeningConfigError("device/dtype must be cpu|cuda and float64")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return ScreeningConfig(
        schema_version=1,
        experiment_id=str(payload["experiment_id"]),
        collector_config=str(payload["collector_config"]),
        pmxt_config=str(payload["pmxt_config"]),
        data_root=str(payload["data_root"]),
        period_start=start,
        period_end_exclusive=end,
        decision_seconds_before_end=int(payload["decision_seconds_before_end"]),
        transition_horizon_seconds=int(payload["transition_horizon_seconds"]),
        execution_latency_ms=int(payload["execution_latency_ms"]),
        train_fraction=train,
        validation_fraction=validation,
        price_bin_edges=edges,
        terminal_alpha=float(payload["terminal_alpha"]),
        transition_alpha=float(payload["transition_alpha"]),
        minimum_support=int(payload["minimum_support"]),
        cost_scenarios_per_share=costs,
        selection_cost_per_share=selection_cost,
        minimum_validation_fills=int(payload["minimum_validation_fills"]),
        minimum_test_fills=int(payload["minimum_test_fills"]),
        initial_bankroll_usdc=float(payload["initial_bankroll_usdc"]),
        maximum_drawdown_fraction=float(payload["maximum_drawdown_fraction"]),
        minimum_profit_factor=float(payload["minimum_profit_factor"]),
        maximum_week_profit_share=float(payload["maximum_week_profit_share"]),
        minimum_test_calendar_days_for_concentration=int(
            payload["minimum_test_calendar_days_for_concentration"]
        ),
        neighbor_edge_delta=float(payload["neighbor_edge_delta"]),
        neighbor_persistence_delta=float(payload["neighbor_persistence_delta"]),
        seed=int(payload["seed"]),
        device=str(payload["device"]),
        dtype=str(payload["dtype"]),
        pmxt_workers=int(payload["pmxt_workers"]),
        strategy_configs=tuple(str(value) for value in payload["strategy_configs"]),
        openmarket_revision=str(payload["openmarket_revision"]),
        openmarket_table=str(payload["openmarket_table"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )
