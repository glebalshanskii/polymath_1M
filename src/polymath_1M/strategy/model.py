from __future__ import annotations

from dataclasses import dataclass

import torch

from polymath_1M.domain import DecisionBatch


class ModelFitError(RuntimeError):
    """The train split cannot fit the declared empirical model."""


@dataclass(frozen=True)
class LookupModel:
    edges: torch.Tensor
    probability_up: torch.Tensor
    support: torch.Tensor
    transition_matrix: torch.Tensor
    persistence: torch.Tensor
    prior_up: torch.Tensor


@dataclass(frozen=True)
class Evaluation:
    state_bin: torch.Tensor
    probability: torch.Tensor
    support: torch.Tensor
    persistence: torch.Tensor
    side: torch.Tensor
    signal_ask: torch.Tensor
    fill_vwap: torch.Tensor
    fill_cost: torch.Tensor
    platform_fee: torch.Tensor
    net_edge: torch.Tensor
    fill_shares: torch.Tensor
    gross_pnl: torch.Tensor
    extra_cost: torch.Tensor
    net_pnl: torch.Tensor
    filled: torch.Tensor
    status_code: torch.Tensor


STATUS_NAMES = (
    "filled",
    "data_invalid",
    "insufficient_support",
    "ask_out_of_range",
    "low_persistence",
    "edge_below_threshold",
    "no_liquidity",
    "side_tie",
    "not_market_favorite",
)


def _state_bins(values: torch.Tensor, edges: torch.Tensor) -> torch.Tensor:
    return torch.bucketize(values, edges[1:-1], right=False)


def calculate_platform_fee(
    shares: torch.Tensor,
    prices: torch.Tensor,
    fee_rate: float | torch.Tensor,
    *,
    decimals: int,
) -> torch.Tensor:
    """Calculate current Polymarket taker fees per market/outcome side.

    Inputs are matched shares and prices with shape ``[N, 2, L]``. The fee is
    rounded per aggregated matched price level and accumulated over L2 levels.
    """

    if shares.shape != prices.shape or shares.ndim != 3:
        raise ValueError("fee inputs must share shape [market, side, level]")
    rates = torch.as_tensor(fee_rate, dtype=shares.dtype, device=shares.device)
    if rates.ndim == 1:
        if rates.shape[0] != shares.shape[0]:
            raise ValueError("per-market fee rate must have shape [market]")
        rates = rates[:, None, None]
    elif rates.ndim != 0:
        raise ValueError("fee rate must be scalar or have shape [market]")
    if bool((rates < 0).any().item()) or decimals < 0:
        raise ValueError("fee rate and decimals must be nonnegative")
    raw_per_level = shares * rates * prices * (1 - prices)
    scale = float(10**decimals)
    return (torch.round(raw_per_level * scale) / scale).sum(dim=2)


def fit_lookup_model(
    train: DecisionBatch,
    edges: torch.Tensor,
    *,
    terminal_alpha: float,
    transition_alpha: float,
) -> LookupModel:
    if train.current_mid_up.dtype != torch.float64 or edges.dtype != torch.float64:
        raise TypeError("lookup model requires torch.float64 tensors")
    states = _state_bins(train.current_mid_up, edges)
    previous_states = _state_bins(train.previous_mid_up, edges)
    valid = (
        train.snapshot_valid
        & torch.isfinite(train.outcome_up)
        & (train.outcome_up >= 0)
        & (train.outcome_up <= 1)
    )
    if int(valid.sum().item()) == 0:
        raise ModelFitError("train split has no valid labeled decision snapshots")
    bins = edges.numel() - 1
    valid_states = states[valid]
    valid_outcomes = train.outcome_up[valid]
    support = torch.bincount(valid_states, minlength=bins)
    wins = torch.bincount(valid_states, weights=valid_outcomes, minlength=bins).to(
        dtype=torch.float64
    )
    prior = valid_outcomes.mean()
    probability_up = (wins + terminal_alpha * prior) / (
        support.to(dtype=torch.float64) + terminal_alpha
    )
    flat_transitions = previous_states[valid] * bins + states[valid]
    transition_counts = torch.bincount(flat_transitions, minlength=bins * bins).reshape(
        bins, bins
    )
    smoothed = transition_counts.to(dtype=torch.float64) + transition_alpha
    transition_matrix = smoothed / smoothed.sum(dim=1, keepdim=True)
    return LookupModel(
        edges=edges,
        probability_up=probability_up,
        support=support,
        transition_matrix=transition_matrix,
        persistence=torch.diagonal(transition_matrix),
        prior_up=prior,
    )


def evaluate_batch(
    batch: DecisionBatch,
    model: LookupModel,
    *,
    minimum_support: int,
    minimum_persistence: float,
    minimum_ask: float,
    maximum_ask: float,
    minimum_net_edge: float,
    target_notional_usdc: float,
    platform_fee_rate: float | torch.Tensor,
    platform_fee_round_decimals: int,
    extra_cost_per_share: float,
    require_market_favorite: bool,
    forced_side: torch.Tensor | None = None,
    apply_support_gate: bool = True,
    apply_persistence_gate: bool = True,
    apply_edge_gate: bool = True,
) -> Evaluation:
    states = _state_bins(batch.current_mid_up, model.edges)
    probability_up = model.probability_up[states]
    probabilities = torch.stack((probability_up, 1 - probability_up), dim=1)
    prices = batch.ask_depth_prices
    sizes = torch.nan_to_num(batch.ask_depth_sizes, nan=0.0, posinf=0.0, neginf=0.0)
    usable = (
        torch.isfinite(prices) & (prices > 0) & (prices <= maximum_ask) & (sizes > 0)
    )
    prices = torch.where(usable, prices, torch.ones_like(prices))
    sizes = torch.where(usable, sizes, torch.zeros_like(sizes))
    level_notional = prices * sizes
    notional_before = torch.cumsum(level_notional, dim=2) - level_notional
    remaining = torch.clamp(target_notional_usdc - notional_before, min=0.0)
    taken_sizes = torch.minimum(sizes, remaining / prices)
    side_shares = taken_sizes.sum(dim=2)
    side_cost = (taken_sizes * prices).sum(dim=2)
    side_fee = calculate_platform_fee(
        taken_sizes,
        prices,
        platform_fee_rate,
        decimals=platform_fee_round_decimals,
    )
    side_vwap = torch.where(
        side_shares > 0,
        side_cost / side_shares,
        torch.full_like(side_shares, float("nan")),
    )
    side_fee_per_share = torch.where(
        side_shares > 0,
        side_fee / side_shares,
        torch.full_like(side_shares, float("nan")),
    )
    side_edges = probabilities - side_vwap - side_fee_per_share - extra_cost_per_share
    comparable_edges = torch.nan_to_num(side_edges, nan=-torch.inf)
    if forced_side is None:
        side = torch.argmax(comparable_edges, dim=1)
        tie = torch.isclose(
            comparable_edges[:, 0], comparable_edges[:, 1], atol=1e-12, rtol=0
        )
    else:
        if forced_side.shape != (len(batch),) or forced_side.dtype != torch.int64:
            raise ValueError("forced side must be int64 with shape [market]")
        if bool(((forced_side < 0) | (forced_side > 1)).any().item()):
            raise ValueError("forced side values must be 0 or 1")
        side = forced_side.to(device=comparable_edges.device)
        tie = torch.zeros(len(batch), dtype=torch.bool, device=side.device)
    gather = side.unsqueeze(1)
    chosen_probability = probabilities.gather(1, gather).squeeze(1)
    signal_ask = batch.asks.gather(1, gather).squeeze(1)
    fill_vwap = side_vwap.gather(1, gather).squeeze(1)
    chosen_shares = side_shares.gather(1, gather).squeeze(1)
    chosen_cost = side_cost.gather(1, gather).squeeze(1)
    chosen_fee = side_fee.gather(1, gather).squeeze(1)
    chosen_fee_per_share = side_fee_per_share.gather(1, gather).squeeze(1)
    net_edge = (
        chosen_probability - fill_vwap - chosen_fee_per_share - extra_cost_per_share
    )
    support = model.support[states]
    persistence = model.persistence[states]

    status = torch.zeros(len(batch), dtype=torch.int64, device=fill_vwap.device)
    eligible = batch.snapshot_valid.clone()
    status[~eligible] = 1
    failed = eligible & apply_support_gate & (support < minimum_support)
    status[failed] = 2
    eligible &= ~failed
    failed = eligible & ((signal_ask < minimum_ask) | (signal_ask > maximum_ask))
    status[failed] = 3
    eligible &= ~failed
    failed = eligible & apply_persistence_gate & (persistence < minimum_persistence)
    status[failed] = 4
    eligible &= ~failed
    failed = eligible & apply_edge_gate & (net_edge < minimum_net_edge)
    status[failed] = 5
    eligible &= ~failed
    failed = eligible & (~torch.isfinite(chosen_shares) | (chosen_shares <= 0))
    status[failed] = 6
    eligible &= ~failed
    failed = eligible & tie
    status[failed] = 7
    eligible &= ~failed
    if require_market_favorite:
        market_favorite = torch.argmax(batch.current_mid, dim=1)
        failed = eligible & (side != market_favorite)
        status[failed] = 8
        eligible &= ~failed

    fill_shares = torch.where(eligible, chosen_shares, torch.zeros_like(fill_vwap))
    fill_cost = torch.where(eligible, chosen_cost, torch.zeros_like(fill_vwap))
    platform_fee = torch.where(eligible, chosen_fee, torch.zeros_like(fill_vwap))
    filled = eligible & (fill_shares > 0)
    status[eligible & ~filled] = 6
    settlement = torch.where(side == 0, batch.outcome_up, 1 - batch.outcome_up)
    gross_pnl = torch.where(
        filled, fill_shares * settlement - fill_cost, torch.zeros_like(fill_vwap)
    )
    extra_cost = torch.where(
        filled,
        fill_shares * extra_cost_per_share,
        torch.zeros_like(fill_vwap),
    )
    net_pnl = gross_pnl - platform_fee - extra_cost
    return Evaluation(
        state_bin=states,
        probability=chosen_probability,
        support=support,
        persistence=persistence,
        side=side,
        signal_ask=signal_ask,
        fill_vwap=fill_vwap,
        fill_cost=fill_cost,
        platform_fee=platform_fee,
        net_edge=net_edge,
        fill_shares=fill_shares,
        gross_pnl=gross_pnl,
        extra_cost=extra_cost,
        net_pnl=net_pnl,
        filled=filled,
        status_code=status,
    )
