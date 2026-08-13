from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import plotly
import plotly.graph_objects as go
import torch
from plotly.subplots import make_subplots

from polymath_1M.domain import DecisionBatch
from polymath_1M.historical.config import load_kacho_dataset_config
from polymath_1M.historical.kacho import load_kacho_decision_batch
from polymath_1M.historical.trent import load_trent_decision_batch
from polymath_1M.strategy.model import calculate_platform_fee

from .regime_models import Fold, _git, load_regime_config
from .regime_robustness import load_early_robustness_config


class LiteralMarkovError(RuntimeError):
    """The frozen literal-Markov contract or its inputs are invalid."""


@dataclass(frozen=True)
class SourceVariant:
    variant_id: str
    source: str
    minimum_ask: float
    maximum_ask: float
    minimum_gap: float
    gap_operator: str
    minimum_destination_persistence: float


@dataclass(frozen=True)
class LiteralMarkovConfig:
    experiment_id: str
    primary_source_config: str
    early_source_config: str
    asset: str
    duration: str
    decision_seconds_before_end: int
    transition_horizon_seconds: int
    execution_latency_seconds: int
    state_bin_edges: tuple[float, ...]
    transition_pseudocount: float
    minimum_row_transitions: int
    side_state_policy: str
    side_selection_policy: str
    variants: tuple[SourceVariant, ...]
    target_notional_usdc: float
    platform_fee_round_decimals: int
    primary_extra_cost_per_share: float
    stress_extra_cost_per_share: float
    minimum_candidate_fills: int
    minimum_positive_folds: int
    minimum_profit_factor: float
    seed: int
    device: str
    dtype: str
    config_sha256: str


@dataclass(frozen=True)
class TransitionModel:
    edges: torch.Tensor
    counts: torch.Tensor
    row_support: torch.Tensor
    matrix: torch.Tensor
    destination: torch.Tensor
    maximum_probability: torch.Tensor
    destination_persistence: torch.Tensor


@dataclass(frozen=True)
class LiteralEvaluation:
    current_state: torch.Tensor
    destination_state: torch.Tensor
    row_support: torch.Tensor
    transition_probability: torch.Tensor
    destination_persistence: torch.Tensor
    signal_ask: torch.Tensor
    gap: torch.Tensor
    side: torch.Tensor
    signal: torch.Tensor
    fill_vwap: torch.Tensor
    fill_cost: torch.Tensor
    platform_fee: torch.Tensor
    fill_shares: torch.Tensor
    primary_extra_cost: torch.Tensor
    stress_extra_cost: torch.Tensor
    primary_pnl: torch.Tensor
    stress_pnl: torch.Tensor
    filled: torch.Tensor
    status_code: torch.Tensor
    funnel: dict[str, int | float | None]


@dataclass(frozen=True)
class SourceData:
    source_id: str
    batch: DecisionBatch
    folds: tuple[Fold, ...]
    fee_rate: float | torch.Tensor
    fee_exponent: float
    provenance: dict[str, Any]


STATUS_NAMES = (
    "filled",
    "data_invalid",
    "no_supported_side",
    "ask_out_of_range",
    "gap_below_threshold",
    "low_destination_persistence",
    "no_liquidity",
)


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise LiteralMarkovError(f"cannot write empty CSV: {path}")
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def load_literal_markov_config(path: str | Path) -> LiteralMarkovConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "experiment_id",
        "primary_source_config",
        "early_source_config",
        "asset",
        "duration",
        "decision_seconds_before_end",
        "transition_horizon_seconds",
        "execution_latency_seconds",
        "state_bin_edges",
        "transition_pseudocount",
        "minimum_row_transitions",
        "side_state_policy",
        "side_selection_policy",
        "variants",
        "target_notional_usdc",
        "platform_fee_round_decimals",
        "primary_extra_cost_per_share",
        "stress_extra_cost_per_share",
        "minimum_candidate_fills",
        "minimum_positive_folds",
        "minimum_profit_factor",
        "seed",
        "device",
        "dtype",
    }
    if payload.keys() != expected:
        raise LiteralMarkovError(
            f"Stage 4f config differs: missing={sorted(expected - payload.keys())}, "
            f"extra={sorted(payload.keys() - expected)}"
        )
    variant_fields = {
        "variant_id",
        "source",
        "minimum_ask",
        "maximum_ask",
        "minimum_gap",
        "gap_operator",
        "minimum_destination_persistence",
    }
    variants: list[SourceVariant] = []
    for raw in payload["variants"]:
        if raw.keys() != variant_fields:
            raise LiteralMarkovError("Stage 4f variant fields differ")
        variants.append(
            SourceVariant(
                variant_id=str(raw["variant_id"]),
                source=str(raw["source"]),
                minimum_ask=float(raw["minimum_ask"]),
                maximum_ask=float(raw["maximum_ask"]),
                minimum_gap=float(raw["minimum_gap"]),
                gap_operator=str(raw["gap_operator"]),
                minimum_destination_persistence=float(
                    raw["minimum_destination_persistence"]
                ),
            )
        )
    edges = tuple(float(value) for value in payload["state_bin_edges"])
    if (
        payload["schema_version"] != 1
        or payload["asset"] != "BTC"
        or payload["duration"] != "5m"
        or int(payload["decision_seconds_before_end"]) != 60
        or int(payload["transition_horizon_seconds"]) != 60
        or int(payload["execution_latency_seconds"]) != 1
        or edges != (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.000001)
        or float(payload["transition_pseudocount"]) != 0
        or int(payload["minimum_row_transitions"]) != 1
        or payload["side_state_policy"] != "up_mid_and_one_minus_up_mid_shared_matrix"
        or payload["side_selection_policy"]
        != "evaluate_both_sides_choose_larger_gap_then_up"
        or tuple(item.variant_id for item in variants) != ("core_tau87", "b27_tau75")
        or variants[0].gap_operator != "greater_equal"
        or variants[0].minimum_ask != 0.64
        or variants[0].maximum_ask != 0.99
        or variants[0].minimum_gap != 0.05
        or variants[0].minimum_destination_persistence != 0.87
        or variants[1].gap_operator != "greater"
        or variants[1].minimum_ask != 0.01
        or variants[1].maximum_ask != 0.96
        or variants[1].minimum_gap != 0.05
        or variants[1].minimum_destination_persistence != 0.75
        or float(payload["target_notional_usdc"]) != 10
        or int(payload["platform_fee_round_decimals"]) != 4
        or float(payload["primary_extra_cost_per_share"]) != 0.01
        or float(payload["stress_extra_cost_per_share"]) != 0.02
        or int(payload["minimum_candidate_fills"]) != 100
        or int(payload["minimum_positive_folds"]) != 6
        or float(payload["minimum_profit_factor"]) != 1.10
        or int(payload["seed"]) != 20260813
        or payload["device"] != "cuda"
        or payload["dtype"] != "float64"
    ):
        raise LiteralMarkovError("Stage 4f frozen source contract differs")
    if any(
        item.minimum_ask < 0
        or item.maximum_ask > 1
        or item.minimum_ask >= item.maximum_ask
        or item.minimum_gap < 0
        or not 0 <= item.minimum_destination_persistence <= 1
        for item in variants
    ):
        raise LiteralMarkovError("Stage 4f threshold outside its domain")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return LiteralMarkovConfig(
        experiment_id=str(payload["experiment_id"]),
        primary_source_config=str(payload["primary_source_config"]),
        early_source_config=str(payload["early_source_config"]),
        asset="BTC",
        duration="5m",
        decision_seconds_before_end=60,
        transition_horizon_seconds=60,
        execution_latency_seconds=1,
        state_bin_edges=edges,
        transition_pseudocount=0.0,
        minimum_row_transitions=1,
        side_state_policy=str(payload["side_state_policy"]),
        side_selection_policy=str(payload["side_selection_policy"]),
        variants=tuple(variants),
        target_notional_usdc=float(payload["target_notional_usdc"]),
        platform_fee_round_decimals=int(payload["platform_fee_round_decimals"]),
        primary_extra_cost_per_share=float(payload["primary_extra_cost_per_share"]),
        stress_extra_cost_per_share=float(payload["stress_extra_cost_per_share"]),
        minimum_candidate_fills=int(payload["minimum_candidate_fills"]),
        minimum_positive_folds=int(payload["minimum_positive_folds"]),
        minimum_profit_factor=float(payload["minimum_profit_factor"]),
        seed=int(payload["seed"]),
        device=str(payload["device"]),
        dtype=str(payload["dtype"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _side_midpoints(batch: DecisionBatch) -> tuple[torch.Tensor, torch.Tensor]:
    previous = torch.stack((batch.previous_mid_up, 1 - batch.previous_mid_up), dim=1)
    current = torch.stack((batch.current_mid_up, 1 - batch.current_mid_up), dim=1)
    return previous, current


def fit_transition_model(
    train: DecisionBatch,
    edges: torch.Tensor,
    *,
    pseudocount: float,
) -> TransitionModel:
    if train.current_mid_up.dtype != torch.float64 or edges.dtype != torch.float64:
        raise TypeError("literal Markov model requires float64 tensors")
    if edges.ndim != 1 or edges.numel() < 3 or pseudocount < 0:
        raise ValueError("literal Markov edges/pseudocount are invalid")
    previous, current = _side_midpoints(train)
    valid = (
        train.snapshot_valid.unsqueeze(1)
        & torch.isfinite(previous)
        & torch.isfinite(current)
        & (previous >= 0)
        & (previous <= 1)
        & (current >= 0)
        & (current <= 1)
    )
    bins = edges.numel() - 1
    previous_states = torch.bucketize(previous, edges[1:-1], right=False)
    current_states = torch.bucketize(current, edges[1:-1], right=False)
    flat = previous_states[valid] * bins + current_states[valid]
    counts = torch.bincount(flat, minlength=bins * bins).reshape(bins, bins)
    row_support = counts.sum(dim=1)
    smoothed = counts.to(torch.float64) + pseudocount
    denominator = row_support.to(torch.float64) + pseudocount * bins
    matrix = torch.where(
        denominator[:, None] > 0,
        smoothed
        / torch.clamp(denominator[:, None], min=torch.finfo(torch.float64).tiny),
        torch.zeros_like(smoothed),
    )
    destination = torch.argmax(matrix, dim=1)
    maximum_probability = matrix.gather(1, destination[:, None]).squeeze(1)
    destination_persistence = matrix[destination, destination]
    return TransitionModel(
        edges=edges,
        counts=counts,
        row_support=row_support,
        matrix=matrix,
        destination=destination,
        maximum_probability=maximum_probability,
        destination_persistence=destination_persistence,
    )


def evaluate_literal_markov(
    batch: DecisionBatch,
    model: TransitionModel,
    variant: SourceVariant,
    *,
    minimum_row_transitions: int,
    target_notional_usdc: float,
    fee_rate: float | torch.Tensor,
    fee_exponent: float,
    fee_round_decimals: int,
    primary_extra_cost_per_share: float,
    stress_extra_cost_per_share: float,
) -> LiteralEvaluation:
    _, current = _side_midpoints(batch)
    states = torch.bucketize(current, model.edges[1:-1], right=False)
    destination = model.destination[states]
    support = model.row_support[states]
    probability = model.maximum_probability[states]
    persistence = model.destination_persistence[states]
    asks = batch.asks
    gap = probability - asks
    supported = support >= minimum_row_transitions
    in_range = (asks >= variant.minimum_ask) & (asks <= variant.maximum_ask)
    if variant.gap_operator == "greater_equal":
        gap_pass = gap >= variant.minimum_gap
    elif variant.gap_operator == "greater":
        gap_pass = gap > variant.minimum_gap
    else:
        raise LiteralMarkovError(f"unknown gap operator: {variant.gap_operator}")
    persistence_pass = persistence >= variant.minimum_destination_persistence
    valid = batch.snapshot_valid.unsqueeze(1)
    side_signal = valid & supported & in_range & gap_pass & persistence_pass
    signal = side_signal.any(dim=1)
    signal_gap = torch.where(side_signal, gap, torch.full_like(gap, -torch.inf))
    diagnostic_candidate = valid & supported & in_range
    diagnostic_gap = torch.where(
        diagnostic_candidate, gap, torch.full_like(gap, -torch.inf)
    )
    comparable_gap = torch.where(signal[:, None], signal_gap, diagnostic_gap)
    side = torch.argmax(comparable_gap, dim=1)
    gather = side[:, None]

    depth_prices = batch.ask_depth_prices
    depth_sizes = torch.nan_to_num(
        batch.ask_depth_sizes, nan=0.0, posinf=0.0, neginf=0.0
    )
    usable = (
        torch.isfinite(depth_prices)
        & (depth_prices > 0)
        & (depth_prices <= variant.maximum_ask)
        & (depth_sizes > 0)
    )
    prices = torch.where(usable, depth_prices, torch.ones_like(depth_prices))
    sizes = torch.where(usable, depth_sizes, torch.zeros_like(depth_sizes))
    level_notional = prices * sizes
    before = torch.cumsum(level_notional, dim=2) - level_notional
    remaining = torch.clamp(target_notional_usdc - before, min=0)
    taken = torch.minimum(sizes, remaining / prices)
    side_shares = taken.sum(dim=2)
    side_cost = (taken * prices).sum(dim=2)
    side_fee = calculate_platform_fee(
        taken,
        prices,
        fee_rate,
        decimals=fee_round_decimals,
        exponent=fee_exponent,
    )
    chosen_shares = side_shares.gather(1, gather).squeeze(1)
    chosen_cost = side_cost.gather(1, gather).squeeze(1)
    chosen_fee = side_fee.gather(1, gather).squeeze(1)
    chosen_vwap = torch.where(
        chosen_shares > 0,
        chosen_cost / chosen_shares,
        torch.full_like(chosen_shares, float("nan")),
    )
    filled = signal & torch.isfinite(chosen_vwap) & (chosen_shares > 0)
    outcome_side = torch.where(side == 0, batch.outcome_up, 1 - batch.outcome_up)
    gross = torch.where(
        filled,
        chosen_shares * outcome_side - chosen_cost,
        torch.zeros_like(chosen_cost),
    )
    fee = torch.where(filled, chosen_fee, torch.zeros_like(chosen_fee))
    shares = torch.where(filled, chosen_shares, torch.zeros_like(chosen_shares))
    fill_cost = torch.where(filled, chosen_cost, torch.zeros_like(chosen_cost))
    primary_extra_cost = shares * primary_extra_cost_per_share
    stress_extra_cost = shares * stress_extra_cost_per_share
    primary_pnl = gross - fee - primary_extra_cost
    stress_pnl = gross - fee - stress_extra_cost

    market_supported = (valid & supported).any(dim=1)
    market_range = (valid & supported & in_range).any(dim=1)
    market_gap = (valid & supported & in_range & gap_pass).any(dim=1)
    eligible_range_gap = gap[diagnostic_candidate]
    status = torch.full((len(batch),), 1, dtype=torch.int64, device=states.device)
    status[batch.snapshot_valid & ~market_supported] = 2
    status[market_supported & ~market_range] = 3
    status[market_range & ~market_gap] = 4
    status[market_gap & ~signal] = 5
    status[signal & ~filled] = 6
    status[filled] = 0

    def chosen(values: torch.Tensor) -> torch.Tensor:
        return values.gather(1, gather).squeeze(1)

    return LiteralEvaluation(
        current_state=chosen(states),
        destination_state=chosen(destination),
        row_support=chosen(support),
        transition_probability=chosen(probability),
        destination_persistence=chosen(persistence),
        signal_ask=chosen(asks),
        gap=chosen(gap),
        side=side,
        signal=signal,
        fill_vwap=chosen_vwap,
        fill_cost=fill_cost,
        platform_fee=fee,
        fill_shares=shares,
        primary_extra_cost=primary_extra_cost,
        stress_extra_cost=stress_extra_cost,
        primary_pnl=primary_pnl,
        stress_pnl=stress_pnl,
        filled=filled,
        status_code=status,
        funnel={
            "markets": len(batch),
            "valid_snapshot": int(batch.snapshot_valid.sum().item()),
            "row_supported": int(market_supported.sum().item()),
            "ask_in_range": int(market_range.sum().item()),
            "gap_pass": int(market_gap.sum().item()),
            "maximum_gap_after_ask": float(eligible_range_gap.max().item())
            if eligible_range_gap.numel()
            else None,
            "destination_persistence_pass": int(signal.sum().item()),
            "signals": int(signal.sum().item()),
            "fills": int(filled.sum().item()),
        },
    )


def _decision_rows(
    source_id: str,
    fold_id: str,
    variant: SourceVariant,
    batch: DecisionBatch,
    evaluation: LiteralEvaluation,
) -> list[dict[str, Any]]:
    cpu_batch = batch.to(torch.device("cpu"), torch.float64)
    tensors = {
        "current_state": evaluation.current_state.detach().cpu(),
        "destination_state": evaluation.destination_state.detach().cpu(),
        "row_support": evaluation.row_support.detach().cpu(),
        "transition_probability": evaluation.transition_probability.detach().cpu(),
        "destination_persistence": evaluation.destination_persistence.detach().cpu(),
        "signal_ask": evaluation.signal_ask.detach().cpu(),
        "gap": evaluation.gap.detach().cpu(),
        "side_code": evaluation.side.detach().cpu(),
        "signal": evaluation.signal.detach().cpu(),
        "fill_vwap": evaluation.fill_vwap.detach().cpu(),
        "fill_cost": evaluation.fill_cost.detach().cpu(),
        "platform_fee": evaluation.platform_fee.detach().cpu(),
        "fill_shares": evaluation.fill_shares.detach().cpu(),
        "primary_extra_cost": evaluation.primary_extra_cost.detach().cpu(),
        "stress_extra_cost": evaluation.stress_extra_cost.detach().cpu(),
        "primary_pnl": evaluation.primary_pnl.detach().cpu(),
        "stress_pnl": evaluation.stress_pnl.detach().cpu(),
        "filled": evaluation.filled.detach().cpu(),
        "status_code": evaluation.status_code.detach().cpu(),
    }
    rows: list[dict[str, Any]] = []
    for index, condition_id in enumerate(cpu_batch.condition_ids):
        side = int(tensors["side_code"][index].item())
        outcome_side = float(
            cpu_batch.outcome_up[index].item()
            if side == 0
            else 1 - cpu_batch.outcome_up[index].item()
        )
        row: dict[str, Any] = {
            "source": source_id,
            "variant": variant.variant_id,
            "fold_id": fold_id,
            "condition_id": condition_id,
            "decision_utc": datetime.fromtimestamp(
                int(cpu_batch.decision_s[index].item()), tz=UTC
            ).isoformat(),
            "decision_s": int(cpu_batch.decision_s[index].item()),
            "side": "UP" if side == 0 else "DOWN",
            "outcome_side": outcome_side,
            "current_mid_up": float(cpu_batch.current_mid_up[index].item()),
            "previous_mid_up": float(cpu_batch.previous_mid_up[index].item()),
            "minimum_gap": variant.minimum_gap,
            "minimum_destination_persistence": (
                variant.minimum_destination_persistence
            ),
        }
        for name, values in tensors.items():
            value = values[index].item()
            row[name] = bool(value) if name in {"signal", "filled"} else value
        row["status"] = STATUS_NAMES[int(row["status_code"])]
        rows.append(row)
    return rows


def _profit_factor(pnl: torch.Tensor) -> tuple[float | None, bool]:
    profit = float(pnl[pnl > 0].sum().item())
    loss = float(-pnl[pnl < 0].sum().item())
    return (profit / loss if loss else None, bool(profit > 0 and loss == 0))


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise LiteralMarkovError("cannot summarize empty decisions")
    ordered = sorted(rows, key=lambda item: (item["decision_s"], item["condition_id"]))
    signal = torch.tensor([row["signal"] for row in ordered], dtype=torch.bool)
    filled = torch.tensor([row["filled"] for row in ordered], dtype=torch.bool)
    primary_all = torch.tensor(
        [row["primary_pnl"] for row in ordered], dtype=torch.float64
    )
    stress_all = torch.tensor(
        [row["stress_pnl"] for row in ordered], dtype=torch.float64
    )
    primary = primary_all[filled]
    stress = stress_all[filled]
    cumulative = torch.cumsum(primary_all, dim=0)
    peaks = torch.cummax(
        torch.cat((torch.zeros(1, dtype=torch.float64), cumulative)), dim=0
    ).values[1:]
    pf, pf_infinite = _profit_factor(primary)
    stress_pf, stress_pf_infinite = _profit_factor(stress)
    turnover = sum(
        row["fill_cost"] + row["platform_fee"] + row["primary_extra_cost"]
        for row in ordered
        if row["filled"]
    )
    return {
        "markets": len(ordered),
        "signals": int(signal.sum().item()),
        "fills": int(filled.sum().item()),
        "net_pnl_usdc": float(primary.sum().item()),
        "stress_net_pnl_usdc": float(stress.sum().item()),
        "cash_turnover_usdc": float(turnover),
        "return_on_turnover": float(primary.sum().item()) / turnover
        if turnover
        else None,
        "profit_factor": pf,
        "profit_factor_infinite": pf_infinite,
        "stress_profit_factor": stress_pf,
        "stress_profit_factor_infinite": stress_pf_infinite,
        "max_drawdown_usdc": float((peaks - cumulative).max().item()),
        "win_rate": float(
            torch.tensor(
                [row["outcome_side"] for row in ordered if row["filled"]],
                dtype=torch.float64,
            )
            .gt(0.5)
            .to(torch.float64)
            .mean()
            .item()
        )
        if bool(filled.any().item())
        else None,
    }


def _render_variant(
    path: Path,
    source_id: str,
    variant: SourceVariant,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    ordered = sorted(rows, key=lambda item: (item["decision_s"], item["condition_id"]))
    times = [row["decision_utc"] for row in ordered]
    primary = torch.tensor([row["primary_pnl"] for row in ordered], dtype=torch.float64)
    cumulative = torch.cumsum(primary, dim=0)
    peaks = torch.cummax(
        torch.cat((torch.zeros(1, dtype=torch.float64), cumulative)), dim=0
    ).values[1:]
    signal_rows = [row for row in ordered if row["signal"]]
    fill_rows = [row for row in ordered if row["filled"]]
    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        specs=[[{}], [{}], [{}], [{"secondary_y": True}]],
        subplot_titles=(
            "Maximum transition probability and article gap",
            "Destination-state persistence",
            "Signals and fills",
            "Cumulative PnL and drawdown",
        ),
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=[row["transition_probability"] for row in ordered],
            name="p_hat = P[i,j*]",
            opacity=0.55,
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=[row["gap"] for row in ordered],
            name="gap = p_hat - ask",
            opacity=0.55,
        ),
        row=1,
        col=1,
    )
    figure.add_hline(
        y=variant.minimum_gap, line_dash="dash", line_color="black", row=1, col=1
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=[row["destination_persistence"] for row in ordered],
            name="P[j*,j*]",
        ),
        row=2,
        col=1,
    )
    figure.add_hline(
        y=variant.minimum_destination_persistence,
        line_dash="dash",
        line_color="firebrick",
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=[row["decision_utc"] for row in signal_rows],
            y=[row["signal_ask"] for row in signal_rows],
            mode="markers",
            name="article signal",
            marker={"symbol": "circle-open", "size": 8},
        ),
        row=3,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=[row["decision_utc"] for row in fill_rows],
            y=[row["signal_ask"] for row in fill_rows],
            mode="markers",
            name="fill",
            marker={"symbol": "diamond", "size": 8},
        ),
        row=3,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=times, y=cumulative.tolist(), name="cumulative PnL"),
        row=4,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=(peaks - cumulative).tolist(),
            name="drawdown",
            fill="tozeroy",
            line={"color": "firebrick"},
        ),
        row=4,
        col=1,
        secondary_y=True,
    )
    figure.update_layout(
        title=(
            f"Stage 4f {source_id} / {variant.variant_id}: "
            f"signals {summary['signals']}, fills {summary['fills']}, "
            f"PnL {summary['net_pnl_usdc']:.2f} USDC"
        ),
        template="plotly_white",
        height=1_180,
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.02},
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def _render_matrices(path: Path, source_id: str, records: list[dict[str, Any]]) -> None:
    columns = 3
    rows_count = math.ceil(len(records) / columns)
    figure = make_subplots(
        rows=rows_count,
        cols=columns,
        subplot_titles=tuple(record["fold_id"] for record in records),
    )
    for index, record in enumerate(records):
        row = index // columns + 1
        column = index % columns + 1
        figure.add_trace(
            go.Heatmap(
                z=record["transition_matrix"],
                zmin=0,
                zmax=1,
                coloraxis="coloraxis",
                text=record["transition_matrix"],
                texttemplate="%{text:.2f}",
                hovertemplate="from %{y} to %{x}: %{z:.4f}<extra></extra>",
                showscale=False,
            ),
            row=row,
            col=column,
        )
    figure.update_layout(
        title=f"Stage 4f {source_id}: train-only transition matrices",
        template="plotly_white",
        height=330 * rows_count,
        coloraxis={"colorscale": "Blues", "cmin": 0, "cmax": 1},
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def _load_sources(
    config: LiteralMarkovConfig, data_root: str | Path
) -> tuple[SourceData, SourceData]:
    primary = load_regime_config(config.primary_source_config)
    if (
        primary.device != config.device
        or primary.dtype != config.dtype
        or primary.development_end_exclusive_s
        != primary.folds[-1].validation_end_exclusive_s
    ):
        raise LiteralMarkovError("primary source contract differs")
    dataset = load_kacho_dataset_config(primary.dataset_config)
    primary_batch = load_kacho_decision_batch(
        dataset,
        data_root,
        assets=(config.asset,),
        max_markets=0,
        decision_seconds_before_end=config.decision_seconds_before_end,
        transition_horizon_seconds=config.transition_horizon_seconds,
        label_policy="kacho_inferred_development_only",
        execution_latency_seconds=config.execution_latency_seconds,
        period_start_s=primary.period_start_s,
        period_end_exclusive_s=primary.development_end_exclusive_s,
    )
    primary_order = torch.argsort(primary_batch.market_start_s, stable=True)
    primary_batch = primary_batch.index(primary_order)
    manifest = dataset.dataset_dir(data_root) / "manifest.json"
    primary_source = SourceData(
        source_id="kacho_primary",
        batch=primary_batch,
        folds=primary.folds,
        fee_rate=primary.platform_fee_rate,
        fee_exponent=1.0,
        provenance={
            "dataset_config": primary.dataset_config,
            "dataset_config_sha256": dataset.config_sha256,
            "dataset_manifest": str(manifest),
            "dataset_manifest_sha256": _sha256(manifest),
            "label_source": primary_batch.label_source,
            "execution_depth": "kacho_exact_second_l2",
        },
    )

    early = load_early_robustness_config(config.early_source_config)
    early_batch, early_provenance = load_trent_decision_batch(
        early.dataset_config,
        early.outcome_config,
        data_root,
        decision_seconds_before_end=config.decision_seconds_before_end,
        transition_horizon_seconds=config.transition_horizon_seconds,
        execution_latency_seconds=config.execution_latency_seconds,
        target_notional_usdc=config.target_notional_usdc,
        minimum_nonempty_market_fraction=early.minimum_nonempty_market_fraction,
    )
    gamma = early_provenance["gamma"]
    early_source = SourceData(
        source_id="trent_early",
        batch=early_batch,
        folds=early.folds,
        fee_rate=float(gamma["fee_rate"]),
        fee_exponent=float(gamma["fee_exponent"]),
        provenance=early_provenance,
    )
    return primary_source, early_source


def _fold_indices(batch: DecisionBatch, start_s: int, end_s: int) -> torch.Tensor:
    return torch.nonzero(
        (batch.market_start_s >= start_s) & (batch.market_start_s < end_s),
        as_tuple=False,
    ).flatten()


def _run_source_variant(
    source: SourceData,
    variant: SourceVariant,
    config: LiteralMarkovConfig,
    edges: torch.Tensor,
    device: torch.device,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows: list[dict[str, Any]] = []
    fold_metrics: list[dict[str, Any]] = []
    matrices: list[dict[str, Any]] = []
    for fold in source.folds:
        train_indices = _fold_indices(
            source.batch, fold.train_start_s, fold.train_end_exclusive_s
        )
        validation_indices = _fold_indices(
            source.batch, fold.validation_start_s, fold.validation_end_exclusive_s
        )
        train = source.batch.index(train_indices).to(device, torch.float64)
        validation = source.batch.index(validation_indices).to(device, torch.float64)
        fee_rate = source.fee_rate
        if isinstance(fee_rate, torch.Tensor):
            fee_rate = fee_rate.index_select(0, validation_indices).to(device)
        model = fit_transition_model(
            train, edges, pseudocount=config.transition_pseudocount
        )
        evaluation = evaluate_literal_markov(
            validation,
            model,
            variant,
            minimum_row_transitions=config.minimum_row_transitions,
            target_notional_usdc=config.target_notional_usdc,
            fee_rate=fee_rate,
            fee_exponent=source.fee_exponent,
            fee_round_decimals=config.platform_fee_round_decimals,
            primary_extra_cost_per_share=config.primary_extra_cost_per_share,
            stress_extra_cost_per_share=config.stress_extra_cost_per_share,
        )
        rows = _decision_rows(
            source.source_id, fold.fold_id, variant, validation, evaluation
        )
        metrics = _metrics(rows)
        metrics.update({"fold_id": fold.fold_id, "train_markets": len(train)})
        metrics.update(evaluation.funnel)
        all_rows.extend(rows)
        fold_metrics.append(metrics)
        matrices.append(
            {
                "fold_id": fold.fold_id,
                "train_markets": len(train),
                "row_support": model.row_support.detach().cpu().tolist(),
                "transition_counts": model.counts.detach().cpu().tolist(),
                "transition_matrix": model.matrix.detach().cpu().tolist(),
                "destination_state": model.destination.detach().cpu().tolist(),
                "maximum_probability": (
                    model.maximum_probability.detach().cpu().tolist()
                ),
                "destination_persistence": (
                    model.destination_persistence.detach().cpu().tolist()
                ),
            }
        )
    return all_rows, fold_metrics, matrices


def _classification(
    variant_results: dict[str, dict[str, dict[str, Any]]],
    config: LiteralMarkovConfig,
) -> dict[str, Any]:
    variants: dict[str, Any] = {}
    for variant_id, sources in variant_results.items():
        summaries = [record["summary"] for record in sources.values()]
        folds = [fold for record in sources.values() for fold in record["folds"]]
        total_signals = sum(item["signals"] for item in summaries)
        total_fills = sum(item["fills"] for item in summaries)
        positive_folds = sum(item["net_pnl_usdc"] > 0 for item in folds)
        source_gates = {
            source_id: {
                "minimum_fills": record["summary"]["fills"]
                >= config.minimum_candidate_fills,
                "positive_primary_pnl": record["summary"]["net_pnl_usdc"] > 0,
                "positive_stress_pnl": record["summary"]["stress_net_pnl_usdc"] > 0,
                "minimum_profit_factor": (
                    math.inf
                    if record["summary"]["profit_factor_infinite"]
                    else (record["summary"]["profit_factor"] or 0)
                )
                >= config.minimum_profit_factor,
            }
            for source_id, record in sources.items()
        }
        candidate = (
            all(all(gates.values()) for gates in source_gates.values())
            and positive_folds >= config.minimum_positive_folds
        )
        if total_signals == 0:
            status = "source_rule_no_signals"
        elif total_fills == 0:
            status = "source_rule_not_executable"
        elif candidate:
            status = "development_candidate"
        else:
            status = "source_rule_unprofitable"
        variants[variant_id] = {
            "status": status,
            "signals": total_signals,
            "fills": total_fills,
            "positive_folds": positive_folds,
            "source_gates": source_gates,
        }
    return {
        "schema_version": 1,
        "selection_performed": False,
        "holdout_opened": False,
        "variants": variants,
    }


def run_stage4f_literal_markov(
    config_path: str | Path,
    *,
    data_root: str | Path = "data/historical",
    output_root: str | Path = "outputs/literal_markov",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    source_dirty = bool(_git(["status", "--porcelain"]))
    config = load_literal_markov_config(config_path)
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise LiteralMarkovError("frozen CUDA device is unavailable")
    torch.manual_seed(config.seed)
    sources = _load_sources(config, data_root)
    edges = torch.tensor(config.state_bin_edges, dtype=torch.float64, device=device)
    run_dir = Path(output_root) / (
        f"{started.strftime('%Y%m%dT%H%M%SZ')}_{config.experiment_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    result_records: dict[str, dict[str, dict[str, Any]]] = {
        variant.variant_id: {} for variant in config.variants
    }
    for source in sources:
        source_dir = run_dir / source.source_id
        source_dir.mkdir()
        source_matrices: list[dict[str, Any]] | None = None
        for variant in config.variants:
            rows, folds, matrices = _run_source_variant(
                source, variant, config, edges, device
            )
            summary = _metrics(rows)
            summary["positive_folds"] = sum(item["net_pnl_usdc"] > 0 for item in folds)
            summary["positive_stress_folds"] = sum(
                item["stress_net_pnl_usdc"] > 0 for item in folds
            )
            variant_dir = source_dir / variant.variant_id
            variant_dir.mkdir()
            _write_csv(variant_dir / "decisions.csv", rows)
            _write_csv(variant_dir / "fold_metrics.csv", folds)
            _write_json(variant_dir / "summary.json", summary)
            _write_json(variant_dir / "transition_matrices.json", matrices)
            _render_variant(
                variant_dir / "diagnostics.html",
                source.source_id,
                variant,
                rows,
                summary,
            )
            result_records[variant.variant_id][source.source_id] = {
                "summary": summary,
                "folds": folds,
            }
            source_matrices = matrices
        if source_matrices is None:
            raise LiteralMarkovError("no variants were evaluated")
        _render_matrices(
            source_dir / "transition_matrices.html",
            source.source_id,
            source_matrices,
        )
        _write_json(source_dir / "source_provenance.json", source.provenance)
    result = _classification(result_records, config)
    result["source_results"] = result_records
    _write_json(run_dir / "result.json", result)
    _write_json(
        run_dir / "run_record.json",
        {
            "schema_version": 1,
            "experiment_id": config.experiment_id,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_seconds": time.monotonic() - started_monotonic,
            "source_commit": source_commit,
            "source_dirty": source_dirty,
            "config_path": str(config_path),
            "config_sha256": config.config_sha256,
            "config_file_sha256": _sha256(config_path),
            "primary_source_config_sha256": _sha256(config.primary_source_config),
            "early_source_config_sha256": _sha256(config.early_source_config),
            "sources": {
                source.source_id: {
                    "rows": len(source.batch),
                    "valid_rows": int(source.batch.snapshot_valid.sum().item()),
                    "folds": len(source.folds),
                    "provenance": source.provenance,
                }
                for source in sources
            },
            "device": str(device),
            "dtype": config.dtype,
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(device)
            if device.type == "cuda"
            else None,
            "plotly_version": plotly.__version__,
            "python": platform.python_version(),
        },
    )
    return run_dir
