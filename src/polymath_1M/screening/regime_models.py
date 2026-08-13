from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import subprocess
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
from polymath_1M.historical.polymarket_chainlink import (
    PolymarketChainlinkSeries,
    load_polymarket_chainlink_series,
)
from polymath_1M.strategy.model import (
    Evaluation,
    LookupModel,
    evaluate_batch,
    fit_lookup_model,
)


class RegimeModelError(RuntimeError):
    """Stage 4e inputs or model results violate the frozen contract."""


@dataclass(frozen=True)
class Fold:
    fold_id: str
    train_start_s: int
    train_end_exclusive_s: int
    validation_start_s: int
    validation_end_exclusive_s: int


@dataclass(frozen=True)
class RegimeConfig:
    experiment_id: str
    dataset_config: str
    dataset_root: str
    chainlink_config: str
    strategy_config: str
    period_start_s: int
    development_end_exclusive_s: int
    holdout_end_exclusive_s: int
    folds: tuple[Fold, ...]
    variants: tuple[str, ...]
    coarse_price_bin_width: float
    fine_price_bin_width: float
    terminal_alpha: float
    transition_alpha: float
    decay_half_life_days: float
    logistic_ridge: float
    logistic_max_iterations: int
    minimum_probability: float
    maximum_probability: float
    minimum_support: int
    minimum_ask: float
    maximum_ask: float
    minimum_net_edge: float
    minimum_persistence: float
    target_notional_usdc: float
    platform_fee_rate: float
    platform_fee_round_decimals: int
    extra_cost_per_share: float
    minimum_relative_fills: float
    minimum_positive_folds: int
    minimum_profit_factor: float
    maximum_drawdown_ratio_to_baseline: float
    bad_regime_start_s: int
    bad_regime_end_exclusive_s: int
    seed: int
    device: str
    dtype: str
    config_sha256: str


@dataclass(frozen=True)
class LogisticModel:
    mean: torch.Tensor
    scale: torch.Tensor
    coefficients: torch.Tensor
    iterations: int


@dataclass(frozen=True)
class VariantResult:
    variant: str
    rows: list[dict[str, Any]]
    fold_metrics: list[dict[str, Any]]
    summary: dict[str, Any]


VARIANTS = (
    "m0_coarse_lookup_10c",
    "m1_fine_lookup_2c",
    "m2_continuous_price",
    "m3_continuous_price_decay",
    "m4_microstructure_decay",
    "m5_chainlink_regime_decay_timestamped_invalid",
    "m6_chainlink_regime_decay_lagged",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
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


def _git(arguments: list[str]) -> str | None:
    try:
        return subprocess.run(
            ["git", *arguments], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _timestamp(value: Any, name: str) -> int:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise RegimeModelError(f"{name} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise RegimeModelError(f"{name} must include timezone")
    return int(parsed.astimezone(UTC).timestamp())


def load_regime_config(path: str | Path) -> RegimeConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "experiment_id",
        "dataset_config",
        "dataset_root",
        "chainlink_config",
        "strategy_config",
        "period_start",
        "development_end_exclusive",
        "holdout_end_exclusive",
        "folds",
        "variants",
        "coarse_price_bin_width",
        "fine_price_bin_width",
        "terminal_alpha",
        "transition_alpha",
        "decay_half_life_days",
        "logistic_ridge",
        "logistic_max_iterations",
        "minimum_probability",
        "maximum_probability",
        "minimum_support",
        "minimum_ask",
        "maximum_ask",
        "minimum_net_edge",
        "minimum_persistence",
        "target_notional_usdc",
        "platform_fee_rate",
        "platform_fee_round_decimals",
        "extra_cost_per_share",
        "minimum_relative_fills",
        "minimum_positive_folds",
        "minimum_profit_factor",
        "maximum_drawdown_ratio_to_baseline",
        "bad_regime_start",
        "bad_regime_end_exclusive",
        "seed",
        "device",
        "dtype",
    }
    if payload.keys() != expected:
        raise RegimeModelError(
            f"Stage 4e config fields differ: missing={sorted(expected - payload.keys())}, "
            f"extra={sorted(payload.keys() - expected)}"
        )
    if payload["schema_version"] != 1 or tuple(payload["variants"]) != VARIANTS:
        raise RegimeModelError("Stage 4e schema or ordered model sequence differs")
    period_start_s = _timestamp(payload["period_start"], "period_start")
    development_end_s = _timestamp(
        payload["development_end_exclusive"], "development_end_exclusive"
    )
    holdout_end_s = _timestamp(
        payload["holdout_end_exclusive"], "holdout_end_exclusive"
    )
    folds: list[Fold] = []
    fold_fields = {
        "fold_id",
        "train_start",
        "train_end_exclusive",
        "validation_start",
        "validation_end_exclusive",
    }
    for raw in payload["folds"]:
        if raw.keys() != fold_fields:
            raise RegimeModelError("Stage 4e fold fields differ")
        fold = Fold(
            fold_id=str(raw["fold_id"]),
            train_start_s=_timestamp(raw["train_start"], "train_start"),
            train_end_exclusive_s=_timestamp(
                raw["train_end_exclusive"], "train_end_exclusive"
            ),
            validation_start_s=_timestamp(raw["validation_start"], "validation_start"),
            validation_end_exclusive_s=_timestamp(
                raw["validation_end_exclusive"], "validation_end_exclusive"
            ),
        )
        if (
            fold.train_start_s != period_start_s
            or fold.train_end_exclusive_s != fold.validation_start_s
            or fold.validation_start_s >= fold.validation_end_exclusive_s
        ):
            raise RegimeModelError("Stage 4e fold chronology differs")
        folds.append(fold)
    if (
        len(folds) != 6
        or any(
            left.validation_end_exclusive_s != right.validation_start_s
            for left, right in zip(folds[:-1], folds[1:], strict=True)
        )
        or folds[-1].validation_end_exclusive_s != development_end_s
        or not period_start_s < development_end_s < holdout_end_s
    ):
        raise RegimeModelError("Stage 4e development/holdout boundaries differ")
    numeric = {
        key: float(payload[key])
        for key in (
            "coarse_price_bin_width",
            "fine_price_bin_width",
            "terminal_alpha",
            "transition_alpha",
            "decay_half_life_days",
            "logistic_ridge",
            "minimum_probability",
            "maximum_probability",
            "minimum_ask",
            "maximum_ask",
            "minimum_net_edge",
            "minimum_persistence",
            "target_notional_usdc",
            "platform_fee_rate",
            "extra_cost_per_share",
            "minimum_relative_fills",
            "minimum_profit_factor",
            "maximum_drawdown_ratio_to_baseline",
        )
    }
    if (
        numeric["coarse_price_bin_width"] != 0.10
        or numeric["fine_price_bin_width"] != 0.02
        or not 0 < numeric["minimum_probability"] < numeric["maximum_probability"] < 1
        or not 0 < numeric["minimum_ask"] < numeric["maximum_ask"] <= 1
        or numeric["target_notional_usdc"] <= 0
        or payload["device"] != "cuda"
        or payload["dtype"] != "float64"
    ):
        raise RegimeModelError("Stage 4e frozen numerical contract differs")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return RegimeConfig(
        experiment_id=str(payload["experiment_id"]),
        dataset_config=str(payload["dataset_config"]),
        dataset_root=str(payload["dataset_root"]),
        chainlink_config=str(payload["chainlink_config"]),
        strategy_config=str(payload["strategy_config"]),
        period_start_s=period_start_s,
        development_end_exclusive_s=development_end_s,
        holdout_end_exclusive_s=holdout_end_s,
        folds=tuple(folds),
        variants=tuple(payload["variants"]),
        coarse_price_bin_width=numeric["coarse_price_bin_width"],
        fine_price_bin_width=numeric["fine_price_bin_width"],
        terminal_alpha=numeric["terminal_alpha"],
        transition_alpha=numeric["transition_alpha"],
        decay_half_life_days=numeric["decay_half_life_days"],
        logistic_ridge=numeric["logistic_ridge"],
        logistic_max_iterations=int(payload["logistic_max_iterations"]),
        minimum_probability=numeric["minimum_probability"],
        maximum_probability=numeric["maximum_probability"],
        minimum_support=int(payload["minimum_support"]),
        minimum_ask=numeric["minimum_ask"],
        maximum_ask=numeric["maximum_ask"],
        minimum_net_edge=numeric["minimum_net_edge"],
        minimum_persistence=numeric["minimum_persistence"],
        target_notional_usdc=numeric["target_notional_usdc"],
        platform_fee_rate=numeric["platform_fee_rate"],
        platform_fee_round_decimals=int(payload["platform_fee_round_decimals"]),
        extra_cost_per_share=numeric["extra_cost_per_share"],
        minimum_relative_fills=numeric["minimum_relative_fills"],
        minimum_positive_folds=int(payload["minimum_positive_folds"]),
        minimum_profit_factor=numeric["minimum_profit_factor"],
        maximum_drawdown_ratio_to_baseline=numeric[
            "maximum_drawdown_ratio_to_baseline"
        ],
        bad_regime_start_s=_timestamp(payload["bad_regime_start"], "bad_regime_start"),
        bad_regime_end_exclusive_s=_timestamp(
            payload["bad_regime_end_exclusive"], "bad_regime_end_exclusive"
        ),
        seed=int(payload["seed"]),
        device=str(payload["device"]),
        dtype=str(payload["dtype"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _uniform_edges(width: float, device: torch.device) -> torch.Tensor:
    bins = round(1.0 / width)
    if not math.isclose(bins * width, 1.0, abs_tol=1e-12):
        raise RegimeModelError("price-bin width must divide one exactly")
    edges = torch.linspace(0, 1, bins + 1, dtype=torch.float64, device=device)
    edges[-1] = 1.000001
    return edges


def _indices(batch: DecisionBatch, start_s: int, end_s: int) -> torch.Tensor:
    result = torch.nonzero(
        (batch.market_start_s >= start_s) & (batch.market_start_s < end_s),
        as_tuple=False,
    ).flatten()
    if result.numel() == 0:
        raise RegimeModelError(f"empty market window: {start_s} to {end_s}")
    return result


def _match_exact(
    series: PolymarketChainlinkSeries, timestamps: torch.Tensor
) -> torch.Tensor:
    positions = torch.searchsorted(series.timestamp_s, timestamps.cpu())
    in_range = positions < len(series)
    safe = torch.clamp(positions, max=max(len(series) - 1, 0))
    if len(series) == 0 or not bool(
        (in_range & (series.timestamp_s[safe] == timestamps.cpu())).all().item()
    ):
        raise RegimeModelError("Chainlink feature timestamp is not minute-complete")
    return series.value[safe]


def chainlink_features(
    batch: DecisionBatch,
    series: PolymarketChainlinkSeries,
    *,
    causal_lag_seconds: int = 0,
) -> torch.Tensor:
    if causal_lag_seconds < 0 or causal_lag_seconds % 60:
        raise RegimeModelError("Chainlink causal lag must be whole nonnegative minutes")
    offsets = torch.arange(5, dtype=torch.int64) * 60 - causal_lag_seconds
    path_times = batch.market_start_s[:, None].cpu() + offsets[None, :]
    path = _match_exact(series, path_times.reshape(-1)).reshape(len(batch), 5)
    log_path = torch.log(path)
    minute_returns = log_path[:, 1:] - log_path[:, :-1]
    return torch.stack(
        (
            log_path[:, -1] - log_path[:, 0],
            minute_returns[:, -1],
            torch.sqrt(torch.mean(minute_returns.square(), dim=1)),
        ),
        dim=1,
    )


def common_features(
    batch: DecisionBatch,
    chainlink_timestamped: torch.Tensor,
    chainlink_lagged: torch.Tensor,
) -> tuple[torch.Tensor, tuple[str, ...]]:
    clipped = torch.clamp(batch.current_mid_up, 1e-6, 1 - 1e-6)
    logit_mid = torch.log(clipped / (1 - clipped))
    token = torch.stack(
        (
            logit_mid,
            batch.current_mid_up - batch.previous_mid_up,
            batch.asks[:, 0] - batch.bids[:, 0],
            batch.asks[:, 1] - batch.bids[:, 1],
            batch.current_mid.sum(dim=1) - 1,
        ),
        dim=1,
    )
    return (
        torch.cat(
            (
                token,
                chainlink_timestamped.to(token.device),
                chainlink_lagged.to(token.device),
            ),
            dim=1,
        ),
        (
            "logit_mid_up",
            "mid_change_1m",
            "spread_up",
            "spread_down",
            "complement_dislocation",
            "chainlink_return_market",
            "chainlink_return_1m",
            "chainlink_realized_vol",
            "chainlink_lagged_return_market",
            "chainlink_lagged_return_1m",
            "chainlink_lagged_realized_vol",
        ),
    )


def _feature_columns(variant: str) -> tuple[int, ...]:
    if variant in {"m2_continuous_price", "m3_continuous_price_decay"}:
        return (0,)
    if variant == "m4_microstructure_decay":
        return tuple(range(5))
    if variant == "m5_chainlink_regime_decay_timestamped_invalid":
        return tuple(range(8))
    if variant == "m6_chainlink_regime_decay_lagged":
        return (*range(5), 8, 9, 10)
    raise RegimeModelError(f"{variant} does not use logistic features")


def fit_logistic_model(
    features: torch.Tensor,
    outcomes: torch.Tensor,
    weights: torch.Tensor,
    *,
    ridge: float,
    maximum_iterations: int,
) -> LogisticModel:
    if (
        features.ndim != 2
        or outcomes.shape != (features.shape[0],)
        or weights.shape != outcomes.shape
        or features.dtype != torch.float64
        or outcomes.dtype != torch.float64
        or weights.dtype != torch.float64
    ):
        raise RegimeModelError("logistic tensors violate shape/dtype contract")
    valid = (
        torch.isfinite(features).all(dim=1)
        & torch.isfinite(outcomes)
        & (outcomes >= 0)
        & (outcomes <= 1)
        & torch.isfinite(weights)
        & (weights > 0)
    )
    if int(valid.sum().item()) <= features.shape[1] + 2:
        raise RegimeModelError("too few valid rows for logistic model")
    x = features[valid]
    y = outcomes[valid]
    w = weights[valid]
    w = w / w.mean()
    mean = (w[:, None] * x).sum(dim=0) / w.sum()
    variance = (w[:, None] * (x - mean).square()).sum(dim=0) / w.sum()
    scale = torch.sqrt(torch.clamp(variance, min=1e-12))
    standardized = (x - mean) / scale
    design = torch.cat(
        (
            torch.ones((x.shape[0], 1), dtype=x.dtype, device=x.device),
            standardized,
        ),
        dim=1,
    )
    coefficients = torch.zeros(design.shape[1], dtype=x.dtype, device=x.device)
    prior = torch.clamp((w * y).sum() / w.sum(), 1e-6, 1 - 1e-6)
    coefficients[0] = torch.log(prior / (1 - prior))
    penalty = torch.eye(design.shape[1], dtype=x.dtype, device=x.device) * ridge
    penalty[0, 0] = 0
    used_iterations = maximum_iterations
    for iteration in range(maximum_iterations):
        probability = torch.sigmoid(design @ coefficients)
        curvature = w * probability * (1 - probability)
        hessian = design.T @ (curvature[:, None] * design) + penalty
        gradient = design.T @ (w * (y - probability)) - penalty @ coefficients
        update = torch.linalg.solve(hessian, gradient)
        coefficients = coefficients + update
        coefficients[1] = torch.clamp(coefficients[1], min=0.0)
        if float(torch.max(torch.abs(update)).item()) < 1e-10:
            used_iterations = iteration + 1
            break
    if not bool(torch.isfinite(coefficients).all().item()):
        raise RegimeModelError("logistic fit produced non-finite coefficients")
    return LogisticModel(
        mean=mean,
        scale=scale,
        coefficients=coefficients,
        iterations=used_iterations,
    )


def predict_logistic(model: LogisticModel, features: torch.Tensor) -> torch.Tensor:
    standardized = (features - model.mean) / model.scale
    design = torch.cat(
        (
            torch.ones(
                (features.shape[0], 1), dtype=features.dtype, device=features.device
            ),
            standardized,
        ),
        dim=1,
    )
    return torch.sigmoid(design @ model.coefficients)


def _recency_weights(
    train: DecisionBatch, end_s: int, half_life_days: float
) -> torch.Tensor:
    age_days = (end_s - train.market_start_s).to(torch.float64) / 86_400
    return torch.exp2(-age_days / half_life_days)


def _evaluate(
    batch: DecisionBatch,
    coarse_model: LookupModel,
    probability_up: torch.Tensor | None,
    config: RegimeConfig,
) -> Evaluation:
    return evaluate_batch(
        batch,
        coarse_model,
        minimum_support=config.minimum_support,
        minimum_persistence=config.minimum_persistence,
        minimum_ask=config.minimum_ask,
        maximum_ask=config.maximum_ask,
        minimum_net_edge=config.minimum_net_edge,
        target_notional_usdc=config.target_notional_usdc,
        platform_fee_rate=config.platform_fee_rate,
        platform_fee_round_decimals=config.platform_fee_round_decimals,
        extra_cost_per_share=config.extra_cost_per_share,
        require_market_favorite=False,
        probability_up_override=probability_up,
    )


def _diagnostic_rows(
    variant: str,
    fold_id: str,
    batch: DecisionBatch,
    result: Evaluation,
    features: torch.Tensor,
    feature_names: tuple[str, ...],
) -> list[dict[str, Any]]:
    cpu_batch = batch.to(torch.device("cpu"), torch.float64)
    tensors = {
        "state_bin": result.state_bin.detach().cpu(),
        "forecast_probability": result.probability.detach().cpu(),
        "support": result.support.detach().cpu(),
        "persistence": result.persistence.detach().cpu(),
        "side_code": result.side.detach().cpu(),
        "signal_ask": result.signal_ask.detach().cpu(),
        "fill_vwap": result.fill_vwap.detach().cpu(),
        "fill_cost": result.fill_cost.detach().cpu(),
        "platform_fee": result.platform_fee.detach().cpu(),
        "net_edge": result.net_edge.detach().cpu(),
        "fill_shares": result.fill_shares.detach().cpu(),
        "extra_cost": result.extra_cost.detach().cpu(),
        "net_pnl": result.net_pnl.detach().cpu(),
        "filled": result.filled.detach().cpu(),
        "status_code": result.status_code.detach().cpu(),
    }
    common = features.detach().cpu()
    rows: list[dict[str, Any]] = []
    for index, condition_id in enumerate(cpu_batch.condition_ids):
        side = int(tensors["side_code"][index].item())
        outcome_side = (
            float(cpu_batch.outcome_up[index].item())
            if side == 0
            else float(1 - cpu_batch.outcome_up[index].item())
        )
        row: dict[str, Any] = {
            "variant": variant,
            "fold_id": fold_id,
            "condition_id": condition_id,
            "market_start_utc": datetime.fromtimestamp(
                int(cpu_batch.market_start_s[index].item()), tz=UTC
            ).isoformat(),
            "market_start_s": int(cpu_batch.market_start_s[index].item()),
            "decision_utc": datetime.fromtimestamp(
                int(cpu_batch.decision_s[index].item()), tz=UTC
            ).isoformat(),
            "decision_s": int(cpu_batch.decision_s[index].item()),
            "outcome_up": float(cpu_batch.outcome_up[index].item()),
            "side": "UP" if side == 0 else "DOWN",
            "outcome_side": outcome_side,
            "current_mid_up": float(cpu_batch.current_mid_up[index].item()),
            "previous_mid_up": float(cpu_batch.previous_mid_up[index].item()),
        }
        for name, values in tensors.items():
            value = values[index].item()
            row[name] = bool(value) if name == "filled" else value
        row["stress_2c_net_pnl"] = float(row["net_pnl"] - row["fill_shares"] * 0.01)
        for column, name in enumerate(feature_names):
            row[name] = float(common[index, column].item())
        rows.append(row)
    return rows


def _rolling_sum(
    values: torch.Tensor, timestamps: torch.Tensor, seconds: int
) -> torch.Tensor:
    cumulative = torch.cumsum(values, dim=0)
    left = torch.searchsorted(timestamps, timestamps - seconds, right=False)
    before = torch.where(
        left > 0,
        cumulative[torch.clamp(left - 1, min=0)],
        torch.zeros_like(cumulative),
    )
    return cumulative - before


def _longest_streak(values: torch.Tensor) -> int:
    if values.numel() == 0:
        return 0
    changes = torch.ones(values.numel(), dtype=torch.bool)
    changes[1:] = values[1:] != values[:-1]
    starts = torch.nonzero(changes, as_tuple=False).flatten()
    ends = torch.cat((starts[1:], torch.tensor([values.numel()])))
    return int((ends - starts).max().item())


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise RegimeModelError("cannot summarize empty rows")
    rows.sort(key=lambda row: (row["decision_s"], row["condition_id"]))
    pnl_all = torch.tensor([row["net_pnl"] for row in rows], dtype=torch.float64)
    timestamps_all = torch.tensor(
        [row["decision_s"] for row in rows], dtype=torch.int64
    )
    filled_mask = torch.tensor([row["filled"] for row in rows], dtype=torch.bool)
    filled_indices = torch.nonzero(filled_mask, as_tuple=False).flatten()
    pnl = pnl_all[filled_mask]
    fills = int(pnl.numel())
    cumulative = torch.cumsum(pnl_all, dim=0)
    peaks = torch.cummax(
        torch.cat((torch.zeros(1, dtype=torch.float64), cumulative)), dim=0
    ).values[1:]
    drawdown = peaks - cumulative
    max_drawdown = float(drawdown.max().item())
    rolling_24h = _rolling_sum(pnl_all, timestamps_all, 86_400)
    gross_profit = float(pnl[pnl > 0].sum().item())
    gross_loss = float(-pnl[pnl < 0].sum().item())
    probability = torch.tensor(
        [rows[index]["forecast_probability"] for index in filled_indices.tolist()],
        dtype=torch.float64,
    )
    outcome = torch.tensor(
        [rows[index]["outcome_side"] for index in filled_indices.tolist()],
        dtype=torch.float64,
    )
    side_up = torch.tensor(
        [rows[index]["side"] == "UP" for index in filled_indices.tolist()],
        dtype=torch.bool,
    )
    rolling_concentration = 0.0
    if fills:
        encoded = side_up.to(torch.float64)
        if fills >= 20:
            up_share = encoded.unfold(0, 20, 1).mean(dim=1)
            rolling_concentration = float(
                torch.maximum(up_share, 1 - up_share).max().item()
            )
        else:
            rolling_concentration = float(
                torch.maximum(encoded.mean(), 1 - encoded.mean()).item()
            )
    total_cost = sum(
        row["fill_cost"] + row["platform_fee"] + row["extra_cost"]
        for row in rows
        if row["filled"]
    )
    recovery_seconds: int | None = None
    if max_drawdown > 0:
        trough_index = int(torch.argmax(drawdown).item())
        prior_peak = float(peaks[trough_index].item())
        recovered = torch.nonzero(
            cumulative[trough_index + 1 :] >= prior_peak, as_tuple=False
        ).flatten()
        if recovered.numel():
            recovery_index = trough_index + 1 + int(recovered[0].item())
            recovery_seconds = int(
                timestamps_all[recovery_index].item()
                - timestamps_all[trough_index].item()
            )
    duration_days = max(
        float((timestamps_all[-1] - timestamps_all[0]).item()) / 86_400,
        1 / 288,
    )
    return {
        "markets": len(rows),
        "fills": fills,
        "fills_per_day": fills / duration_days,
        "cash_turnover_usdc": total_cost,
        "net_pnl_usdc": float(pnl.sum().item()),
        "return_on_turnover": float(pnl.sum().item()) / total_cost
        if total_cost
        else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "profit_factor_infinite": bool(gross_profit > 0 and gross_loss == 0),
        "win_rate": float((outcome > 0.5).to(torch.float64).mean().item())
        if fills
        else None,
        "max_drawdown_usdc": max_drawdown,
        "worst_rolling_24h_pnl_usdc": float(rolling_24h.min().item()),
        "recovery_seconds": recovery_seconds,
        "brier_score": float((probability - outcome).square().mean().item())
        if fills
        else None,
        "log_loss": float(
            -(
                outcome * torch.log(torch.clamp(probability, 1e-12, 1 - 1e-12))
                + (1 - outcome)
                * torch.log(torch.clamp(1 - probability, 1e-12, 1 - 1e-12))
            )
            .mean()
            .item()
        )
        if fills
        else None,
        "calibration_gap": float((probability - outcome).mean().item())
        if fills
        else None,
        "up_share": float(side_up.to(torch.float64).mean().item()) if fills else None,
        "longest_same_side_streak": _longest_streak(side_up) if fills else 0,
        "maximum_side_share_rolling_20": rolling_concentration if fills else None,
        "stress_2c_net_pnl_usdc": sum(row["stress_2c_net_pnl"] for row in rows),
    }


def _augment_summary(
    rows: list[dict[str, Any]], folds: list[dict[str, Any]], config: RegimeConfig
) -> dict[str, Any]:
    summary = _metrics(rows)
    summary["positive_folds"] = sum(item["net_pnl_usdc"] > 0 for item in folds)
    bad = [
        row
        for row in rows
        if config.bad_regime_start_s
        <= row["decision_s"]
        < config.bad_regime_end_exclusive_s
    ]
    summary["bad_regime"] = _metrics(bad) if bad else None
    return summary


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise RegimeModelError(f"cannot write empty CSV: {path}")
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _rolling_mean(values: torch.Tensor, window: int) -> torch.Tensor:
    if values.numel() < window:
        return torch.full_like(values, float("nan"))
    result = torch.full_like(values, float("nan"))
    result[window - 1 :] = values.unfold(0, window, 1).mean(dim=1)
    return result


def _render_variant(path: Path, result: VariantResult, config: RegimeConfig) -> None:
    filled = [row for row in result.rows if row["filled"]]
    times = [row["decision_utc"] for row in filled]
    pnl = torch.tensor([row["net_pnl"] for row in filled], dtype=torch.float64)
    cumulative = torch.cumsum(pnl, dim=0)
    peaks = torch.cummax(
        torch.cat((torch.zeros(1, dtype=torch.float64), cumulative)), dim=0
    ).values[1:]
    timestamps = torch.tensor([row["decision_s"] for row in filled], dtype=torch.int64)
    rolling_pnl = _rolling_sum(pnl, timestamps, 86_400)
    forecast = torch.tensor(
        [row["forecast_probability"] for row in filled], dtype=torch.float64
    )
    realized = torch.tensor(
        [row["outcome_side"] for row in filled], dtype=torch.float64
    )
    side_up = torch.tensor([row["side"] == "UP" for row in filled], dtype=torch.float64)
    chainlink_column = (
        "chainlink_lagged_return_market"
        if result.variant == "m6_chainlink_regime_decay_lagged"
        else "chainlink_return_market"
    )
    chainlink_return = torch.tensor(
        [row[chainlink_column] for row in filled], dtype=torch.float64
    )
    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.055,
        specs=[[{"secondary_y": True}], [{}], [{}], [{"secondary_y": True}]],
        subplot_titles=(
            "Net PnL и drawdown",
            "Rolling 24h PnL",
            "Rolling 20: forecast и realized payout",
            "Rolling 20 UP share и Chainlink return",
        ),
    )
    figure.add_trace(
        go.Scatter(x=times, y=cumulative.tolist(), name="Cumulative PnL"),
        row=1,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=(peaks - cumulative).tolist(),
            name="Drawdown",
            line={"color": "firebrick"},
            fill="tozeroy",
        ),
        row=1,
        col=1,
        secondary_y=True,
    )
    figure.add_trace(
        go.Scatter(x=times, y=rolling_pnl.tolist(), name="24h PnL"), row=2, col=1
    )
    figure.add_hline(y=0, line_width=1, line_color="gray", row=2, col=1)
    figure.add_trace(
        go.Scatter(
            x=times,
            y=_rolling_mean(forecast, 20).tolist(),
            name="Forecast (20)",
        ),
        row=3,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=_rolling_mean(realized, 20).tolist(),
            name="Realized (20)",
        ),
        row=3,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=_rolling_mean(side_up, 20).tolist(),
            name="UP share (20)",
        ),
        row=4,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=times,
            y=(chainlink_return * 100).tolist(),
            name="Chainlink market return, %",
            opacity=0.45,
        ),
        row=4,
        col=1,
        secondary_y=True,
    )
    figure.add_vrect(
        x0=datetime.fromtimestamp(config.bad_regime_start_s, tz=UTC),
        x1=datetime.fromtimestamp(config.bad_regime_end_exclusive_s, tz=UTC),
        fillcolor="red",
        opacity=0.08,
        line_width=0,
        row="all",
        col=1,
    )
    figure.update_layout(
        title=(
            f"Stage 4e {result.variant}: PnL и диагностика | "
            f"PnL {result.summary['net_pnl_usdc']:.2f} USDC, "
            f"fills {result.summary['fills']}, DD {result.summary['max_drawdown_usdc']:.2f}"
        ),
        template="plotly_white",
        height=1_180,
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.02},
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def _render_comparison(path: Path, results: list[VariantResult]) -> None:
    figure = make_subplots(
        rows=2,
        cols=1,
        vertical_spacing=0.13,
        subplot_titles=("Walk-forward cumulative net PnL", "Risk/return summary"),
    )
    for result in results:
        filled = [row for row in result.rows if row["filled"]]
        pnl = torch.tensor([row["net_pnl"] for row in filled], dtype=torch.float64)
        figure.add_trace(
            go.Scatter(
                x=[row["decision_utc"] for row in filled],
                y=torch.cumsum(pnl, dim=0).tolist(),
                name=result.variant,
            ),
            row=1,
            col=1,
        )
    names = [item.variant for item in results]
    figure.add_trace(
        go.Bar(
            x=names,
            y=[item.summary["net_pnl_usdc"] for item in results],
            name="Net PnL",
        ),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Bar(
            x=names,
            y=[-item.summary["max_drawdown_usdc"] for item in results],
            name="-Max drawdown",
        ),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Bar(
            x=names,
            y=[item.summary["worst_rolling_24h_pnl_usdc"] for item in results],
            name="Worst 24h PnL",
        ),
        row=2,
        col=1,
    )
    figure.update_layout(
        title="Stage 4e sequential model comparison",
        template="plotly_white",
        height=850,
        barmode="group",
        hovermode="x unified",
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def _run_variant(
    variant: str,
    config: RegimeConfig,
    full_batch: DecisionBatch,
    full_features: torch.Tensor,
    feature_names: tuple[str, ...],
    device: torch.device,
) -> VariantResult:
    coarse_edges = _uniform_edges(config.coarse_price_bin_width, device)
    fine_edges = _uniform_edges(config.fine_price_bin_width, device)
    rows: list[dict[str, Any]] = []
    fold_metrics: list[dict[str, Any]] = []
    model_records: list[dict[str, Any]] = []
    for fold in config.folds:
        train_indices = _indices(
            full_batch, fold.train_start_s, fold.train_end_exclusive_s
        )
        validation_indices = _indices(
            full_batch,
            fold.validation_start_s,
            fold.validation_end_exclusive_s,
        )
        train = full_batch.index(train_indices).to(device, torch.float64)
        validation = full_batch.index(validation_indices).to(device, torch.float64)
        train_features = full_features.index_select(0, train_indices).to(device)
        validation_features = full_features.index_select(0, validation_indices).to(
            device
        )
        coarse = fit_lookup_model(
            train,
            coarse_edges,
            terminal_alpha=config.terminal_alpha,
            transition_alpha=config.transition_alpha,
        )
        probability_up: torch.Tensor | None = None
        model_record: dict[str, Any] = {"fold_id": fold.fold_id}
        if variant == "m1_fine_lookup_2c":
            fine = fit_lookup_model(
                train,
                fine_edges,
                terminal_alpha=config.terminal_alpha,
                transition_alpha=config.transition_alpha,
            )
            states = torch.bucketize(
                validation.current_mid_up, fine.edges[1:-1], right=False
            )
            probability_up = fine.probability_up[states]
            model_record["terminal_bins"] = int(fine.probability_up.numel())
        elif variant not in {"m0_coarse_lookup_10c", "m1_fine_lookup_2c"}:
            columns = _feature_columns(variant)
            column_index = torch.tensor(columns, dtype=torch.int64, device=device)
            weights = torch.ones(len(train), dtype=torch.float64, device=device)
            if variant != "m2_continuous_price":
                weights = _recency_weights(
                    train, fold.train_end_exclusive_s, config.decay_half_life_days
                )
            logistic = fit_logistic_model(
                train_features.index_select(1, column_index),
                train.outcome_up,
                weights,
                ridge=config.logistic_ridge,
                maximum_iterations=config.logistic_max_iterations,
            )
            probability_up = torch.clamp(
                predict_logistic(
                    logistic, validation_features.index_select(1, column_index)
                ),
                config.minimum_probability,
                config.maximum_probability,
            )
            model_record.update(
                {
                    "feature_names": [feature_names[index] for index in columns],
                    "mean": logistic.mean.detach().cpu().tolist(),
                    "scale": logistic.scale.detach().cpu().tolist(),
                    "coefficients": logistic.coefficients.detach().cpu().tolist(),
                    "iterations": logistic.iterations,
                }
            )
        result = _evaluate(validation, coarse, probability_up, config)
        fold_rows = _diagnostic_rows(
            variant,
            fold.fold_id,
            validation,
            result,
            validation_features,
            feature_names,
        )
        metrics = _metrics(fold_rows)
        metrics["fold_id"] = fold.fold_id
        metrics["train_markets"] = len(train)
        fold_metrics.append(metrics)
        rows.extend(fold_rows)
        model_records.append(model_record)
    summary = _augment_summary(rows, fold_metrics, config)
    summary["variant"] = variant
    summary["models"] = model_records
    return VariantResult(
        variant=variant, rows=rows, fold_metrics=fold_metrics, summary=summary
    )


def _selection(results: list[VariantResult], config: RegimeConfig) -> dict[str, Any]:
    baseline = results[0].summary
    candidates: list[dict[str, Any]] = []
    for result in results[1:]:
        summary = result.summary
        timing_valid = result.variant != "m5_chainlink_regime_decay_timestamped_invalid"
        gates = {
            "causal_timing_valid": timing_valid,
            "relative_fills": summary["fills"]
            >= config.minimum_relative_fills * baseline["fills"],
            "positive_folds": summary["positive_folds"]
            >= config.minimum_positive_folds,
            "profit_factor": (
                math.inf
                if summary["profit_factor_infinite"]
                else (summary["profit_factor"] or 0)
            )
            >= config.minimum_profit_factor,
            "higher_pnl": summary["net_pnl_usdc"] > baseline["net_pnl_usdc"],
            "lower_drawdown": summary["max_drawdown_usdc"]
            <= config.maximum_drawdown_ratio_to_baseline
            * baseline["max_drawdown_usdc"],
            "better_worst_24h": summary["worst_rolling_24h_pnl_usdc"]
            > baseline["worst_rolling_24h_pnl_usdc"],
            "better_bad_regime": summary["bad_regime"]["net_pnl_usdc"]
            > baseline["bad_regime"]["net_pnl_usdc"],
        }
        candidates.append(
            {
                "variant": result.variant,
                "eligible": all(gates.values()),
                "gates": gates,
                "summary": summary,
            }
        )
    eligible = [item for item in candidates if item["eligible"]]
    order = {variant: index for index, variant in enumerate(config.variants)}
    eligible.sort(
        key=lambda item: (
            item["summary"]["max_drawdown_usdc"],
            -item["summary"]["worst_rolling_24h_pnl_usdc"],
            -item["summary"]["net_pnl_usdc"],
            order[item["variant"]],
        )
    )
    return {
        "status": "selected" if eligible else "no_improved_candidate",
        "baseline": results[0].variant,
        "selected_variant": eligible[0]["variant"] if eligible else None,
        "candidates": candidates,
        "holdout_opened": False,
    }


def run_stage4e_development(
    config_path: str | Path,
    data_root: str | Path = "data/historical",
    output_root: str | Path = "outputs/regime_models",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    config = load_regime_config(config_path)
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RegimeModelError("frozen CUDA device is unavailable")
    torch.manual_seed(config.seed)
    dataset_config = load_kacho_dataset_config(config.dataset_config)
    batch = load_kacho_decision_batch(
        dataset_config,
        data_root,
        assets=("BTC",),
        max_markets=0,
        decision_seconds_before_end=60,
        transition_horizon_seconds=60,
        label_policy="kacho_inferred_development_only",
        execution_latency_seconds=1,
        period_start_s=config.period_start_s,
        period_end_exclusive_s=config.development_end_exclusive_s,
    )
    order = torch.argsort(batch.market_start_s, stable=True)
    batch = batch.index(order)
    if int(batch.market_start_s.max().item()) >= config.development_end_exclusive_s:
        raise RegimeModelError("development loader opened holdout rows")
    chainlink, chainlink_provenance = load_polymarket_chainlink_series(
        config.chainlink_config, data_root
    )
    chainlink_timestamped = chainlink_features(batch, chainlink)
    chainlink_lagged = chainlink_features(batch, chainlink, causal_lag_seconds=60)
    features, feature_names = common_features(
        batch, chainlink_timestamped, chainlink_lagged
    )
    if not bool(torch.isfinite(features).all().item()):
        raise RegimeModelError("Stage 4e causal feature matrix is not finite")
    run_dir = Path(output_root) / (
        f"{started.strftime('%Y%m%dT%H%M%SZ')}_{config.experiment_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    results: list[VariantResult] = []
    for variant in config.variants:
        result = _run_variant(variant, config, batch, features, feature_names, device)
        variant_dir = run_dir / variant
        variant_dir.mkdir()
        _write_csv(variant_dir / "decisions.csv", result.rows)
        _write_csv(variant_dir / "fold_metrics.csv", result.fold_metrics)
        _write_json(variant_dir / "summary.json", result.summary)
        _render_variant(variant_dir / "pnl_diagnostics.html", result, config)
        results.append(result)
    comparison_rows = [
        {
            key: value
            for key, value in result.summary.items()
            if key not in {"models", "bad_regime"}
        }
        | {
            "bad_regime_pnl_usdc": result.summary["bad_regime"]["net_pnl_usdc"],
            "bad_regime_fills": result.summary["bad_regime"]["fills"],
        }
        for result in results
    ]
    _write_csv(run_dir / "comparison.csv", comparison_rows)
    _write_json(run_dir / "comparison.json", comparison_rows)
    _render_comparison(run_dir / "comparison.html", results)
    proposal = _selection(results, config)
    _write_json(run_dir / "proposal.json", proposal)
    dataset_manifest = dataset_config.dataset_dir(data_root) / "manifest.json"
    _write_json(
        run_dir / "run_record.json",
        {
            "schema_version": 1,
            "experiment_id": config.experiment_id,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_seconds": time.monotonic() - started_monotonic,
            "source_commit": _git(["rev-parse", "HEAD"]),
            "source_dirty": bool(_git(["status", "--porcelain"])),
            "config_path": str(config_path),
            "config_sha256": config.config_sha256,
            "dataset_revision": dataset_config.revision,
            "dataset_config_sha256": dataset_config.config_sha256,
            "dataset_manifest": str(dataset_manifest),
            "dataset_manifest_sha256": _sha256(dataset_manifest),
            "chainlink_provenance": chainlink_provenance,
            "development_rows_loaded": len(batch),
            "development_valid_rows": int(batch.snapshot_valid.sum().item()),
            "holdout_rows_loaded": 0,
            "holdout_opened": False,
            "device": str(device),
            "dtype": config.dtype,
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(device),
            "plotly_version": plotly.__version__,
            "python": platform.python_version(),
        },
    )
    return run_dir
