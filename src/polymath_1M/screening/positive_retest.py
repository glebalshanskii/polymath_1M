from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import plotly
import plotly.graph_objects as go
import pyarrow as pa
import torch
from plotly.subplots import make_subplots

from polymath_1M.domain import DecisionBatch
from polymath_1M.strategy.model import (
    STATUS_NAMES,
    Evaluation,
    LookupModel,
    evaluate_batch,
    fit_lookup_model,
)
from polymath_1M.strategy.parameters import load_strategy_config

from .config import load_screening_config
from .regime_models import (
    _metrics,
    _recency_weights,
    fit_logistic_model,
    predict_logistic,
)
from .run import ScreeningData, _load_rows, _strategy_data


class PositiveRetestError(RuntimeError):
    """The frozen Stage 4i PMXT retest contract cannot be executed."""


@dataclass(frozen=True)
class Fold:
    fold_id: str
    train_start_s: int
    train_end_exclusive_s: int
    validation_start_s: int
    validation_end_exclusive_s: int


@dataclass(frozen=True)
class ModelSpec:
    config_id: str
    mechanism: str
    assets: tuple[str, ...]
    minimum_ask: float
    maximum_ask: float
    minimum_net_edge: float
    persistence_mode: str
    persistence_value: float
    minimum_support: int
    platform_fee_round_decimals: int


@dataclass(frozen=True)
class PositiveRetestConfig:
    experiment_id: str
    data_config: str
    period_start_s: int
    development_end_exclusive_s: int
    test_end_exclusive_s: int
    folds: tuple[Fold, ...]
    configurations: tuple[ModelSpec, ...]
    coarse_price_bin_width: float
    terminal_alpha: float
    transition_alpha: float
    target_notional_usdc: float
    primary_extra_cost_per_share: float
    stress_extra_cost_per_share: float
    decay_half_life_days: float
    logistic_ridge: float
    logistic_max_iterations: int
    minimum_probability: float
    maximum_probability: float
    minimum_asset_coverage: float
    minimum_test_fills: int
    seed: int
    device: str
    dtype: str
    config_sha256: str


EXPECTED_CONFIG_IDS = (
    "c1_stage4b_pooled",
    "c2_stage4c_btc",
    "c3_stage4c_eth",
    "c4_stage4c_sol",
    "c5_stage4c_xrp",
    "c6_stage4c_pooled",
    "c7_stage4d_btc_m0",
    "c8_stage4d_sol",
    "c9_stage4d_pooled",
    "m2_continuous_price",
    "m3_continuous_price_decay",
    "m4_microstructure_decay",
)


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git(arguments: list[str]) -> str | None:
    try:
        return subprocess.run(
            ["git", *arguments], check=True, capture_output=True, text=True
        ).stdout.strip()
    except FileNotFoundError, subprocess.CalledProcessError:
        return None


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise PositiveRetestError(f"cannot write empty CSV: {path}")
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def _timestamp(value: Any, name: str) -> int:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise PositiveRetestError(f"{name} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PositiveRetestError(f"{name} must include timezone")
    return int(parsed.astimezone(UTC).timestamp())


def load_positive_retest_config(path: str | Path) -> PositiveRetestConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "experiment_id",
        "data_config",
        "period_start",
        "development_end_exclusive",
        "test_end_exclusive",
        "folds",
        "configurations",
        "coarse_price_bin_width",
        "terminal_alpha",
        "transition_alpha",
        "target_notional_usdc",
        "primary_extra_cost_per_share",
        "stress_extra_cost_per_share",
        "decay_half_life_days",
        "logistic_ridge",
        "logistic_max_iterations",
        "minimum_probability",
        "maximum_probability",
        "minimum_asset_coverage",
        "minimum_test_fills",
        "seed",
        "device",
        "dtype",
    }
    if payload.keys() != expected or payload["schema_version"] != 1:
        raise PositiveRetestError("Stage 4i config fields or schema differ")
    period_start_s = _timestamp(payload["period_start"], "period_start")
    development_end_s = _timestamp(
        payload["development_end_exclusive"], "development_end_exclusive"
    )
    test_end_s = _timestamp(payload["test_end_exclusive"], "test_end_exclusive")
    fold_fields = {
        "fold_id",
        "train_start",
        "train_end_exclusive",
        "validation_start",
        "validation_end_exclusive",
    }
    folds: list[Fold] = []
    for raw in payload["folds"]:
        if raw.keys() != fold_fields:
            raise PositiveRetestError("Stage 4i fold fields differ")
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
            raise PositiveRetestError("Stage 4i fold chronology differs")
        folds.append(fold)
    if (
        len(folds) != 4
        or tuple(fold.fold_id for fold in folds)
        != ("fold_1", "fold_2", "fold_3", "fold_4")
        or folds[-1].validation_end_exclusive_s != development_end_s
        or not period_start_s < development_end_s < test_end_s
        or any(
            left.validation_end_exclusive_s != right.validation_start_s
            for left, right in pairwise(folds)
        )
    ):
        raise PositiveRetestError("Stage 4i walk-forward boundary differs")

    spec_fields = {
        "config_id",
        "mechanism",
        "assets",
        "minimum_ask",
        "maximum_ask",
        "minimum_net_edge",
        "persistence_mode",
        "persistence_value",
        "minimum_support",
        "platform_fee_round_decimals",
    }
    specs: list[ModelSpec] = []
    allowed_assets = {"BTC", "ETH", "SOL", "XRP"}
    allowed_mechanisms = {
        "coarse_lookup",
        "continuous_price",
        "continuous_price_decay",
        "microstructure_decay",
    }
    for raw in payload["configurations"]:
        if raw.keys() != spec_fields:
            raise PositiveRetestError("Stage 4i model fields differ")
        spec = ModelSpec(
            config_id=str(raw["config_id"]),
            mechanism=str(raw["mechanism"]),
            assets=tuple(str(value) for value in raw["assets"]),
            minimum_ask=float(raw["minimum_ask"]),
            maximum_ask=float(raw["maximum_ask"]),
            minimum_net_edge=float(raw["minimum_net_edge"]),
            persistence_mode=str(raw["persistence_mode"]),
            persistence_value=float(raw["persistence_value"]),
            minimum_support=int(raw["minimum_support"]),
            platform_fee_round_decimals=int(raw["platform_fee_round_decimals"]),
        )
        if (
            spec.mechanism not in allowed_mechanisms
            or not spec.assets
            or not set(spec.assets) <= allowed_assets
            or len(spec.assets) != len(set(spec.assets))
            or not 0 <= spec.minimum_ask < spec.maximum_ask <= 1
            or spec.minimum_net_edge < 0
            or spec.persistence_mode not in {"fixed", "train_quantile"}
            or not 0 <= spec.persistence_value <= 1
            or spec.minimum_support < 0
            or spec.platform_fee_round_decimals not in {4, 5}
        ):
            raise PositiveRetestError(f"invalid Stage 4i model: {spec.config_id}")
        specs.append(spec)
    if tuple(spec.config_id for spec in specs) != EXPECTED_CONFIG_IDS:
        raise PositiveRetestError("Stage 4i ordered model list differs")
    if any(spec.mechanism != "coarse_lookup" for spec in specs[:9]) or tuple(
        spec.mechanism for spec in specs[9:]
    ) != (
        "continuous_price",
        "continuous_price_decay",
        "microstructure_decay",
    ):
        raise PositiveRetestError("Stage 4i mechanism sequence differs")

    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    config = PositiveRetestConfig(
        experiment_id=str(payload["experiment_id"]),
        data_config=str(payload["data_config"]),
        period_start_s=period_start_s,
        development_end_exclusive_s=development_end_s,
        test_end_exclusive_s=test_end_s,
        folds=tuple(folds),
        configurations=tuple(specs),
        coarse_price_bin_width=float(payload["coarse_price_bin_width"]),
        terminal_alpha=float(payload["terminal_alpha"]),
        transition_alpha=float(payload["transition_alpha"]),
        target_notional_usdc=float(payload["target_notional_usdc"]),
        primary_extra_cost_per_share=float(payload["primary_extra_cost_per_share"]),
        stress_extra_cost_per_share=float(payload["stress_extra_cost_per_share"]),
        decay_half_life_days=float(payload["decay_half_life_days"]),
        logistic_ridge=float(payload["logistic_ridge"]),
        logistic_max_iterations=int(payload["logistic_max_iterations"]),
        minimum_probability=float(payload["minimum_probability"]),
        maximum_probability=float(payload["maximum_probability"]),
        minimum_asset_coverage=float(payload["minimum_asset_coverage"]),
        minimum_test_fills=int(payload["minimum_test_fills"]),
        seed=int(payload["seed"]),
        device=str(payload["device"]),
        dtype=str(payload["dtype"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )
    if (
        config.coarse_price_bin_width != 0.10
        or config.terminal_alpha != 20.0
        or config.transition_alpha != 1.0
        or config.target_notional_usdc != 10.0
        or config.primary_extra_cost_per_share != 0.01
        or config.stress_extra_cost_per_share != 0.02
        or config.decay_half_life_days != 7.0
        or config.logistic_ridge != 0.001
        or config.logistic_max_iterations != 30
        or config.minimum_probability != 0.01
        or config.maximum_probability != 0.99
        or config.minimum_asset_coverage != 0.99
        or config.minimum_test_fills != 50
        or config.seed != 20260813
        or config.device != "cuda"
        or config.dtype != "float64"
    ):
        raise PositiveRetestError("Stage 4i frozen numerical contract differs")
    return config


def _uniform_edges(width: float, device: torch.device) -> torch.Tensor:
    bins = round(1 / width)
    if not math.isclose(bins * width, 1.0, abs_tol=1e-12):
        raise PositiveRetestError("coarse bin width must divide one")
    edges = torch.arange(bins + 1, dtype=torch.float64, device=device) * width
    edges[-1] = 1.000001
    return edges


def _indices(batch: DecisionBatch, start_s: int, end_s: int) -> torch.Tensor:
    return torch.nonzero(
        (batch.market_start_s >= start_s) & (batch.market_start_s < end_s),
        as_tuple=False,
    ).flatten()


def _select_assets(data: ScreeningData, assets: tuple[str, ...]) -> ScreeningData:
    allowed = set(assets)
    indices = torch.tensor(
        [index for index, asset in enumerate(data.batch.assets) if asset in allowed],
        dtype=torch.int64,
    )
    if indices.numel() == 0:
        raise PositiveRetestError(f"empty PMXT universe for {assets}")
    return data.index(indices)


def token_features(batch: DecisionBatch) -> tuple[torch.Tensor, tuple[str, ...]]:
    clipped = torch.clamp(batch.current_mid_up, 1e-6, 1 - 1e-6)
    values = torch.stack(
        (
            torch.log(clipped / (1 - clipped)),
            batch.current_mid_up - batch.previous_mid_up,
            batch.asks[:, 0] - batch.bids[:, 0],
            batch.asks[:, 1] - batch.bids[:, 1],
            batch.current_mid.sum(dim=1) - 1,
        ),
        dim=1,
    )
    return (
        values,
        (
            "logit_mid_up",
            "mid_change_1m",
            "spread_up",
            "spread_down",
            "complement_dislocation",
        ),
    )


def _persistence_threshold(
    spec: ModelSpec, train: DecisionBatch, model: LookupModel
) -> float:
    if spec.persistence_mode == "fixed":
        return spec.persistence_value
    states = torch.bucketize(train.current_mid_up, model.edges[1:-1], right=False)
    values = model.persistence[states]
    valid = train.snapshot_valid & torch.isfinite(values)
    if int(valid.sum().item()) == 0:
        raise PositiveRetestError("train has no valid persistence values")
    return float(
        torch.quantile(
            values[valid],
            torch.tensor(
                spec.persistence_value, dtype=torch.float64, device=values.device
            ),
        ).item()
    )


def _probability_override(
    spec: ModelSpec,
    train: DecisionBatch,
    validation: DecisionBatch,
    train_end_s: int,
    config: PositiveRetestConfig,
) -> tuple[torch.Tensor | None, dict[str, Any]]:
    if spec.mechanism == "coarse_lookup":
        return None, {}
    train_features, feature_names = token_features(train)
    validation_features, _ = token_features(validation)
    columns = (0,) if spec.mechanism != "microstructure_decay" else tuple(range(5))
    device = train.current_mid_up.device
    column_index = torch.tensor(columns, dtype=torch.int64, device=device)
    weights = torch.ones(len(train), dtype=torch.float64, device=device)
    if spec.mechanism != "continuous_price":
        weights = _recency_weights(train, train_end_s, config.decay_half_life_days)
    logistic = fit_logistic_model(
        train_features.index_select(1, column_index),
        train.outcome_up,
        weights,
        ridge=config.logistic_ridge,
        maximum_iterations=config.logistic_max_iterations,
    )
    probability = torch.clamp(
        predict_logistic(logistic, validation_features.index_select(1, column_index)),
        config.minimum_probability,
        config.maximum_probability,
    )
    return (
        probability,
        {
            "feature_names": [feature_names[index] for index in columns],
            "mean": logistic.mean.detach().cpu().tolist(),
            "scale": logistic.scale.detach().cpu().tolist(),
            "coefficients": logistic.coefficients.detach().cpu().tolist(),
            "iterations": logistic.iterations,
        },
    )


def _evaluate_window(
    spec: ModelSpec,
    train_data: ScreeningData,
    evaluation_data: ScreeningData,
    train_end_s: int,
    config: PositiveRetestConfig,
    edges: torch.Tensor,
) -> tuple[Evaluation, float, dict[str, Any], DecisionBatch]:
    model = fit_lookup_model(
        train_data.batch,
        edges,
        terminal_alpha=config.terminal_alpha,
        transition_alpha=config.transition_alpha,
    )
    persistence = _persistence_threshold(spec, train_data.batch, model)
    probability, model_record = _probability_override(
        spec,
        train_data.batch,
        evaluation_data.batch,
        train_end_s,
        config,
    )
    model_record.update(
        {
            "coarse_probability_up": model.probability_up.detach().cpu().tolist(),
            "coarse_support": model.support.detach().cpu().tolist(),
            "coarse_persistence": model.persistence.detach().cpu().tolist(),
            "coarse_transition_matrix": model.transition_matrix.detach().cpu().tolist(),
            "coarse_prior_up": float(model.prior_up.item()),
        }
    )
    batch = evaluation_data.batch
    if probability is not None:
        finite = torch.isfinite(probability)
        batch = replace(batch, snapshot_valid=batch.snapshot_valid & finite)
        probability = torch.where(
            finite, probability, torch.full_like(probability, 0.5)
        )
    result = evaluate_batch(
        batch,
        model,
        minimum_support=spec.minimum_support,
        minimum_persistence=persistence,
        minimum_ask=spec.minimum_ask,
        maximum_ask=spec.maximum_ask,
        minimum_net_edge=spec.minimum_net_edge,
        target_notional_usdc=config.target_notional_usdc,
        platform_fee_rate=evaluation_data.fee_rate,
        platform_fee_round_decimals=spec.platform_fee_round_decimals,
        extra_cost_per_share=config.primary_extra_cost_per_share,
        require_market_favorite=False,
        probability_up_override=probability,
    )
    return result, persistence, model_record, batch


def _decision_rows(
    spec: ModelSpec,
    fold_id: str,
    split: str,
    batch: DecisionBatch,
    result: Evaluation,
    persistence_threshold: float,
    config: PositiveRetestConfig,
) -> list[dict[str, Any]]:
    cpu = batch.to(torch.device("cpu"), torch.float64)
    tensors = {
        name: getattr(result, name).detach().cpu()
        for name in (
            "state_bin",
            "probability",
            "support",
            "persistence",
            "side",
            "signal_ask",
            "fill_vwap",
            "fill_cost",
            "platform_fee",
            "net_edge",
            "fill_shares",
            "gross_pnl",
            "extra_cost",
            "net_pnl",
            "filled",
            "status_code",
        )
    }
    delta_cost = (
        config.stress_extra_cost_per_share - config.primary_extra_cost_per_share
    )
    rows: list[dict[str, Any]] = []
    for index, condition_id in enumerate(cpu.condition_ids):
        side = int(tensors["side"][index].item())
        filled = bool(tensors["filled"][index].item())
        shares = float(tensors["fill_shares"][index].item())
        net_pnl = float(tensors["net_pnl"][index].item())
        rows.append(
            {
                "config_id": spec.config_id,
                "mechanism": spec.mechanism,
                "split": split,
                "fold_id": fold_id,
                "condition_id": condition_id,
                "asset": cpu.assets[index],
                "market_start_utc": datetime.fromtimestamp(
                    int(cpu.market_start_s[index].item()), tz=UTC
                ).isoformat(),
                "market_start_s": int(cpu.market_start_s[index].item()),
                "decision_s": int(cpu.decision_s[index].item()),
                "outcome_up": float(cpu.outcome_up[index].item()),
                "side": "UP" if side == 0 else "DOWN",
                "outcome_side": float(
                    cpu.outcome_up[index].item()
                    if side == 0
                    else 1 - cpu.outcome_up[index].item()
                ),
                "current_mid_up": float(cpu.current_mid_up[index].item()),
                "previous_mid_up": float(cpu.previous_mid_up[index].item()),
                "persistence_threshold": persistence_threshold,
                "state_bin": int(tensors["state_bin"][index].item()),
                "forecast_probability": float(tensors["probability"][index].item()),
                "support": int(tensors["support"][index].item()),
                "persistence": float(tensors["persistence"][index].item()),
                "side_code": side,
                "signal_ask": float(tensors["signal_ask"][index].item()),
                "fill_vwap": float(tensors["fill_vwap"][index].item()),
                "fill_cost": float(tensors["fill_cost"][index].item()),
                "platform_fee": float(tensors["platform_fee"][index].item()),
                "net_edge": float(tensors["net_edge"][index].item()),
                "fill_shares": shares,
                "gross_pnl": float(tensors["gross_pnl"][index].item()),
                "extra_cost": float(tensors["extra_cost"][index].item()),
                "net_pnl": net_pnl,
                "stress_2c_net_pnl": net_pnl - shares * delta_cost,
                "filled": filled,
                "status_code": int(tensors["status_code"][index].item()),
                "status": STATUS_NAMES[int(tensors["status_code"][index].item())],
            }
        )
    return rows


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = _metrics(rows)
    metrics["valid_snapshots"] = sum(row["status"] != "data_invalid" for row in rows)
    metrics["status_counts"] = dict(
        sorted(Counter(row["status"] for row in rows).items())
    )
    valid_forecasts = [
        row
        for row in rows
        if row["status"] != "data_invalid"
        and math.isfinite(float(row["forecast_probability"]))
        and math.isfinite(float(row["outcome_side"]))
    ]
    if valid_forecasts:
        probability = torch.tensor(
            [row["forecast_probability"] for row in valid_forecasts],
            dtype=torch.float64,
        )
        outcome = torch.tensor(
            [row["outcome_side"] for row in valid_forecasts], dtype=torch.float64
        )
        clipped = torch.clamp(probability, 1e-12, 1 - 1e-12)
        metrics["all_valid_forecast"] = {
            "markets": len(valid_forecasts),
            "brier_score": float((probability - outcome).square().mean().item()),
            "log_loss": float(
                -(outcome * torch.log(clipped) + (1 - outcome) * torch.log(1 - clipped))
                .mean()
                .item()
            ),
            "calibration_gap": float((probability - outcome).mean().item()),
        }
    else:
        metrics["all_valid_forecast"] = None
    return metrics


def _profit_factor_above_one(summary: dict[str, Any]) -> bool:
    return bool(summary["profit_factor_infinite"]) or bool(
        summary["profit_factor"] is not None and summary["profit_factor"] > 1
    )


def _verdict(
    coverage_pass: bool,
    development: dict[str, Any],
    test: dict[str, Any],
    config: PositiveRetestConfig,
) -> tuple[str, dict[str, bool]]:
    gates = {
        "coverage": coverage_pass,
        "development_positive_pnl": development["net_pnl_usdc"] > 0,
        "development_profit_factor": _profit_factor_above_one(development),
        "development_positive_folds": development["positive_folds"] >= 3,
        "development_positive_stress": development["stress_2c_net_pnl_usdc"] > 0,
        "test_minimum_fills": test["fills"] >= config.minimum_test_fills,
        "test_positive_pnl": test["net_pnl_usdc"] > 0,
        "test_profit_factor": _profit_factor_above_one(test),
        "test_positive_stress": test["stress_2c_net_pnl_usdc"] > 0,
    }
    if not coverage_pass:
        return "insufficient_data", gates
    return ("replicated_positive" if all(gates.values()) else "not_replicated"), gates


def _render_model(
    path: Path,
    spec: ModelSpec,
    rows: list[dict[str, Any]],
    test_start_s: int,
) -> None:
    filled = sorted(
        (row for row in rows if row["filled"]),
        key=lambda row: (row["decision_s"], row["condition_id"]),
    )
    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.055,
        specs=[[{"secondary_y": True}], [{}], [{"secondary_y": True}], [{}]],
        subplot_titles=(
            "Cumulative PnL и drawdown",
            "PnL сделки",
            "Forecast, исход и стоимость позиции",
            "Дневной PnL",
        ),
    )
    if filled:
        times = [row["market_start_utc"] for row in filled]
        pnl = torch.tensor([row["net_pnl"] for row in filled], dtype=torch.float64)
        stress = torch.tensor(
            [row["stress_2c_net_pnl"] for row in filled], dtype=torch.float64
        )
        cumulative = torch.cumsum(pnl, dim=0)
        cumulative_stress = torch.cumsum(stress, dim=0)
        peaks = torch.cummax(
            torch.cat((torch.zeros(1, dtype=torch.float64), cumulative)), dim=0
        ).values[1:]
        drawdown = peaks - cumulative
        figure.add_trace(
            go.Scatter(x=times, y=cumulative.tolist(), name="PnL 1¢"),
            row=1,
            col=1,
            secondary_y=False,
        )
        figure.add_trace(
            go.Scatter(
                x=times,
                y=cumulative_stress.tolist(),
                name="PnL 2¢ stress",
                line={"dash": "dot"},
            ),
            row=1,
            col=1,
            secondary_y=False,
        )
        figure.add_trace(
            go.Scatter(
                x=times,
                y=drawdown.tolist(),
                name="Drawdown",
                fill="tozeroy",
                opacity=0.25,
            ),
            row=1,
            col=1,
            secondary_y=True,
        )
        figure.add_trace(
            go.Bar(
                x=times,
                y=pnl.tolist(),
                name="Trade PnL",
                marker_color=["#2ca02c" if value >= 0 else "#d62728" for value in pnl],
            ),
            row=2,
            col=1,
        )
        figure.add_trace(
            go.Scatter(
                x=times,
                y=[row["forecast_probability"] for row in filled],
                name="Forecast",
                mode="markers",
                marker={"size": 5},
            ),
            row=3,
            col=1,
            secondary_y=False,
        )
        figure.add_trace(
            go.Scatter(
                x=times,
                y=[row["outcome_side"] for row in filled],
                name="Outcome",
                mode="markers",
                marker={"size": 4, "symbol": "x"},
            ),
            row=3,
            col=1,
            secondary_y=False,
        )
        figure.add_trace(
            go.Scatter(
                x=times,
                y=[
                    row["fill_cost"] + row["platform_fee"] + row["extra_cost"]
                    for row in filled
                ],
                name="Position outlay",
                mode="markers",
                marker={"size": 5},
            ),
            row=3,
            col=1,
            secondary_y=True,
        )
        daily: dict[str, float] = defaultdict(float)
        for row in filled:
            daily[row["market_start_utc"][:10]] += row["net_pnl"]
        figure.add_trace(
            go.Bar(
                x=list(daily),
                y=list(daily.values()),
                name="Daily PnL",
                marker_color=[
                    "#2ca02c" if value >= 0 else "#d62728" for value in daily.values()
                ],
            ),
            row=4,
            col=1,
        )
    boundary = datetime.fromtimestamp(test_start_s, tz=UTC)
    figure.add_vline(x=boundary, line_dash="dash", line_color="black")
    figure.update_yaxes(title_text="USDC", row=1, col=1, secondary_y=False)
    figure.update_yaxes(title_text="DD", row=1, col=1, secondary_y=True)
    figure.update_yaxes(title_text="USDC", row=2, col=1)
    figure.update_yaxes(title_text="Probability", range=[-0.05, 1.05], row=3, col=1)
    figure.update_yaxes(title_text="USDC", row=3, col=1, secondary_y=True)
    figure.update_yaxes(title_text="USDC", row=4, col=1)
    figure.update_layout(
        title=f"Stage 4i PMXT — {spec.config_id}",
        template="plotly_white",
        height=1050,
        hovermode="x unified",
        barmode="overlay",
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def _render_comparison(
    path: Path, results: list[dict[str, Any]], test_start_s: int
) -> None:
    figure = go.Figure()
    for result in results:
        filled = sorted(
            (row for row in result["rows"] if row["filled"]),
            key=lambda row: (row["decision_s"], row["condition_id"]),
        )
        pnl = torch.tensor([row["net_pnl"] for row in filled], dtype=torch.float64)
        figure.add_trace(
            go.Scatter(
                x=[row["market_start_utc"] for row in filled],
                y=torch.cumsum(pnl, dim=0).tolist(),
                name=result["config_id"],
            )
        )
    figure.add_vline(
        x=datetime.fromtimestamp(test_start_s, tz=UTC),
        line_dash="dash",
        line_color="black",
    )
    figure.update_layout(
        title="Stage 4i: PMXT positive-model retest",
        xaxis_title="UTC",
        yaxis_title="Cumulative net PnL, USDC",
        template="plotly_white",
        height=750,
        hovermode="x unified",
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def _coverage(data: ScreeningData) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        mask = torch.tensor(
            [value == asset for value in data.batch.assets], dtype=torch.bool
        )
        markets = int(mask.sum().item())
        valid = int((data.batch.snapshot_valid & mask).sum().item())
        result[asset] = {
            "markets": markets,
            "valid": valid,
            "coverage": valid / markets if markets else 0.0,
        }
    return result


def run_stage4i_positive_retest(
    config_path: str | Path,
    output_root: str | Path = "outputs/positive_retest",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    source_dirty = bool(_git(["status", "--porcelain"]))
    if source_dirty:
        raise PositiveRetestError(
            "commit frozen Stage 4i code/config before target run"
        )
    config = load_positive_retest_config(config_path)
    data_config = load_screening_config(config.data_config)
    if (
        int(data_config.period_start.timestamp()) != config.period_start_s
        or int(data_config.period_end_exclusive.timestamp())
        != config.test_end_exclusive_s
        or data_config.decision_seconds_before_end != 60
        or data_config.transition_horizon_seconds != 60
        or data_config.execution_latency_ms != 1_000
    ):
        raise PositiveRetestError("PMXT data config differs from Stage 4i timing")
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise PositiveRetestError("frozen CUDA device is unavailable")
    torch.manual_seed(config.seed)
    rows, dataset_manifest = _load_rows(data_config)
    strategy = load_strategy_config("cfg/strategies/stage4c_multi_asset_5m.json")
    all_data = _strategy_data(rows, strategy, data_config)
    coverage = _coverage(all_data)
    edges = _uniform_edges(config.coarse_price_bin_width, device)
    run_dir = Path(output_root) / (
        f"{started.strftime('%Y%m%dT%H%M%SZ')}_{config.experiment_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []
    render_payload: list[dict[str, Any]] = []
    for spec in config.configurations:
        selected = _select_assets(all_data, spec.assets)
        fold_rows: list[dict[str, Any]] = []
        fold_metrics: list[dict[str, Any]] = []
        model_records: list[dict[str, Any]] = []
        for fold in config.folds:
            train_indices = _indices(
                selected.batch, fold.train_start_s, fold.train_end_exclusive_s
            )
            validation_indices = _indices(
                selected.batch,
                fold.validation_start_s,
                fold.validation_end_exclusive_s,
            )
            train = selected.index(train_indices).to(device)
            validation = selected.index(validation_indices).to(device)
            evaluation, threshold, model_record, evaluation_batch = _evaluate_window(
                spec,
                train,
                validation,
                fold.train_end_exclusive_s,
                config,
                edges,
            )
            rows_fold = _decision_rows(
                spec,
                fold.fold_id,
                "development_validation",
                evaluation_batch,
                evaluation,
                threshold,
                config,
            )
            summary_fold = _summary(rows_fold)
            summary_fold.update(
                {
                    "fold_id": fold.fold_id,
                    "train_markets": len(train.batch),
                    "persistence_threshold": threshold,
                }
            )
            fold_rows.extend(rows_fold)
            fold_metrics.append(summary_fold)
            model_records.append({"fold_id": fold.fold_id, **model_record})
        development_summary = _summary(fold_rows)
        development_summary["positive_folds"] = sum(
            item["net_pnl_usdc"] > 0 for item in fold_metrics
        )
        development_summary["positive_stress_folds"] = sum(
            item["stress_2c_net_pnl_usdc"] > 0 for item in fold_metrics
        )

        development_indices = _indices(
            selected.batch,
            config.period_start_s,
            config.development_end_exclusive_s,
        )
        test_indices = _indices(
            selected.batch,
            config.development_end_exclusive_s,
            config.test_end_exclusive_s,
        )
        development = selected.index(development_indices).to(device)
        test = selected.index(test_indices).to(device)
        test_evaluation, test_threshold, test_model, test_batch = _evaluate_window(
            spec,
            development,
            test,
            config.development_end_exclusive_s,
            config,
            edges,
        )
        rows_test = _decision_rows(
            spec,
            "final_period",
            "final_period",
            test_batch,
            test_evaluation,
            test_threshold,
            config,
        )
        test_summary = _summary(rows_test)
        spec_rows = fold_rows + rows_test
        coverage_pass = all(
            coverage[asset]["coverage"] >= config.minimum_asset_coverage
            for asset in spec.assets
        )
        verdict, gates = _verdict(
            coverage_pass, development_summary, test_summary, config
        )
        config_dir = run_dir / spec.config_id
        config_dir.mkdir()
        summary = {
            "config_id": spec.config_id,
            "mechanism": spec.mechanism,
            "assets": list(spec.assets),
            "parameters": {
                "minimum_ask": spec.minimum_ask,
                "maximum_ask": spec.maximum_ask,
                "minimum_net_edge": spec.minimum_net_edge,
                "persistence_mode": spec.persistence_mode,
                "persistence_value": spec.persistence_value,
                "minimum_support": spec.minimum_support,
                "platform_fee_round_decimals": spec.platform_fee_round_decimals,
            },
            "coverage": {asset: coverage[asset] for asset in spec.assets},
            "folds": fold_metrics,
            "development": development_summary,
            "final_period": test_summary,
            "final_persistence_threshold": test_threshold,
            "models": model_records,
            "final_model": test_model,
            "verdict": verdict,
            "gates": gates,
        }
        _write_csv(config_dir / "decisions.csv", spec_rows)
        _write_json(config_dir / "summary.json", summary)
        _render_model(
            config_dir / "pnl_diagnostics.html",
            spec,
            spec_rows,
            config.development_end_exclusive_s,
        )
        results.append(summary)
        render_payload.append({"config_id": spec.config_id, "rows": spec_rows})
        print(
            f"{spec.config_id}: dev={development_summary['net_pnl_usdc']:.2f} "
            f"test={test_summary['net_pnl_usdc']:.2f} verdict={verdict}",
            flush=True,
        )
    _render_comparison(
        run_dir / "comparison.html",
        render_payload,
        config.development_end_exclusive_s,
    )
    manifest_path = Path(data_config.data_root) / "pmxt_dataset_manifest.json"
    result = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "source_commit": source_commit,
        "config_path": str(config_path),
        "config_sha256": config.config_sha256,
        "data_config": config.data_config,
        "data_config_sha256": data_config.config_sha256,
        "pmxt_dataset_manifest": str(manifest_path),
        "pmxt_dataset_manifest_sha256": _sha256(manifest_path),
        "pmxt_source_inventory": dataset_manifest["source_inventory"],
        "coverage": coverage,
        "results": results,
        "replicated_positive": [
            item["config_id"]
            for item in results
            if item["verdict"] == "replicated_positive"
        ],
    }
    _write_json(run_dir / "result.json", result)
    _write_json(
        run_dir / "run_record.json",
        {
            "schema_version": 1,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_seconds": time.monotonic() - started_monotonic,
            "source_commit": source_commit,
            "source_dirty": source_dirty,
            "config_path": str(config_path),
            "config_sha256": config.config_sha256,
            "device": str(device),
            "dtype": config.dtype,
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(device)
            if device.type == "cuda"
            else None,
            "plotly_version": plotly.__version__,
            "pyarrow_version": pa.__version__,
            "python": platform.python_version(),
        },
    )
    artifacts = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file() and path.name != "artifact_manifest.json":
            artifacts.append(
                {
                    "path": str(path.relative_to(run_dir)),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    _write_json(
        run_dir / "artifact_manifest.json",
        {"schema_version": 1, "artifacts": artifacts},
    )
    return run_dir
