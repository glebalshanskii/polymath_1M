from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class MarketRecord:
    series_id: str
    series_slug: str
    asset: str
    duration: str
    event_id: str
    market_id: str
    slug: str
    question: str
    condition_id: str
    token_ids: tuple[str, str]
    outcomes: tuple[str, str]
    start_timestamp_ms: int
    end_timestamp_ms: int
    description: str
    resolution_source: str
    reference_topic: str
    reference_symbol: str
    gamma_min_tick_size: str | None
    gamma_min_order_size: str | None
    gamma_fees_enabled: bool | None
    gamma_fee_schedule: dict[str, Any] | None
    clob_min_tick_size: str
    clob_min_order_size: str
    clob_maker_base_fee_bps: int
    clob_taker_base_fee_bps: int
    clob_fee_schedule: dict[str, Any]
    clob_order_delay_enabled: bool | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["token_ids"] = list(self.token_ids)
        payload["outcomes"] = list(self.outcomes)
        return payload


@dataclass(frozen=True)
class DiscoveryResult:
    markets: tuple[MarketRecord, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceBoundary:
    condition_id: str
    market_slug: str
    reference_topic: str
    reference_symbol: str
    boundary_timestamp_ms: int
    observed_timestamp_ms: int | None
    value: str | None
    exact: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
