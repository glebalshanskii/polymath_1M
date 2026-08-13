from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


class StrategyConfigError(ValueError):
    """A production-shaped strategy config violates the Stage 3 policy."""


@dataclass(frozen=True)
class StrategyConfig:
    schema_version: int
    strategy_id: str
    assets: tuple[str, ...]
    duration: str
    minimum_ask: float
    maximum_ask: float
    minimum_net_edge: float
    minimum_persistence: float
    target_notional_usdc: float
    require_market_favorite: bool
    order_type: str
    one_entry_per_market: bool
    exit_policy: str
    config_sha256: str


def load_strategy_config(path: str | Path) -> StrategyConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = set(StrategyConfig.__dataclass_fields__) - {"config_sha256"}
    if payload.keys() != required:
        raise StrategyConfigError(
            f"strategy config fields differ: missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    if payload["schema_version"] != 1:
        raise StrategyConfigError("only strategy schema_version=1 is supported")
    if not isinstance(payload["assets"], list) or not payload["assets"]:
        raise StrategyConfigError("strategy assets must be a nonempty list")
    if str(payload["duration"]) not in {"5m", "15m", "1h"}:
        raise StrategyConfigError("duration must be 5m, 15m, or 1h")
    minimum_ask = float(payload["minimum_ask"])
    maximum_ask = float(payload["maximum_ask"])
    if not 0 < minimum_ask < maximum_ask <= 1:
        raise StrategyConfigError("ask range must satisfy 0 < min < max <= 1")
    if not 0 <= float(payload["minimum_persistence"]) <= 1:
        raise StrategyConfigError("minimum persistence must be in [0, 1]")
    if float(payload["minimum_net_edge"]) < 0:
        raise StrategyConfigError("minimum net edge must be nonnegative")
    if float(payload["target_notional_usdc"]) <= 0:
        raise StrategyConfigError("target notional must be positive")
    if payload["order_type"] != "FAK":
        raise StrategyConfigError("Stage 3 supports FAK only")
    if not isinstance(payload["require_market_favorite"], bool):
        raise StrategyConfigError("require_market_favorite must be boolean")
    if payload["one_entry_per_market"] is not True:
        raise StrategyConfigError("Stage 3 requires one entry per market")
    if payload["exit_policy"] != "hold_to_resolution":
        raise StrategyConfigError("Stage 3 supports hold_to_resolution only")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return StrategyConfig(
        schema_version=int(payload["schema_version"]),
        strategy_id=str(payload["strategy_id"]),
        assets=tuple(str(asset).upper() for asset in payload["assets"]),
        duration=str(payload["duration"]),
        minimum_ask=minimum_ask,
        maximum_ask=maximum_ask,
        minimum_net_edge=float(payload["minimum_net_edge"]),
        minimum_persistence=float(payload["minimum_persistence"]),
        target_notional_usdc=float(payload["target_notional_usdc"]),
        require_market_favorite=bool(payload["require_market_favorite"]),
        order_type=str(payload["order_type"]),
        one_entry_per_market=True,
        exit_policy=str(payload["exit_policy"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )
