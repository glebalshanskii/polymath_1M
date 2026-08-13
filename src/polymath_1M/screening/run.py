from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import torch

from polymath_1M.domain import DecisionBatch
from polymath_1M.strategy.model import (
    STATUS_NAMES,
    Evaluation,
    LookupModel,
    evaluate_batch,
    fit_lookup_model,
)
from polymath_1M.strategy.parameters import StrategyConfig, load_strategy_config

from .config import ScreeningConfig, load_screening_config


class ScreeningRunError(RuntimeError):
    """Stage 4 screening inputs or runtime violate the frozen contract."""


@dataclass(frozen=True)
class ScreeningData:
    batch: DecisionBatch
    fee_rate: torch.Tensor

    def index(self, indices: torch.Tensor) -> ScreeningData:
        return ScreeningData(
            batch=self.batch.index(indices),
            fee_rate=self.fee_rate.index_select(0, indices.to(self.fee_rate.device)),
        )

    def to(self, device: torch.device) -> ScreeningData:
        return ScreeningData(
            batch=self.batch.to(device, torch.float64),
            fee_rate=self.fee_rate.to(device=device, dtype=torch.float64),
        )


def _git(args: list[str]) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True
        ).stdout.strip()
    except FileNotFoundError, subprocess.CalledProcessError:
        return None


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


def _load_rows(
    config: ScreeningConfig, *, row_end_exclusive_s: int | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = Path(config.data_root)
    manifest_path = root / "pmxt_dataset_manifest.json"
    if not manifest_path.is_file():
        raise ScreeningRunError("build the PMXT dataset before screening")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("data_contract_sha256") != config.data_contract_sha256:
        raise ScreeningRunError("PMXT manifest and config hashes differ")
    period_seconds = int(
        (config.period_end_exclusive - config.period_start).total_seconds()
    )
    if period_seconds % 3_600:
        raise ScreeningRunError("screening period must contain whole UTC hours")
    expected_hours = period_seconds // 3_600
    market_count = int(manifest.get("market_count", 0))
    valid_count = int(manifest.get("valid_count", 0))
    if int(manifest.get("hour_count", 0)) != expected_hours or market_count <= 0:
        raise ScreeningRunError("PMXT dataset is incomplete for the frozen horizon")
    if valid_count / market_count < 0.99:
        raise ScreeningRunError("PMXT causal coverage is below the frozen 99% gate")
    rows: list[dict[str, Any]] = []
    for item in manifest["hours"]:
        hour_s = int(
            datetime.strptime(item["hour"], "%Y-%m-%dT%H")
            .replace(tzinfo=UTC)
            .timestamp()
        )
        if row_end_exclusive_s is not None and hour_s >= row_end_exclusive_s:
            continue
        path = Path(item["parquet_path"])
        if not path.is_file() or _sha256(path) != item["parquet_sha256"]:
            raise ScreeningRunError(f"PMXT checkpoint hash mismatch: {path}")
        rows.extend(pq.read_table(path).to_pylist())
    rows.sort(key=lambda row: (row["market_start_ms"], row["condition_id"]))
    if row_end_exclusive_s is None and len(rows) != int(manifest["market_count"]):
        raise ScreeningRunError("PMXT manifest market count mismatch")
    return rows, manifest


def _strategy_data(
    rows: list[dict[str, Any]], strategy: StrategyConfig, config: ScreeningConfig
) -> ScreeningData:
    selected = [
        row
        for row in rows
        if row["asset"] in strategy.assets and row["duration"] == strategy.duration
    ]
    if not selected:
        raise ScreeningRunError(f"empty universe for {strategy.strategy_id}")
    maximum_depth = max(
        (
            max(
                len(row["execution_ask_prices_up"]),
                len(row["execution_ask_prices_down"]),
            )
            for row in selected
        ),
        default=0,
    )
    maximum_depth = max(maximum_depth, 1)
    count = len(selected)
    prices = torch.full((count, 2, maximum_depth), torch.nan, dtype=torch.float64)
    sizes = torch.zeros((count, 2, maximum_depth), dtype=torch.float64)
    for index, row in enumerate(selected):
        for side, suffix in enumerate(("up", "down")):
            side_prices = torch.tensor(
                row[f"execution_ask_prices_{suffix}"], dtype=torch.float64
            )
            side_sizes = torch.tensor(
                row[f"execution_ask_sizes_{suffix}"], dtype=torch.float64
            )
            prices[index, side, : side_prices.numel()] = side_prices
            sizes[index, side, : side_sizes.numel()] = side_sizes

    def floats(name: str) -> torch.Tensor:
        return torch.tensor(
            [float("nan") if row[name] is None else row[name] for row in selected],
            dtype=torch.float64,
        )

    market_start = torch.tensor(
        [int(row["market_start_ms"]) // 1_000 for row in selected], dtype=torch.int64
    )
    market_end = torch.tensor(
        [int(row["market_end_ms"]) // 1_000 for row in selected], dtype=torch.int64
    )
    decision = market_end - config.decision_seconds_before_end
    previous = decision - config.transition_horizon_seconds
    current_mid = torch.stack(
        (floats("signal_mid_up"), floats("signal_mid_down")), dim=1
    )
    batch = DecisionBatch(
        condition_ids=tuple(str(row["condition_id"]) for row in selected),
        assets=tuple(str(row["asset"]) for row in selected),
        market_start_s=market_start,
        market_end_s=market_end,
        decision_s=decision,
        previous_s=previous,
        outcome_up=floats("outcome_up"),
        n_ticks=torch.tensor(
            [int(row["pmxt_event_rows"]) for row in selected], dtype=torch.int64
        ),
        previous_mid_up=floats("previous_mid_up"),
        current_mid_up=floats("signal_mid_up"),
        current_mid=current_mid,
        bids=torch.stack((floats("signal_bid_up"), floats("signal_bid_down")), dim=1),
        asks=torch.stack((floats("signal_ask_up"), floats("signal_ask_down")), dim=1),
        ask_depth_prices=prices,
        ask_depth_sizes=sizes,
        snapshot_valid=torch.tensor(
            [bool(row["snapshot_valid"]) for row in selected], dtype=torch.bool
        ),
        label_source="gamma_authoritative_terminal_price",
    )
    return ScreeningData(batch=batch, fee_rate=floats("fee_rate"))


def chronological_market_splits(
    market_start_s: torch.Tensor, train_fraction: float, validation_fraction: float
) -> dict[str, torch.Tensor]:
    starts = market_start_s.detach().cpu()
    unique = torch.unique(starts, sorted=True)
    train_end = int(unique.numel() * train_fraction)
    validation_end = int(unique.numel() * (train_fraction + validation_fraction))
    if (
        train_end == 0
        or validation_end <= train_end
        or validation_end >= unique.numel()
    ):
        raise ScreeningRunError("chronological split has an empty partition")
    train_boundary = unique[train_end]
    validation_boundary = unique[validation_end]
    return {
        "train": torch.nonzero(starts < train_boundary, as_tuple=False).flatten(),
        "validation": torch.nonzero(
            (starts >= train_boundary) & (starts < validation_boundary),
            as_tuple=False,
        ).flatten(),
        "test": torch.nonzero(starts >= validation_boundary, as_tuple=False).flatten(),
    }


def literal_one_step_model(model: LookupModel) -> LookupModel:
    centers = torch.clamp((model.edges[:-1] + model.edges[1:]) / 2, 0, 1)
    return replace(
        model,
        probability_up=model.transition_matrix @ centers,
    )


def _evaluate(
    data: ScreeningData,
    model: LookupModel,
    config: ScreeningConfig,
    strategy: StrategyConfig,
    cost: float,
    variant: str,
) -> Evaluation:
    forced_side = None
    support_gate = True
    persistence_gate = True
    edge_gate = True
    model_to_use = model
    require_favorite = strategy.require_market_favorite
    if variant == "literal_one_step":
        model_to_use = literal_one_step_model(model)
    elif variant == "range_only":
        forced_side = torch.argmax(data.batch.current_mid, dim=1)
        support_gate = False
        persistence_gate = False
        edge_gate = False
        require_favorite = False
    elif variant != "terminal_lookup":
        raise ScreeningRunError(f"unknown model variant: {variant}")
    return evaluate_batch(
        data.batch,
        model_to_use,
        minimum_support=config.minimum_support,
        minimum_persistence=strategy.minimum_persistence,
        minimum_ask=strategy.minimum_ask,
        maximum_ask=strategy.maximum_ask,
        minimum_net_edge=strategy.minimum_net_edge,
        target_notional_usdc=strategy.target_notional_usdc,
        platform_fee_rate=data.fee_rate,
        platform_fee_round_decimals=5,
        extra_cost_per_share=cost,
        require_market_favorite=require_favorite,
        forced_side=forced_side,
        apply_support_gate=support_gate,
        apply_persistence_gate=persistence_gate,
        apply_edge_gate=edge_gate,
    )


def _optional_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def evaluation_summary(data: ScreeningData, result: Evaluation) -> dict[str, Any]:
    pnl = result.net_pnl[result.filled]
    positive = float(pnl[pnl > 0].sum().item())
    negative = float(-pnl[pnl < 0].sum().item())
    cumulative = torch.cumsum(result.net_pnl, dim=0)
    if cumulative.numel():
        peaks = torch.cummax(
            torch.cat(
                (
                    torch.zeros(1, device=cumulative.device, dtype=cumulative.dtype),
                    cumulative,
                )
            ),
            dim=0,
        ).values[1:]
        max_drawdown = float((peaks - cumulative).max().item())
    else:
        max_drawdown = 0.0
    cash_cost = float(
        (result.fill_cost + result.platform_fee + result.extra_cost)[result.filled]
        .sum()
        .item()
    )
    statuses = Counter(
        STATUS_NAMES[index] for index in result.status_code.detach().cpu().tolist()
    )
    starts = data.batch.market_start_s.detach().cpu()
    net_cpu = result.net_pnl.detach().cpu()
    weekly: dict[str, float] = defaultdict(float)
    daily: dict[str, float] = defaultdict(float)
    for timestamp, value in zip(starts.tolist(), net_cpu.tolist(), strict=True):
        date = datetime.fromtimestamp(timestamp, tz=UTC)
        daily[date.date().isoformat()] += value
        iso = date.isocalendar()
        weekly[f"{iso.year}-W{iso.week:02d}"] += value
    positive_daily = {key: value for key, value in daily.items() if value > 0}
    positive_day_sum = sum(positive_daily.values())
    maximum_day_share = (
        max(positive_daily.values()) / positive_day_sum
        if positive_day_sum > 0
        else None
    )
    fills = int(result.filled.sum().item())
    return {
        "markets": len(data.batch),
        "valid_snapshots": int(data.batch.snapshot_valid.sum().item()),
        "fills": fills,
        "fill_rate": fills / len(data.batch),
        "gross_pnl_usdc": float(result.gross_pnl.sum().item()),
        "platform_fee_usdc": float(result.platform_fee.sum().item()),
        "extra_cost_usdc": float(result.extra_cost.sum().item()),
        "net_pnl_usdc": float(result.net_pnl.sum().item()),
        "cash_cost_usdc": cash_cost,
        "return_on_cash_cost": _optional_ratio(
            float(result.net_pnl.sum().item()), cash_cost
        ),
        "profit_factor": _optional_ratio(positive, negative),
        "max_drawdown_usdc": max_drawdown,
        "calendar_days": len(daily),
        "daily_net_pnl_usdc": dict(sorted(daily.items())),
        "weekly_net_pnl_usdc": dict(sorted(weekly.items())),
        "maximum_positive_day_share": maximum_day_share,
        "status_counts": dict(sorted(statuses.items())),
    }


def _decision_rows(
    data: ScreeningData,
    result: Evaluation,
    *,
    variant: str,
    cost: float,
) -> list[dict[str, Any]]:
    values = {
        name: getattr(result, name).detach().cpu().tolist()
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
    return [
        {
            "condition_id": condition_id,
            "asset": data.batch.assets[index],
            "market_start_s": int(data.batch.market_start_s[index].item()),
            "variant": variant,
            "extra_cost_per_share": cost,
            **{name: value[index] for name, value in values.items()},
            "status": STATUS_NAMES[values["status_code"][index]],
        }
        for index, condition_id in enumerate(data.batch.condition_ids)
    ]


def _primary_gate(
    summary: dict[str, Any],
    config: ScreeningConfig,
    neighbor_summaries: dict[str, dict[str, Any]],
) -> tuple[str, list[str]]:
    failures: list[str] = []
    if summary["fills"] < config.minimum_test_fills:
        failures.append("minimum_test_fills")
    if summary["calendar_days"] < config.minimum_test_calendar_days_for_concentration:
        failures.append("minimum_test_calendar_days_for_concentration")
    if summary["net_pnl_usdc"] <= 0:
        failures.append("positive_net_pnl")
    if (
        summary["profit_factor"] is None
        or summary["profit_factor"] < config.minimum_profit_factor
    ):
        failures.append("minimum_profit_factor")
    if (
        summary["max_drawdown_usdc"]
        >= config.initial_bankroll_usdc * config.maximum_drawdown_fraction
    ):
        failures.append("maximum_drawdown")
    day_share = summary["maximum_positive_day_share"]
    if day_share is None or day_share > config.maximum_single_day_profit_share:
        failures.append("daily_concentration")
    base = summary["net_pnl_usdc"]
    for name, neighbor in neighbor_summaries.items():
        if neighbor["net_pnl_usdc"] <= 0 or neighbor["net_pnl_usdc"] < 0.5 * base:
            failures.append(f"neighbor_stability:{name}")
    if "minimum_test_calendar_days_for_concentration" in failures:
        return "inconclusive", failures
    return ("pass" if not failures else "fail"), failures


def run_stage4_screening(
    config_path: str | Path,
    output_root: str | Path = "outputs/screening",
) -> Path:
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    source_dirty = bool(_git(["status", "--porcelain"]))
    config = load_screening_config(config_path)
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise ScreeningRunError("frozen CUDA device is unavailable")
    torch.manual_seed(config.seed)
    rows, dataset_manifest = _load_rows(config)
    edges = torch.tensor(config.price_bin_edges, dtype=torch.float64, device=device)
    candidates: list[dict[str, Any]] = []
    fitted: dict[str, tuple[StrategyConfig, LookupModel, dict[str, ScreeningData]]] = {}
    for strategy_path in config.strategy_configs:
        strategy = load_strategy_config(strategy_path)
        data = _strategy_data(rows, strategy, config)
        indices = chronological_market_splits(
            data.batch.market_start_s, config.train_fraction, config.validation_fraction
        )
        splits = {name: data.index(index).to(device) for name, index in indices.items()}
        model = fit_lookup_model(
            splits["train"].batch,
            edges,
            terminal_alpha=config.terminal_alpha,
            transition_alpha=config.transition_alpha,
        )
        validation = _evaluate(
            splits["validation"],
            model,
            config,
            strategy,
            config.selection_cost_per_share,
            "terminal_lookup",
        )
        summary = evaluation_summary(splits["validation"], validation)
        eligible = summary["fills"] >= config.minimum_validation_fills
        candidates.append(
            {
                "strategy_id": strategy.strategy_id,
                "strategy_config": strategy_path,
                "eligible": eligible,
                "validation": summary,
                "split_markets": {
                    name: len(item.batch) for name, item in splits.items()
                },
            }
        )
        fitted[strategy.strategy_id] = (strategy, model, splits)
    eligible_candidates = [item for item in candidates if item["eligible"]]
    run_dir = (
        Path(output_root)
        / f"{started_at.strftime('%Y%m%dT%H%M%SZ')}_{config.experiment_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    if not eligible_candidates:
        selected_id = None
        status = "inconclusive_no_candidate"
        evaluations_payload: dict[str, Any] = {}
        primary_failures = ["minimum_validation_fills"]
        decision_rows: list[dict[str, Any]] = []
    else:
        selected = min(
            eligible_candidates,
            key=lambda item: (-item["validation"]["net_pnl_usdc"], item["strategy_id"]),
        )
        selected_id = selected["strategy_id"]
        strategy, model, splits = fitted[selected_id]
        test = splits["test"]
        evaluations_payload = {}
        decision_rows = []
        primary_summary: dict[str, Any] | None = None
        for variant in ("terminal_lookup", "literal_one_step", "range_only"):
            evaluations_payload[variant] = {}
            for cost in config.cost_scenarios_per_share:
                result = _evaluate(test, model, config, strategy, cost, variant)
                key = f"{cost:.3f}"
                evaluations_payload[variant][key] = evaluation_summary(test, result)
                decision_rows.extend(
                    _decision_rows(test, result, variant=variant, cost=cost)
                )
                if (
                    variant == "terminal_lookup"
                    and cost == config.selection_cost_per_share
                ):
                    primary_summary = evaluations_payload[variant][key]
        assert primary_summary is not None
        neighbor_summaries: dict[str, dict[str, Any]] = {}
        for name, modified in (
            (
                "edge_minus",
                replace(
                    strategy,
                    minimum_net_edge=max(
                        0.0, strategy.minimum_net_edge - config.neighbor_edge_delta
                    ),
                ),
            ),
            (
                "edge_plus",
                replace(
                    strategy,
                    minimum_net_edge=strategy.minimum_net_edge
                    + config.neighbor_edge_delta,
                ),
            ),
            (
                "persistence_minus",
                replace(
                    strategy,
                    minimum_persistence=max(
                        0.0,
                        strategy.minimum_persistence
                        - config.neighbor_persistence_delta,
                    ),
                ),
            ),
            (
                "persistence_plus",
                replace(
                    strategy,
                    minimum_persistence=min(
                        1.0,
                        strategy.minimum_persistence
                        + config.neighbor_persistence_delta,
                    ),
                ),
            ),
        ):
            result = _evaluate(
                test,
                model,
                config,
                modified,
                config.selection_cost_per_share,
                "terminal_lookup",
            )
            neighbor_summaries[name] = evaluation_summary(test, result)
        evaluations_payload["neighbor_stability"] = neighbor_summaries
        status, primary_failures = _primary_gate(
            primary_summary, config, neighbor_summaries
        )
        pq.write_table(
            pa.Table.from_pylist(decision_rows),
            run_dir / "test_decisions.parquet",
            compression="zstd",
        )
    models = {
        strategy_id: {
            "edges": model.edges.detach().cpu().tolist(),
            "probability_up": model.probability_up.detach().cpu().tolist(),
            "support": model.support.detach().cpu().tolist(),
            "transition_matrix": model.transition_matrix.detach().cpu().tolist(),
            "persistence": model.persistence.detach().cpu().tolist(),
            "prior_up": float(model.prior_up.detach().cpu().item()),
        }
        for strategy_id, (_, model, _) in fitted.items()
    }
    _write_json(run_dir / "models.json", models)
    effective_config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    _write_json(run_dir / "effective_config.json", effective_config)
    result = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "status": status,
        "primary_failures": primary_failures,
        "selected_strategy_id": selected_id,
        "selection_rule": "max_validation_net_pnl_terminal_lookup_at_0.01_with_minimum_fills",
        "candidates": candidates,
        "test_evaluations": evaluations_payload,
    }
    _write_json(run_dir / "result.json", result)
    artifact_names = ["effective_config.json", "models.json", "result.json"]
    if (run_dir / "test_decisions.parquet").is_file():
        artifact_names.append("test_decisions.parquet")
    record = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "runtime_seconds": time.monotonic() - started_monotonic,
        "source_commit": source_commit,
        "source_dirty": source_dirty,
        "config_path": str(config_path),
        "config_sha256": config.config_sha256,
        "dataset_manifest_sha256": _sha256(
            Path(config.data_root) / "pmxt_dataset_manifest.json"
        ),
        "dataset_market_count": dataset_manifest["market_count"],
        "dataset_valid_count": dataset_manifest["valid_count"],
        "seed": config.seed,
        "device": str(device),
        "dtype": config.dtype,
        "torch_version": torch.__version__,
        "duckdb_version": duckdb.__version__,
        "pyarrow_version": pa.__version__,
        "cuda_version": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(device) if device.type == "cuda" else None,
        "python": platform.python_version(),
        "strategy_configs": [
            {"path": path, "sha256": _sha256(Path(path))}
            for path in config.strategy_configs
        ],
        "artifacts": [
            {
                "path": name,
                "bytes": (run_dir / name).stat().st_size,
                "sha256": _sha256(run_dir / name),
            }
            for name in artifact_names
        ],
    }
    _write_json(run_dir / "run_record.json", record)
    return run_dir
