from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import torch

from .config import AccountClaim, AuditConfig

_ASSETS = {
    "BTC": ("bitcoin", "btc"),
    "ETH": ("ethereum", "eth"),
    "SOL": ("solana", "sol"),
    "XRP": ("xrp",),
    "BNB": ("bnb", "binance coin"),
}


def analyze_account_db(
    config: AuditConfig,
    claim: AccountClaim,
    profile: dict[str, Any],
    connection: sqlite3.Connection,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    windows = _window_metrics_db(config, claim, connection)
    matched = [row for row in windows if row["joint_match"]]
    best = (
        min(windows, key=lambda row: row["normalized_claim_distance"])
        if windows
        else None
    )
    activity_count = _scalar_count(connection, "activity", claim.id)
    activity_trade_count = _scalar_count(
        connection, "activity", claim.id, "activity_type = 'TRADE'"
    )
    trade_count = _scalar_count(connection, "trades", claim.id)
    closed_count = _scalar_count(connection, "closed_positions", claim.id)
    trade_api_complete = _collection_complete(connection, claim.id, "trades")
    closed_positions_complete = _collection_complete(
        connection, claim.id, "closed_positions"
    )
    proxy_wallet = str(profile.get("proxyWallet") or "").lower()
    identity_status = (
        "same_proxy" if proxy_wallet == claim.source_address else "mapped_proxy"
    )
    summary = {
        "account_id": claim.id,
        "source_label": claim.expected_name,
        "source_address": claim.source_address,
        "profile_proxy_wallet": profile.get("proxyWallet"),
        "profile_name": profile.get("name"),
        "profile_created_at": profile.get("createdAt"),
        "identity_status": identity_status,
        "activity_rows": activity_count,
        "activity_trade_rows": activity_trade_count,
        "trade_api_rows": trade_count,
        "trade_api_complete": trade_api_complete,
        "closed_position_rows": closed_count,
        "closed_positions_complete": closed_positions_complete,
        "crosscheck_trade_count_difference": (
            trade_count - activity_trade_count if trade_api_complete else None
        ),
        "claim_status": "matched_public_ledger" if matched else "not_reconstructable",
        "matching_windows": len(matched),
        "best_candidate": best,
        "limitations": [
            "Market PnL is reconstructed from TRADE/SPLIT/REDEEM/MERGE cash flows; fee fields are unavailable.",
            "Net activity cash flow is diagnostic and is not treated as PnL without boundary inventory.",
            "Closed-positions pagination is not usable for these high-volume accounts (HTTP 500 at deep offsets).",
            "Endpoint row counts are comparable only when the corresponding complete flag is true.",
            "A deterministic different result requires fee and on-chain reconciliation.",
        ],
    }
    return summary, windows, _behavior_summary_db(connection, claim.id)


def _window_metrics_db(
    config: AuditConfig,
    claim: AccountClaim,
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    window_seconds = config.window_days * 86_400
    last_start = config.end_epoch - window_seconds + 1
    starts = torch.arange(config.start_epoch, last_start + 1, 86_400, dtype=torch.int64)
    ends = starts + window_seconds - 1

    activity_ts, activity_values = _load_numeric_rows(
        connection,
        "SELECT timestamp, cash_flow, reward_cash FROM activity WHERE account_id = ? ORDER BY timestamp",
        (claim.id,),
        value_columns=2,
    )
    trade_ts, _ = _load_numeric_rows(
        connection,
        "SELECT timestamp FROM activity WHERE account_id = ? AND activity_type = 'TRADE' ORDER BY timestamp",
        (claim.id,),
        value_columns=0,
    )
    market_rows = connection.execute(
        """SELECT condition_id, MAX(timestamp) AS close_timestamp, SUM(cash_flow) AS pnl
           FROM activity
           WHERE account_id = ?
             AND activity_type IN ('TRADE', 'SPLIT', 'REDEEM', 'MERGE')
             AND condition_id IS NOT NULL
           GROUP BY condition_id
           HAVING MAX(CASE WHEN activity_type IN ('REDEEM', 'MERGE') THEN 1 ELSE 0 END) = 1
           ORDER BY close_timestamp""",
        (claim.id,),
    ).fetchall()
    market_ts = torch.tensor(
        [_integer(row[1]) for row in market_rows], dtype=torch.int64
    )
    market_pnl = torch.tensor(
        [_number(row[2]) for row in market_rows], dtype=torch.float64
    )
    trade_activity_counts = _range_sums(
        trade_ts, torch.ones(trade_ts.shape, dtype=torch.float64), starts, ends
    ).to(torch.int64)
    cashflow = _range_sums(activity_ts, activity_values[:, 0], starts, ends)
    rewards = _range_sums(activity_ts, activity_values[:, 1], starts, ends)
    realized_pnl = _range_sums(market_ts, market_pnl, starts, ends)
    unique_market_counts = _entity_window_counts(
        connection,
        claim.id,
        "condition_id",
        starts,
        ends,
    )
    unique_position_counts = _entity_window_counts(
        connection,
        claim.id,
        "condition_id, asset, outcome",
        starts,
        ends,
    )

    output: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(
        zip(starts.tolist(), ends.tolist(), strict=True)
    ):
        closed_mask = (market_ts >= start) & (market_ts <= end)
        resolved_markets = int(torch.count_nonzero(closed_mask).item())
        biggest_win = (
            float(torch.max(market_pnl[closed_mask]).item())
            if bool(torch.any(closed_mask))
            else 0.0
        )
        counts = {
            "trade_activity_rows": int(trade_activity_counts[index].item()),
            "unique_markets": int(unique_market_counts[index].item()),
            "unique_positions": int(unique_position_counts[index].item()),
            "resolved_markets": int(resolved_markets),
        }
        output.append(
            _claim_window_row(
                config=config,
                claim=claim,
                start=start,
                end=end,
                pnl=float(realized_pnl[index].item()),
                cashflow=float(cashflow[index].item()),
                rewards=float(rewards[index].item()),
                biggest_win=biggest_win,
                counts=counts,
            )
        )
    return output


def _entity_window_counts(
    connection: sqlite3.Connection,
    account_id: str,
    group_columns: str,
    starts: torch.Tensor,
    ends: torch.Tensor,
) -> torch.Tensor:
    if group_columns not in {"condition_id", "condition_id, asset, outcome"}:
        raise ValueError(f"unsupported group columns: {group_columns}")
    rows = connection.execute(
        f"""SELECT MIN(timestamp), MAX(timestamp)
            FROM activity
            WHERE account_id = ? AND activity_type = 'TRADE'
              AND condition_id IS NOT NULL
            GROUP BY {group_columns}""",
        (account_id,),
    ).fetchall()
    if not rows:
        return torch.zeros(starts.shape, dtype=torch.int64)
    ranges = torch.tensor(rows, dtype=torch.int64)
    active = (ranges[:, 0, None] <= ends[None, :]) & (
        ranges[:, 1, None] >= starts[None, :]
    )
    return torch.sum(active, dim=0, dtype=torch.int64)


def _behavior_summary_db(
    connection: sqlite3.Connection, account_id: str
) -> dict[str, Any]:
    side_counts = {
        str(side or "UNKNOWN"): int(count)
        for side, count in connection.execute(
            """SELECT side, COUNT(*) FROM activity
               WHERE account_id = ? AND activity_type = 'TRADE' GROUP BY side""",
            (account_id,),
        )
    }
    asset_counts: Counter[str] = Counter()
    duration_counts: Counter[str] = Counter()
    for title, slug, count in connection.execute(
        """SELECT title, slug, COUNT(*) FROM activity
           WHERE account_id = ? AND activity_type = 'TRADE'
           GROUP BY title, slug""",
        (account_id,),
    ):
        row = {"title": title, "slug": slug}
        asset_counts[_classify_asset(row)] += int(count)
        duration_counts[_classify_duration(row)] += int(count)

    numeric_parts: list[torch.Tensor] = []
    cursor = connection.execute(
        """SELECT price, usdc_size FROM activity
           WHERE account_id = ? AND activity_type = 'TRADE'""",
        (account_id,),
    )
    while rows := cursor.fetchmany(50_000):
        numeric_parts.append(
            torch.tensor(
                [[_number(price), _number(size)] for price, size in rows],
                dtype=torch.float64,
            )
        )
    numeric = (
        torch.cat(numeric_parts)
        if numeric_parts
        else torch.empty((0, 2), dtype=torch.float64)
    )
    if numeric.numel():
        quantiles = torch.quantile(
            numeric, torch.tensor([0.1, 0.5, 0.9], dtype=torch.float64), dim=0
        )
    else:
        quantiles = torch.zeros((3, 2), dtype=torch.float64)
    trade_rows, unique_markets = connection.execute(
        """SELECT COUNT(*), COUNT(DISTINCT condition_id) FROM activity
           WHERE account_id = ? AND activity_type = 'TRADE'""",
        (account_id,),
    ).fetchone()
    return {
        "trade_rows": int(trade_rows),
        "unique_markets": int(unique_markets),
        "side_counts": dict(sorted(side_counts.items())),
        "asset_counts": dict(sorted(asset_counts.items())),
        "duration_counts": dict(sorted(duration_counts.items())),
        "price_quantiles_p10_p50_p90": [
            round(value, 6) for value in quantiles[:, 0].tolist()
        ],
        "usdc_size_quantiles_p10_p50_p90": [
            round(value, 6) for value in quantiles[:, 1].tolist()
        ],
    }


def _load_numeric_rows(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...],
    value_columns: int,
) -> tuple[torch.Tensor, torch.Tensor]:
    timestamp_parts: list[torch.Tensor] = []
    value_parts: list[torch.Tensor] = []
    cursor = connection.execute(query, params)
    while rows := cursor.fetchmany(100_000):
        timestamp_parts.append(
            torch.tensor([_integer(row[0]) for row in rows], dtype=torch.int64)
        )
        if value_columns:
            value_parts.append(
                torch.tensor(
                    [
                        [_number(value) for value in row[1 : 1 + value_columns]]
                        for row in rows
                    ],
                    dtype=torch.float64,
                )
            )
    timestamps = (
        torch.cat(timestamp_parts)
        if timestamp_parts
        else torch.empty(0, dtype=torch.int64)
    )
    values = (
        torch.cat(value_parts)
        if value_parts
        else torch.empty((timestamps.numel(), value_columns), dtype=torch.float64)
    )
    return timestamps, values


def _scalar_count(
    connection: sqlite3.Connection,
    table: str,
    account_id: str,
    extra_where: str | None = None,
) -> int:
    if table not in {"activity", "trades", "closed_positions"}:
        raise ValueError(f"unsupported table: {table}")
    where = "account_id = ?" + (f" AND {extra_where}" if extra_where else "")
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", (account_id,)
        ).fetchone()[0]
    )


def _collection_complete(
    connection: sqlite3.Connection, account_id: str, endpoint: str
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM collection_state WHERE account_id = ? AND endpoint = ?",
            (account_id, endpoint),
        ).fetchone()
        is not None
    )


def _claim_window_row(
    *,
    config: AuditConfig,
    claim: AccountClaim,
    start: int,
    end: int,
    pnl: float,
    cashflow: float,
    rewards: float,
    biggest_win: float,
    counts: dict[str, int],
) -> dict[str, Any]:
    matching_count_names = [
        name for name, value in counts.items() if value == claim.claimed_predictions
    ]
    pnl_candidates = {
        "settled_market_cashflow": pnl,
        "settled_market_cashflow_plus_rewards": pnl + rewards,
    }
    matching_pnl_names = [
        name
        for name, value in pnl_candidates.items()
        if abs(value - claim.claimed_pnl_usdc) <= config.pnl_tolerance_usdc
    ]
    pnl_match = bool(matching_pnl_names)
    nearest_pnl_name, nearest_pnl = min(
        pnl_candidates.items(),
        key=lambda item: abs(item[1] - claim.claimed_pnl_usdc),
    )
    biggest_match = (
        abs(biggest_win - claim.claimed_biggest_win_usdc)
        <= config.biggest_win_tolerance_usdc
    )
    count_distance = min(
        abs(value - claim.claimed_predictions) for value in counts.values()
    )
    distance = (
        abs(nearest_pnl - claim.claimed_pnl_usdc)
        / max(abs(claim.claimed_pnl_usdc), 1.0)
        + count_distance / max(claim.claimed_predictions, 1)
        + abs(biggest_win - claim.claimed_biggest_win_usdc)
        / max(abs(claim.claimed_biggest_win_usdc), 1.0)
    )
    return {
        "account_id": claim.id,
        "window_start": datetime.fromtimestamp(start, UTC).date().isoformat(),
        "window_end": datetime.fromtimestamp(end, UTC).date().isoformat(),
        "settled_market_cashflow_pnl_usdc": round(pnl, 6),
        "settled_market_cashflow_plus_rewards_usdc": round(pnl + rewards, 6),
        "net_activity_cashflow_usdc_diagnostic": round(cashflow, 6),
        "rewards_rebates_usdc": round(rewards, 6),
        "biggest_resolved_market_cashflow_win_usdc": round(biggest_win, 6),
        **counts,
        "pnl_match": pnl_match,
        "matching_pnl_definitions": ",".join(matching_pnl_names),
        "nearest_public_pnl_definition": nearest_pnl_name,
        "nearest_public_pnl_usdc": round(nearest_pnl, 6),
        "diagnostic_cashflow_match": (
            abs(cashflow - claim.claimed_pnl_usdc) <= config.pnl_tolerance_usdc
        ),
        "biggest_win_match": biggest_match,
        "matching_prediction_definitions": ",".join(matching_count_names),
        "joint_match": pnl_match and biggest_match and bool(matching_count_names),
        "normalized_claim_distance": round(distance, 9),
    }


def _range_sums(
    timestamps: torch.Tensor,
    values: torch.Tensor,
    starts: torch.Tensor,
    ends: torch.Tensor,
) -> torch.Tensor:
    if timestamps.numel() == 0:
        return torch.zeros(starts.shape, dtype=torch.float64)
    order = torch.argsort(timestamps)
    sorted_ts = timestamps[order]
    sorted_values = values[order]
    prefix = torch.cat(
        (torch.zeros(1, dtype=torch.float64), torch.cumsum(sorted_values, dim=0))
    )
    lower = torch.searchsorted(sorted_ts, starts, right=False)
    upper = torch.searchsorted(sorted_ts, ends, right=True)
    return prefix[upper] - prefix[lower]


def _classify_asset(row: dict[str, Any]) -> str:
    text = f"{row.get('title', '')} {row.get('slug', '')}".lower()
    for asset, aliases in _ASSETS.items():
        if any(
            re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text)
            for alias in aliases
        ):
            return asset
    return "OTHER"


def _classify_duration(row: dict[str, Any]) -> str:
    text = f"{row.get('title', '')} {row.get('slug', '')}".lower()
    for duration, patterns in {
        "5m": (r"(?<!\d)5\s*(?:m|min|minute)", r"-5m-"),
        "15m": (r"(?<!\d)15\s*(?:m|min|minute)", r"-15m-"),
        "1h": (r"(?<!\d)1\s*(?:h|hour)", r"hourly"),
        "4h": (r"(?<!\d)4\s*(?:h|hour)",),
    }.items():
        if any(re.search(pattern, text) for pattern in patterns):
            return duration
    return "UNKNOWN"


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except TypeError, ValueError:
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return 0
