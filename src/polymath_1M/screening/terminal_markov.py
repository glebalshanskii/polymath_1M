from __future__ import annotations

import hashlib
import json
import math
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
from polymath_1M.strategy.model import calculate_platform_fee

from .literal_markov import (
    SourceData,
    _fold_indices,
    _git,
    _load_sources,
    _sha256,
    _write_csv,
    _write_json,
)


class TerminalMarkovError(RuntimeError):
    """The frozen terminal-Markov contract or its inputs are invalid."""


@dataclass(frozen=True)
class TerminalVariant:
    variant_id: str
    state_policy: str
    apply_support_gate: bool


@dataclass(frozen=True)
class TerminalMarkovConfig:
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
    minimum_state_support: int
    terminal_residual_prior_strength: float
    maximum_absolute_residual: float
    minimum_probability: float
    maximum_probability: float
    variants: tuple[TerminalVariant, ...]
    minimum_ask: float
    maximum_ask: float
    minimum_persistence: float
    minimum_net_edge: float
    target_notional_usdc: float
    platform_fee_round_decimals: int
    primary_extra_cost_per_share: float
    stress_extra_cost_per_share: float
    candidate_variant: str
    ablation_variant: str
    minimum_candidate_fills_per_source: int
    minimum_positive_primary_folds: int
    minimum_positive_kacho_folds: int
    minimum_positive_trent_folds: int
    minimum_profit_factor: float
    seed: int
    device: str
    dtype: str
    config_sha256: str


@dataclass(frozen=True)
class TerminalModel:
    edges: torch.Tensor
    state_policy: str
    state_support: torch.Tensor
    residual_sum: torch.Tensor
    residual: torch.Tensor
    state_mean_mid: torch.Tensor
    absorption_at_state_mean: torch.Tensor
    transition_counts: torch.Tensor
    transition_matrix: torch.Tensor
    persistence: torch.Tensor


@dataclass(frozen=True)
class TerminalEvaluation:
    previous_state: torch.Tensor
    current_state: torch.Tensor
    terminal_state: torch.Tensor
    state_support: torch.Tensor
    residual: torch.Tensor
    probability_up: torch.Tensor
    forecast_probability: torch.Tensor
    persistence: torch.Tensor
    signal_ask: torch.Tensor
    decision_fee_per_share: torch.Tensor
    net_edge: torch.Tensor
    side: torch.Tensor
    signal: torch.Tensor
    fill_vwap: torch.Tensor
    maximum_executed_price: torch.Tensor
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


STATUS_NAMES = (
    "filled",
    "data_invalid",
    "insufficient_support",
    "ask_out_of_range",
    "low_persistence",
    "edge_below_threshold",
    "side_tie",
    "no_liquidity",
)


def load_terminal_markov_config(path: str | Path) -> TerminalMarkovConfig:
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
        "minimum_state_support",
        "terminal_residual_prior_strength",
        "maximum_absolute_residual",
        "minimum_probability",
        "maximum_probability",
        "variants",
        "minimum_ask",
        "maximum_ask",
        "minimum_persistence",
        "minimum_net_edge",
        "target_notional_usdc",
        "platform_fee_round_decimals",
        "primary_extra_cost_per_share",
        "stress_extra_cost_per_share",
        "candidate_variant",
        "ablation_variant",
        "minimum_candidate_fills_per_source",
        "minimum_positive_primary_folds",
        "minimum_positive_kacho_folds",
        "minimum_positive_trent_folds",
        "minimum_profit_factor",
        "seed",
        "device",
        "dtype",
    }
    if payload.keys() != expected:
        raise TerminalMarkovError(
            f"Stage 4g config differs: missing={sorted(expected - payload.keys())}, "
            f"extra={sorted(payload.keys() - expected)}"
        )
    variant_fields = {"variant_id", "state_policy", "apply_support_gate"}
    variants: list[TerminalVariant] = []
    for raw in payload["variants"]:
        if raw.keys() != variant_fields:
            raise TerminalMarkovError("Stage 4g variant fields differ")
        variants.append(
            TerminalVariant(
                variant_id=str(raw["variant_id"]),
                state_policy=str(raw["state_policy"]),
                apply_support_gate=bool(raw["apply_support_gate"]),
            )
        )
    edges = tuple(float(value) for value in payload["state_bin_edges"])
    variant_contract = tuple(
        (item.variant_id, item.state_policy, item.apply_support_gate)
        for item in variants
    )
    if (
        payload["schema_version"] != 1
        or payload["asset"] != "BTC"
        or payload["duration"] != "5m"
        or int(payload["decision_seconds_before_end"]) != 60
        or int(payload["transition_horizon_seconds"]) != 60
        or int(payload["execution_latency_seconds"]) != 1
        or edges != (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.000001)
        or float(payload["transition_pseudocount"]) != 1.0
        or int(payload["minimum_state_support"]) != 50
        or float(payload["terminal_residual_prior_strength"]) != 100.0
        or float(payload["maximum_absolute_residual"]) != 0.10
        or float(payload["minimum_probability"]) != 0.01
        or float(payload["maximum_probability"]) != 0.99
        or variant_contract
        != (
            (
                "market_anchor_control",
                "market_mid_no_learned_residual",
                False,
            ),
            (
                "current_state_control",
                "current_price_bin_terminal_residual",
                True,
            ),
            (
                "transition_pair_candidate",
                "previous_current_price_bin_terminal_residual",
                True,
            ),
        )
        or float(payload["minimum_ask"]) != 0.60
        or float(payload["maximum_ask"]) != 0.90
        or float(payload["minimum_persistence"]) != 0.87
        or float(payload["minimum_net_edge"]) != 0.02
        or float(payload["target_notional_usdc"]) != 10.0
        or int(payload["platform_fee_round_decimals"]) != 4
        or float(payload["primary_extra_cost_per_share"]) != 0.01
        or float(payload["stress_extra_cost_per_share"]) != 0.02
        or payload["candidate_variant"] != "transition_pair_candidate"
        or payload["ablation_variant"] != "current_state_control"
        or int(payload["minimum_candidate_fills_per_source"]) != 100
        or int(payload["minimum_positive_primary_folds"]) != 6
        or int(payload["minimum_positive_kacho_folds"]) != 4
        or int(payload["minimum_positive_trent_folds"]) != 2
        or float(payload["minimum_profit_factor"]) != 1.10
        or int(payload["seed"]) != 20260813
        or payload["device"] != "cuda"
        or payload["dtype"] != "float64"
    ):
        raise TerminalMarkovError("Stage 4g frozen numerical contract differs")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return TerminalMarkovConfig(
        experiment_id=str(payload["experiment_id"]),
        primary_source_config=str(payload["primary_source_config"]),
        early_source_config=str(payload["early_source_config"]),
        asset="BTC",
        duration="5m",
        decision_seconds_before_end=60,
        transition_horizon_seconds=60,
        execution_latency_seconds=1,
        state_bin_edges=edges,
        transition_pseudocount=1.0,
        minimum_state_support=50,
        terminal_residual_prior_strength=100.0,
        maximum_absolute_residual=0.10,
        minimum_probability=0.01,
        maximum_probability=0.99,
        variants=tuple(variants),
        minimum_ask=0.60,
        maximum_ask=0.90,
        minimum_persistence=0.87,
        minimum_net_edge=0.02,
        target_notional_usdc=10.0,
        platform_fee_round_decimals=4,
        primary_extra_cost_per_share=0.01,
        stress_extra_cost_per_share=0.02,
        candidate_variant=str(payload["candidate_variant"]),
        ablation_variant=str(payload["ablation_variant"]),
        minimum_candidate_fills_per_source=100,
        minimum_positive_primary_folds=6,
        minimum_positive_kacho_folds=4,
        minimum_positive_trent_folds=2,
        minimum_profit_factor=1.10,
        seed=20260813,
        device="cuda",
        dtype="float64",
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _side_values(
    batch: DecisionBatch,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    previous = torch.stack((batch.previous_mid_up, 1 - batch.previous_mid_up), dim=1)
    current = torch.stack((batch.current_mid_up, 1 - batch.current_mid_up), dim=1)
    outcome = torch.stack((batch.outcome_up, 1 - batch.outcome_up), dim=1)
    return previous, current, outcome


def _state_index(
    previous_state: torch.Tensor,
    current_state: torch.Tensor,
    bins: int,
    state_policy: str,
) -> tuple[torch.Tensor, int]:
    if state_policy in {
        "market_mid_no_learned_residual",
        "current_price_bin_terminal_residual",
    }:
        return current_state, bins
    if state_policy == "previous_current_price_bin_terminal_residual":
        return previous_state * bins + current_state, bins * bins
    raise TerminalMarkovError(f"unknown terminal state policy: {state_policy}")


def fit_terminal_model(
    train: DecisionBatch,
    edges: torch.Tensor,
    variant: TerminalVariant,
    *,
    transition_pseudocount: float,
    residual_prior_strength: float,
    maximum_absolute_residual: float,
) -> TerminalModel:
    if train.current_mid_up.dtype != torch.float64 or edges.dtype != torch.float64:
        raise TypeError("terminal Markov model requires float64 tensors")
    if (
        edges.ndim != 1
        or edges.numel() < 3
        or transition_pseudocount < 0
        or residual_prior_strength <= 0
        or maximum_absolute_residual <= 0
    ):
        raise ValueError("terminal Markov fit parameters are invalid")
    previous, current, outcome = _side_values(train)
    valid = (
        train.snapshot_valid.unsqueeze(1)
        & torch.isfinite(previous)
        & torch.isfinite(current)
        & torch.isfinite(outcome)
        & (previous >= 0)
        & (previous <= 1)
        & (current >= 0)
        & (current <= 1)
        & (outcome >= 0)
        & (outcome <= 1)
    )
    if not bool(valid.any().item()):
        raise TerminalMarkovError("train fold has no valid labeled snapshots")
    bins = edges.numel() - 1
    previous_state = torch.bucketize(previous, edges[1:-1], right=False)
    current_state = torch.bucketize(current, edges[1:-1], right=False)
    state, state_count = _state_index(
        previous_state, current_state, bins, variant.state_policy
    )
    flat_transition = previous_state[valid] * bins + current_state[valid]
    transition_counts = torch.bincount(flat_transition, minlength=bins * bins).reshape(
        bins, bins
    )
    smoothed = transition_counts.to(torch.float64) + transition_pseudocount
    transition_matrix = smoothed / smoothed.sum(dim=1, keepdim=True)

    valid_state = state[valid]
    valid_mid = current[valid]
    valid_outcome = outcome[valid]
    support = torch.bincount(valid_state, minlength=state_count)
    residual_sum = torch.bincount(
        valid_state, weights=valid_outcome - valid_mid, minlength=state_count
    ).to(torch.float64)
    mid_sum = torch.bincount(valid_state, weights=valid_mid, minlength=state_count).to(
        torch.float64
    )
    denominator = support.to(torch.float64) + residual_prior_strength
    if variant.state_policy == "market_mid_no_learned_residual":
        residual = torch.zeros_like(residual_sum)
    else:
        residual = torch.clamp(
            residual_sum / denominator,
            -maximum_absolute_residual,
            maximum_absolute_residual,
        )
    default_mid = torch.full_like(mid_sum, 0.5)
    mean_mid = torch.where(
        support > 0,
        mid_sum / torch.clamp(support.to(torch.float64), min=1),
        default_mid,
    )
    state_probability = torch.clamp(mean_mid + residual, 0.0, 1.0)
    absorption = torch.stack((state_probability, 1 - state_probability), dim=1)
    return TerminalModel(
        edges=edges,
        state_policy=variant.state_policy,
        state_support=support,
        residual_sum=residual_sum,
        residual=residual,
        state_mean_mid=mean_mid,
        absorption_at_state_mean=absorption,
        transition_counts=transition_counts,
        transition_matrix=transition_matrix,
        persistence=torch.diagonal(transition_matrix),
    )


def _fee_rate_by_market(
    value: float | torch.Tensor, reference: torch.Tensor
) -> torch.Tensor:
    tensor = torch.as_tensor(value, dtype=reference.dtype, device=reference.device)
    if tensor.ndim == 0:
        return tensor.expand(reference.shape[0])
    if tensor.ndim != 1 or tensor.shape[0] != reference.shape[0]:
        raise ValueError("fee parameter must be scalar or have shape [market]")
    return tensor


def evaluate_terminal_markov(
    batch: DecisionBatch,
    model: TerminalModel,
    variant: TerminalVariant,
    *,
    minimum_state_support: int,
    minimum_probability: float,
    maximum_probability: float,
    minimum_ask: float,
    maximum_ask: float,
    minimum_persistence: float,
    minimum_net_edge: float,
    target_notional_usdc: float,
    fee_rate: float | torch.Tensor,
    fee_exponent: float | torch.Tensor,
    fee_round_decimals: int,
    primary_extra_cost_per_share: float,
    stress_extra_cost_per_share: float,
) -> TerminalEvaluation:
    if model.state_policy != variant.state_policy:
        raise TerminalMarkovError("terminal model/variant policy mismatch")
    previous, current, _ = _side_values(batch)
    bins = model.edges.numel() - 1
    previous_state = torch.bucketize(previous, model.edges[1:-1], right=False)
    current_state = torch.bucketize(current, model.edges[1:-1], right=False)
    terminal_state, _ = _state_index(
        previous_state, current_state, bins, variant.state_policy
    )
    support = model.state_support[terminal_state]
    residual = model.residual[terminal_state]
    probabilities = torch.clamp(
        current + residual, minimum_probability, maximum_probability
    )
    persistence = model.persistence[current_state]

    asks = batch.asks
    rates = _fee_rate_by_market(fee_rate, asks)[:, None]
    exponents = _fee_rate_by_market(fee_exponent, asks)[:, None]
    decision_fee = rates * torch.pow(asks * (1 - asks), exponents)
    side_edge = probabilities - asks - decision_fee - primary_extra_cost_per_share
    valid = batch.snapshot_valid.unsqueeze(1)
    support_pass = (
        support >= minimum_state_support
        if variant.apply_support_gate
        else torch.ones_like(support, dtype=torch.bool)
    )
    range_pass = (asks >= minimum_ask) & (asks <= maximum_ask)
    persistence_pass = persistence >= minimum_persistence
    edge_pass = side_edge >= minimum_net_edge
    side_signal = valid & support_pass & range_pass & persistence_pass & edge_pass
    comparable = torch.where(
        side_signal, side_edge, torch.full_like(side_edge, -torch.inf)
    )
    diagnostic = torch.where(valid, side_edge, torch.full_like(side_edge, -torch.inf))
    signal_count = side_signal.sum(dim=1)
    side = torch.argmax(
        torch.where(signal_count[:, None] > 0, comparable, diagnostic), dim=1
    )
    signal_tie = (signal_count == 2) & torch.isclose(
        side_edge[:, 0], side_edge[:, 1], atol=1e-12, rtol=0
    )
    signal = (signal_count > 0) & ~signal_tie
    gather = side[:, None]

    depth_prices = batch.ask_depth_prices
    depth_sizes = torch.nan_to_num(
        batch.ask_depth_sizes, nan=0.0, posinf=0.0, neginf=0.0
    )
    level_rates = _fee_rate_by_market(fee_rate, asks)[:, None, None]
    level_exponents = _fee_rate_by_market(fee_exponent, asks)[:, None, None]
    finite_level = (
        torch.isfinite(depth_prices) & (depth_prices > 0) & (depth_prices <= 1)
    )
    safe_level_price = torch.where(
        finite_level, depth_prices, torch.ones_like(depth_prices)
    )
    level_fee = level_rates * torch.pow(
        safe_level_price * (1 - safe_level_price), level_exponents
    )
    level_edge = (
        probabilities.unsqueeze(2)
        - safe_level_price
        - level_fee
        - primary_extra_cost_per_share
    )
    usable = (
        finite_level
        & (safe_level_price <= maximum_ask)
        & (depth_sizes > 0)
        & (level_edge >= minimum_net_edge)
    )
    prices = torch.where(usable, safe_level_price, torch.ones_like(depth_prices))
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
    side_maximum_price = (
        torch.where(taken > 0, prices, torch.full_like(prices, -torch.inf))
        .max(dim=2)
        .values
    )
    chosen_shares = side_shares.gather(1, gather).squeeze(1)
    chosen_cost = side_cost.gather(1, gather).squeeze(1)
    chosen_fee = side_fee.gather(1, gather).squeeze(1)
    chosen_maximum_price = side_maximum_price.gather(1, gather).squeeze(1)
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
    maximum_executed_price = torch.where(
        filled,
        chosen_maximum_price,
        torch.full_like(chosen_maximum_price, float("nan")),
    )
    primary_extra = shares * primary_extra_cost_per_share
    stress_extra = shares * stress_extra_cost_per_share
    primary_pnl = gross - fee - primary_extra
    stress_pnl = gross - fee - stress_extra

    market_valid = batch.snapshot_valid
    market_support = (valid & support_pass).any(dim=1)
    market_range = (valid & support_pass & range_pass).any(dim=1)
    market_persistence = (valid & support_pass & range_pass & persistence_pass).any(
        dim=1
    )
    market_edge = side_signal.any(dim=1)
    status = torch.full(
        (len(batch),), 1, dtype=torch.int64, device=batch.current_mid_up.device
    )
    status[market_valid & ~market_support] = 2
    status[market_support & ~market_range] = 3
    status[market_range & ~market_persistence] = 4
    status[market_persistence & ~market_edge] = 5
    status[market_edge & signal_tie] = 6
    status[signal & ~filled] = 7
    status[filled] = 0

    def chosen(values: torch.Tensor) -> torch.Tensor:
        return values.gather(1, gather).squeeze(1)

    valid_edges = side_edge[valid.expand_as(side_edge)]
    return TerminalEvaluation(
        previous_state=chosen(previous_state),
        current_state=chosen(current_state),
        terminal_state=chosen(terminal_state),
        state_support=chosen(support),
        residual=chosen(residual),
        probability_up=probabilities[:, 0],
        forecast_probability=chosen(probabilities),
        persistence=chosen(persistence),
        signal_ask=chosen(asks),
        decision_fee_per_share=chosen(decision_fee),
        net_edge=chosen(side_edge),
        side=side,
        signal=signal,
        fill_vwap=chosen_vwap,
        maximum_executed_price=maximum_executed_price,
        fill_cost=fill_cost,
        platform_fee=fee,
        fill_shares=shares,
        primary_extra_cost=primary_extra,
        stress_extra_cost=stress_extra,
        primary_pnl=primary_pnl,
        stress_pnl=stress_pnl,
        filled=filled,
        status_code=status,
        funnel={
            "markets": len(batch),
            "valid_snapshot": int(market_valid.sum().item()),
            "state_supported": int(market_support.sum().item()),
            "ask_in_range": int(market_range.sum().item()),
            "persistence_pass": int(market_persistence.sum().item()),
            "net_edge_pass": int(market_edge.sum().item()),
            "side_ties": int(signal_tie.sum().item()),
            "signals": int(signal.sum().item()),
            "fills": int(filled.sum().item()),
            "maximum_decision_net_edge": float(valid_edges.max().item())
            if valid_edges.numel()
            else None,
        },
    )


def _decision_rows(
    source_id: str,
    fold_id: str,
    variant: TerminalVariant,
    batch: DecisionBatch,
    evaluation: TerminalEvaluation,
) -> list[dict[str, Any]]:
    cpu_batch = batch.to(torch.device("cpu"), torch.float64)
    tensors = {
        name: getattr(evaluation, name).detach().cpu()
        for name in (
            "previous_state",
            "current_state",
            "terminal_state",
            "state_support",
            "residual",
            "probability_up",
            "forecast_probability",
            "persistence",
            "signal_ask",
            "decision_fee_per_share",
            "net_edge",
            "side",
            "signal",
            "fill_vwap",
            "maximum_executed_price",
            "fill_cost",
            "platform_fee",
            "fill_shares",
            "primary_extra_cost",
            "stress_extra_cost",
            "primary_pnl",
            "stress_pnl",
            "filled",
            "status_code",
        )
    }
    rows: list[dict[str, Any]] = []
    for index, condition_id in enumerate(cpu_batch.condition_ids):
        side = int(tensors["side"][index].item())
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
            "snapshot_valid": bool(cpu_batch.snapshot_valid[index].item()),
            "side": "UP" if side == 0 else "DOWN",
            "outcome_up": float(cpu_batch.outcome_up[index].item()),
            "outcome_side": outcome_side,
            "current_mid_up": float(cpu_batch.current_mid_up[index].item()),
            "previous_mid_up": float(cpu_batch.previous_mid_up[index].item()),
        }
        for name, values in tensors.items():
            if name == "side":
                row["side_code"] = side
                continue
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
        raise TerminalMarkovError("cannot summarize empty terminal decisions")
    ordered = sorted(rows, key=lambda item: (item["decision_s"], item["condition_id"]))
    signal = torch.tensor([row["signal"] for row in ordered], dtype=torch.bool)
    filled = torch.tensor([row["filled"] for row in ordered], dtype=torch.bool)
    valid = torch.tensor([row["snapshot_valid"] for row in ordered], dtype=torch.bool)
    primary_all = torch.tensor(
        [row["primary_pnl"] for row in ordered], dtype=torch.float64
    )
    stress_all = torch.tensor(
        [row["stress_pnl"] for row in ordered], dtype=torch.float64
    )
    probability_up = torch.tensor(
        [row["probability_up"] for row in ordered], dtype=torch.float64
    )
    outcome_up = torch.tensor(
        [row["outcome_up"] for row in ordered], dtype=torch.float64
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
    valid_error = probability_up[valid] - outcome_up[valid]
    return {
        "markets": len(ordered),
        "valid_snapshots": int(valid.sum().item()),
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
        "brier_score_all_valid": float(valid_error.square().mean().item()),
        "calibration_bias_all_valid": float(valid_error.mean().item()),
    }


def _model_record(
    fold_id: str, train_markets: int, model: TerminalModel
) -> dict[str, Any]:
    absorption_sums = model.absorption_at_state_mean.sum(dim=1)
    if not torch.allclose(
        absorption_sums,
        torch.ones_like(absorption_sums),
        atol=1e-12,
        rtol=0,
    ):
        raise TerminalMarkovError("absorbing terminal rows do not sum to one")
    return {
        "fold_id": fold_id,
        "train_markets": train_markets,
        "state_policy": model.state_policy,
        "state_support": model.state_support.detach().cpu().tolist(),
        "residual_sum": model.residual_sum.detach().cpu().tolist(),
        "residual": model.residual.detach().cpu().tolist(),
        "state_mean_mid": model.state_mean_mid.detach().cpu().tolist(),
        "absorption_at_state_mean": (
            model.absorption_at_state_mean.detach().cpu().tolist()
        ),
        "transition_counts": model.transition_counts.detach().cpu().tolist(),
        "transition_matrix": model.transition_matrix.detach().cpu().tolist(),
        "persistence": model.persistence.detach().cpu().tolist(),
    }


def _run_source_variant(
    source: SourceData,
    variant: TerminalVariant,
    config: TerminalMarkovConfig,
    edges: torch.Tensor,
    device: torch.device,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows: list[dict[str, Any]] = []
    fold_metrics: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    for fold in source.folds:
        train_indices = _fold_indices(
            source.batch, fold.train_start_s, fold.train_end_exclusive_s
        )
        validation_indices = _fold_indices(
            source.batch, fold.validation_start_s, fold.validation_end_exclusive_s
        )
        if bool(
            (
                source.batch.market_start_s.index_select(0, train_indices).max()
                >= source.batch.market_start_s.index_select(0, validation_indices).min()
            ).item()
        ):
            raise TerminalMarkovError("train/validation chronology overlaps")
        train = source.batch.index(train_indices).to(device, torch.float64)
        validation = source.batch.index(validation_indices).to(device, torch.float64)
        fee_rate = source.fee_rate
        if isinstance(fee_rate, torch.Tensor):
            fee_rate = fee_rate.index_select(0, validation_indices).to(device)
        model = fit_terminal_model(
            train,
            edges,
            variant,
            transition_pseudocount=config.transition_pseudocount,
            residual_prior_strength=config.terminal_residual_prior_strength,
            maximum_absolute_residual=config.maximum_absolute_residual,
        )
        evaluation = evaluate_terminal_markov(
            validation,
            model,
            variant,
            minimum_state_support=config.minimum_state_support,
            minimum_probability=config.minimum_probability,
            maximum_probability=config.maximum_probability,
            minimum_ask=config.minimum_ask,
            maximum_ask=config.maximum_ask,
            minimum_persistence=config.minimum_persistence,
            minimum_net_edge=config.minimum_net_edge,
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
        models.append(_model_record(fold.fold_id, len(train), model))
    return all_rows, fold_metrics, models


def _render_variant(
    path: Path,
    source_id: str,
    variant: TerminalVariant,
    config: TerminalMarkovConfig,
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
            "Terminal payout forecast, ask and net edge",
            "Price-state persistence",
            "Signals and fills",
            "Cumulative PnL and drawdown",
        ),
    )
    for name, field in (
        ("terminal p_hat", "forecast_probability"),
        ("decision ask", "signal_ask"),
        ("net edge", "net_edge"),
    ):
        figure.add_trace(
            go.Scatter(
                x=times,
                y=[row[field] for row in ordered],
                name=name,
                opacity=0.55,
            ),
            row=1,
            col=1,
        )
    figure.add_hline(
        y=config.minimum_net_edge,
        line_dash="dash",
        line_color="black",
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=[row["persistence"] for row in ordered],
            name="P[current,current]",
        ),
        row=2,
        col=1,
    )
    figure.add_hline(
        y=config.minimum_persistence,
        line_dash="dash",
        line_color="firebrick",
        row=2,
        col=1,
    )
    for name, selected, symbol in (
        ("signal", signal_rows, "circle-open"),
        ("fill", fill_rows, "diamond"),
    ):
        figure.add_trace(
            go.Scatter(
                x=[row["decision_utc"] for row in selected],
                y=[row["signal_ask"] for row in selected],
                mode="markers",
                name=name,
                marker={"symbol": symbol, "size": 8},
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
            f"Stage 4g {source_id} / {variant.variant_id}: "
            f"signals {summary['signals']}, fills {summary['fills']}, "
            f"PnL {summary['net_pnl_usdc']:.2f} USDC"
        ),
        template="plotly_white",
        height=1_180,
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.02},
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def _render_transition_matrices(
    path: Path, source_id: str, records: list[dict[str, Any]]
) -> None:
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
        title=f"Stage 4g {source_id}: train-only price-transition matrices",
        template="plotly_white",
        height=330 * rows_count,
        coloraxis={"colorscale": "Blues", "cmin": 0, "cmax": 1},
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def _classification(
    results: dict[str, dict[str, dict[str, Any]]],
    config: TerminalMarkovConfig,
) -> dict[str, Any]:
    candidate = results[config.candidate_variant]
    ablation = results[config.ablation_variant]
    source_gates: dict[str, dict[str, bool]] = {}
    for source_id, record in candidate.items():
        summary = record["summary"]
        baseline = ablation[source_id]["summary"]
        required_positive = (
            config.minimum_positive_kacho_folds
            if source_id == "kacho_primary"
            else config.minimum_positive_trent_folds
        )
        source_gates[source_id] = {
            "minimum_fills": summary["fills"]
            >= config.minimum_candidate_fills_per_source,
            "positive_primary_pnl": summary["net_pnl_usdc"] > 0,
            "positive_stress_pnl": summary["stress_net_pnl_usdc"] > 0,
            "minimum_profit_factor": (
                math.inf
                if summary["profit_factor_infinite"]
                else (summary["profit_factor"] or 0)
            )
            >= config.minimum_profit_factor,
            "minimum_positive_folds": summary["positive_folds"] >= required_positive,
            "brier_not_worse_than_ablation": summary["brier_score_all_valid"]
            <= baseline["brier_score_all_valid"],
            "pnl_above_ablation": summary["net_pnl_usdc"] > baseline["net_pnl_usdc"],
        }
    total_positive = sum(
        record["summary"]["positive_folds"] for record in candidate.values()
    )
    candidate_pass = (
        all(all(gates.values()) for gates in source_gates.values())
        and total_positive >= config.minimum_positive_primary_folds
    )
    total_fills = sum(record["summary"]["fills"] for record in candidate.values())
    if total_fills == 0:
        status = "no_executable_signals"
    elif any(
        record["summary"]["fills"] < config.minimum_candidate_fills_per_source
        for record in candidate.values()
    ):
        status = "insufficient_trades"
    elif candidate_pass:
        status = "development_candidate"
    else:
        status = "rejected_on_development"
    return {
        "schema_version": 1,
        "status": status,
        "selection_performed": False,
        "holdout_opened": False,
        "candidate_variant": config.candidate_variant,
        "ablation_variant": config.ablation_variant,
        "total_positive_folds": total_positive,
        "source_gates": source_gates,
    }


def run_stage4g_terminal_markov(
    config_path: str | Path,
    *,
    data_root: str | Path = "data/historical",
    output_root: str | Path = "outputs/terminal_markov",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    source_dirty = bool(_git(["status", "--porcelain"]))
    if source_dirty:
        raise TerminalMarkovError("canonical Stage 4g run requires a clean commit")
    config = load_terminal_markov_config(config_path)
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise TerminalMarkovError("frozen CUDA device is unavailable")
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
        transition_records: list[dict[str, Any]] | None = None
        for variant in config.variants:
            rows, folds, models = _run_source_variant(
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
            _write_json(variant_dir / "models.json", models)
            _render_variant(
                variant_dir / "diagnostics.html",
                source.source_id,
                variant,
                config,
                rows,
                summary,
            )
            result_records[variant.variant_id][source.source_id] = {
                "summary": summary,
                "folds": folds,
            }
            transition_records = models
        if transition_records is None:
            raise TerminalMarkovError("no terminal variants were evaluated")
        _render_transition_matrices(
            source_dir / "transition_matrices.html",
            source.source_id,
            transition_records,
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
            "selection_performed": False,
            "holdout_opened": False,
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
