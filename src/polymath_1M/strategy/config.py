from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BacktestConfigError(ValueError):
    """The executable backtest contract is invalid."""


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class BacktestConfig:
    schema_version: int
    experiment_id: str
    strategy_config: str
    dataset_config: str
    dataset_root: str
    assets: tuple[str, ...]
    max_markets: int
    seed: int
    device: str
    dtype: str
    label_policy: str
    decision_seconds_before_end: int
    transition_horizon_seconds: int
    train_fraction: float
    validation_fraction: float
    price_bin_edges: tuple[float, ...]
    terminal_alpha: float
    transition_alpha: float
    minimum_support: int
    platform_fee_rate: float
    platform_fee_round_decimals: int
    fee_source: str
    extra_cost_per_share: float
    config_sha256: str


def load_backtest_config(path: str | Path) -> BacktestConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = set(BacktestConfig.__dataclass_fields__) - {"config_sha256"}
    missing = required - payload.keys()
    extra = payload.keys() - required
    if missing or extra:
        raise BacktestConfigError(
            f"backtest config fields differ: missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )
    edges = tuple(float(value) for value in payload["price_bin_edges"])
    if int(payload["schema_version"]) != 1:
        raise BacktestConfigError("only backtest schema_version=1 is supported")
    if len(edges) < 3 or edges[0] != 0.0 or edges[-1] <= 1.0:
        raise BacktestConfigError("price bins must cover [0, 1] with >=2 bins")
    if any(right <= left for left, right in zip(edges[:-1], edges[1:], strict=True)):
        raise BacktestConfigError("price bin edges must be strictly increasing")
    train_fraction = float(payload["train_fraction"])
    validation_fraction = float(payload["validation_fraction"])
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1:
        raise BacktestConfigError("train/validation fractions must be in (0, 1)")
    if train_fraction + validation_fraction >= 1:
        raise BacktestConfigError(
            "train + validation must leave a nonempty test fraction"
        )
    if int(payload["max_markets"]) < 5:
        raise BacktestConfigError("max_markets must be at least five")
    decision_seconds = int(payload["decision_seconds_before_end"])
    transition_seconds = int(payload["transition_horizon_seconds"])
    if decision_seconds <= 0 or transition_seconds <= 0:
        raise BacktestConfigError("decision and transition horizons must be positive")
    if decision_seconds + transition_seconds >= 300:
        raise BacktestConfigError(
            "5m decision and previous snapshots must be inside market"
        )
    if str(payload["dtype"]) != "float64":
        raise BacktestConfigError("Stage 3 backtest requires dtype=float64")
    if str(payload["label_policy"]) != "kacho_inferred_development_only":
        raise BacktestConfigError(
            "this config may only run the explicit Kacho dev label"
        )
    if float(payload["terminal_alpha"]) <= 0 or float(payload["transition_alpha"]) <= 0:
        raise BacktestConfigError("smoothing alphas must be positive")
    if int(payload["minimum_support"]) < 0:
        raise BacktestConfigError("minimum support must be nonnegative")
    if (
        float(payload["platform_fee_rate"]) < 0
        or float(payload["extra_cost_per_share"]) < 0
    ):
        raise BacktestConfigError("execution costs must be nonnegative")
    fee_decimals = int(payload["platform_fee_round_decimals"])
    if fee_decimals != payload["platform_fee_round_decimals"] or fee_decimals < 0:
        raise BacktestConfigError("fee round decimals must be a nonnegative integer")
    if not str(payload["fee_source"]).strip():
        raise BacktestConfigError("fee source must be explicit")
    return BacktestConfig(
        schema_version=int(payload["schema_version"]),
        experiment_id=str(payload["experiment_id"]),
        strategy_config=str(payload["strategy_config"]),
        dataset_config=str(payload["dataset_config"]),
        dataset_root=str(payload["dataset_root"]),
        assets=tuple(str(asset).upper() for asset in payload["assets"]),
        max_markets=int(payload["max_markets"]),
        seed=int(payload["seed"]),
        device=str(payload["device"]),
        dtype=str(payload["dtype"]),
        label_policy=str(payload["label_policy"]),
        decision_seconds_before_end=decision_seconds,
        transition_horizon_seconds=transition_seconds,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
        price_bin_edges=edges,
        terminal_alpha=float(payload["terminal_alpha"]),
        transition_alpha=float(payload["transition_alpha"]),
        minimum_support=int(payload["minimum_support"]),
        platform_fee_rate=float(payload["platform_fee_rate"]),
        platform_fee_round_decimals=fee_decimals,
        fee_source=str(payload["fee_source"]),
        extra_cost_per_share=float(payload["extra_cost_per_share"]),
        config_sha256=_canonical_hash(payload),
    )
