from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_.-]+$")


@dataclass(frozen=True)
class AccountClaim:
    id: str
    source_address: str
    expected_name: str
    claimed_pnl_usdc: float
    claimed_predictions: int
    claimed_biggest_win_usdc: float
    fetch_trade_crosscheck: bool


@dataclass(frozen=True)
class AuditConfig:
    audit_id: str
    period_start: datetime
    period_end: datetime
    window_days: int
    pnl_tolerance_usdc: float
    biggest_win_tolerance_usdc: float
    fetch_trades: bool
    fetch_closed_positions: bool
    accounts: tuple[AccountClaim, ...]

    @property
    def start_epoch(self) -> int:
        return int(self.period_start.timestamp())

    @property
    def end_epoch(self) -> int:
        return int(self.period_end.timestamp())


def _utc_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def load_config(path: str | Path) -> AuditConfig:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    accounts: list[AccountClaim] = []
    seen_ids: set[str] = set()
    for item in raw.get("accounts", []):
        account = AccountClaim(
            id=str(item["id"]),
            source_address=str(item["source_address"]).lower(),
            expected_name=str(item["expected_name"]),
            claimed_pnl_usdc=float(item["claimed_pnl_usdc"]),
            claimed_predictions=int(item["claimed_predictions"]),
            claimed_biggest_win_usdc=float(item["claimed_biggest_win_usdc"]),
            fetch_trade_crosscheck=bool(item.get("fetch_trade_crosscheck", True)),
        )
        if account.id in seen_ids:
            raise ValueError(f"duplicate account id: {account.id}")
        if not _ADDRESS.fullmatch(account.source_address):
            raise ValueError(f"invalid source address for {account.id}")
        if account.claimed_predictions < 0:
            raise ValueError(f"negative claimed_predictions for {account.id}")
        if not math.isfinite(account.claimed_pnl_usdc):
            raise ValueError(f"non-finite claimed_pnl_usdc for {account.id}")
        if not math.isfinite(account.claimed_biggest_win_usdc):
            raise ValueError(f"non-finite claimed_biggest_win_usdc for {account.id}")
        seen_ids.add(account.id)
        accounts.append(account)

    period_start = _utc_datetime(raw["period_start"], "period_start")
    period_end = _utc_datetime(raw["period_end"], "period_end")
    window_days = int(raw["window_days"])
    if not accounts:
        raise ValueError("accounts must not be empty")
    if period_end <= period_start:
        raise ValueError("period_end must be after period_start")
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if period_end.timestamp() - period_start.timestamp() + 1 < window_days * 86_400:
        raise ValueError("period must contain at least one complete audit window")
    audit_id = str(raw["audit_id"])
    if not _SAFE_ID.fullmatch(audit_id):
        raise ValueError("audit_id must be filesystem-safe")
    pnl_tolerance = float(raw["pnl_tolerance_usdc"])
    biggest_win_tolerance = float(raw["biggest_win_tolerance_usdc"])
    if not math.isfinite(pnl_tolerance) or pnl_tolerance < 0:
        raise ValueError("pnl_tolerance_usdc must be finite and non-negative")
    if not math.isfinite(biggest_win_tolerance) or biggest_win_tolerance < 0:
        raise ValueError("biggest_win_tolerance_usdc must be finite and non-negative")
    if bool(raw.get("fetch_market_metadata", False)):
        raise ValueError("market metadata belongs to the Stage 2 collector")

    return AuditConfig(
        audit_id=audit_id,
        period_start=period_start,
        period_end=period_end,
        window_days=window_days,
        pnl_tolerance_usdc=pnl_tolerance,
        biggest_win_tolerance_usdc=biggest_win_tolerance,
        fetch_trades=bool(raw.get("fetch_trades", True)),
        fetch_closed_positions=bool(raw.get("fetch_closed_positions", True)),
        accounts=tuple(accounts),
    )
