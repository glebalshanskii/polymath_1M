from __future__ import annotations

import csv
import hashlib
import json
import platform
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import plotly
import plotly.graph_objects as go
import torch
from plotly.subplots import make_subplots

from .positive_retest import _git, _sha256


class EdgeDiagnosticError(RuntimeError):
    """The frozen Stage 4k edge diagnostic cannot be executed."""


@dataclass(frozen=True)
class EdgeDiagnosticConfig:
    experiment_id: str
    source_run_dir: Path
    source_result_sha256: str
    source_artifact_manifest_sha256: str
    source_experiment_id: str
    successful_configuration_ids: tuple[str, ...]
    final_minimum_fills: int
    cost_scenarios_per_share: tuple[float, ...]
    minimum_segment_fills: int
    minimum_transfer_fills_per_split: int
    fixed_bins: tuple[tuple[str, tuple[float, ...]], ...]
    categorical_features: tuple[str, ...]
    preserve_signals_and_fills: bool
    config_sha256: str


@dataclass(frozen=True)
class TradeLedger:
    config_id: str
    condition_ids: tuple[str, ...]
    splits: tuple[str, ...]
    assets: tuple[str, ...]
    sides: tuple[str, ...]
    state_bins: tuple[str, ...]
    numeric: dict[str, torch.Tensor]

    @property
    def fills(self) -> int:
        return len(self.condition_ids)


EXPECTED_SUCCESSFUL_IDS = (
    "c2_stage4c_btc",
    "c3_stage4c_eth",
    "c4_stage4c_sol",
    "c5_stage4c_xrp",
    "c7_stage4d_btc_m0",
    "c8_stage4d_sol",
    "c9_stage4d_pooled",
)
EXPECTED_NUMERIC_FEATURES = (
    "signal_ask",
    "forecast_probability",
    "net_edge",
    "persistence",
    "support",
    "signed_move",
    "utc_hour",
)
EXPECTED_CATEGORICAL_FEATURES = ("asset", "side", "state_bin")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_edge_diagnostic_config(path: str | Path) -> EdgeDiagnosticConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "experiment_id",
        "source_run_dir",
        "source_result_sha256",
        "source_artifact_manifest_sha256",
        "source_experiment_id",
        "successful_configuration_ids",
        "success_rule",
        "cost_scenarios_per_share",
        "minimum_segment_fills",
        "minimum_transfer_fills_per_split",
        "fixed_bins",
        "categorical_features",
        "preserve_signals_and_fills",
    }
    if payload.keys() != expected or payload["schema_version"] != 1:
        raise EdgeDiagnosticError("Stage 4k config fields or schema differ")
    success_rule = payload["success_rule"]
    expected_rule = {
        "development_stress_pnl_strictly_positive": True,
        "final_minimum_fills": 20,
        "final_primary_pnl_strictly_positive": True,
        "final_profit_factor_strictly_above_one": True,
        "combine": "or",
    }
    if success_rule != expected_rule:
        raise EdgeDiagnosticError("Stage 4k success rule differs")
    bins_payload = payload["fixed_bins"]
    if tuple(bins_payload) != EXPECTED_NUMERIC_FEATURES:
        raise EdgeDiagnosticError("Stage 4k numeric features differ")
    fixed_bins = tuple(
        (str(name), tuple(float(value) for value in values))
        for name, values in bins_payload.items()
    )
    for name, edges in fixed_bins:
        if len(edges) < 2 or any(left >= right for left, right in pairwise(edges)):
            raise EdgeDiagnosticError(f"invalid Stage 4k bin edges: {name}")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    config = EdgeDiagnosticConfig(
        experiment_id=str(payload["experiment_id"]),
        source_run_dir=Path(payload["source_run_dir"]),
        source_result_sha256=str(payload["source_result_sha256"]),
        source_artifact_manifest_sha256=str(payload["source_artifact_manifest_sha256"]),
        source_experiment_id=str(payload["source_experiment_id"]),
        successful_configuration_ids=tuple(
            str(value) for value in payload["successful_configuration_ids"]
        ),
        final_minimum_fills=int(success_rule["final_minimum_fills"]),
        cost_scenarios_per_share=tuple(
            float(value) for value in payload["cost_scenarios_per_share"]
        ),
        minimum_segment_fills=int(payload["minimum_segment_fills"]),
        minimum_transfer_fills_per_split=int(
            payload["minimum_transfer_fills_per_split"]
        ),
        fixed_bins=fixed_bins,
        categorical_features=tuple(
            str(value) for value in payload["categorical_features"]
        ),
        preserve_signals_and_fills=bool(payload["preserve_signals_and_fills"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )
    if (
        config.experiment_id != "stage4k_edge_anti_edge_diagnostic"
        or config.source_experiment_id
        != "stage4i_pmxt_positive_retest_min20_20260422_20260518"
        or config.successful_configuration_ids != EXPECTED_SUCCESSFUL_IDS
        or config.cost_scenarios_per_share != (0.0, 0.01, 0.02)
        or config.minimum_segment_fills != 20
        or config.minimum_transfer_fills_per_split != 10
        or config.categorical_features != EXPECTED_CATEGORICAL_FEATURES
        or not config.preserve_signals_and_fills
        or len(config.source_result_sha256) != 64
        or len(config.source_artifact_manifest_sha256) != 64
    ):
        raise EdgeDiagnosticError("Stage 4k frozen contract differs")
    return config


def select_successful_configuration_ids(
    source_results: Sequence[dict[str, Any]], minimum_final_fills: int
) -> tuple[str, ...]:
    selected = []
    for item in source_results:
        development = item["development"]
        final_period = item["final_period"]
        final_pf = final_period["profit_factor"]
        dev_success = float(development["stress_2c_net_pnl_usdc"]) > 0
        final_success = (
            int(final_period["fills"]) >= minimum_final_fills
            and float(final_period["net_pnl_usdc"]) > 0
            and final_pf is not None
            and float(final_pf) > 1
        )
        if dev_success or final_success:
            selected.append(str(item["config_id"]))
    return tuple(selected)


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _tensor(rows: Sequence[dict[str, str]], field: str) -> torch.Tensor:
    return torch.tensor([float(row[field]) for row in rows], dtype=torch.float64)


def build_trade_ledger(rows: Sequence[dict[str, str]]) -> TradeLedger:
    filled = [row for row in rows if row["filled"] == "True"]
    if not filled:
        raise EdgeDiagnosticError("Stage 4k requires at least one filled row")
    config_ids = {row["config_id"] for row in filled}
    if len(config_ids) != 1:
        raise EdgeDiagnosticError("one Stage 4k ledger must contain one config")
    side_sign = torch.tensor(
        [1.0 if row["side"] == "UP" else -1.0 for row in filled],
        dtype=torch.float64,
    )
    current_mid = _tensor(filled, "current_mid_up")
    previous_mid = _tensor(filled, "previous_mid_up")
    decision_s = _tensor(filled, "decision_s")
    gross = _tensor(filled, "gross_pnl")
    fee = _tensor(filled, "platform_fee")
    shares = _tensor(filled, "fill_shares")
    primary = _tensor(filled, "net_pnl")
    stress = _tensor(filled, "stress_2c_net_pnl")
    extra = _tensor(filled, "extra_cost")
    zero = gross - fee
    if bool(torch.any(shares <= 0)):
        raise EdgeDiagnosticError("Stage 4k filled shares must be positive")
    if not torch.allclose(zero, primary + extra, atol=1e-10, rtol=0):
        raise EdgeDiagnosticError("Stage 4k zero/primary identity failed")
    if not torch.allclose(zero, stress + 0.02 * shares, atol=1e-10, rtol=0):
        raise EdgeDiagnosticError("Stage 4k zero/stress identity failed")
    numeric = {
        "gross_pnl": gross,
        "platform_fee": fee,
        "fill_shares": shares,
        "fill_cost": _tensor(filled, "fill_cost"),
        "zero_cost_pnl": zero,
        "primary_1c_pnl": primary,
        "stress_2c_pnl": stress,
        "outcome_side": _tensor(filled, "outcome_side"),
        "signal_ask": _tensor(filled, "signal_ask"),
        "fill_vwap": _tensor(filled, "fill_vwap"),
        "forecast_probability": _tensor(filled, "forecast_probability"),
        "net_edge": _tensor(filled, "net_edge"),
        "persistence": _tensor(filled, "persistence"),
        "support": _tensor(filled, "support"),
        "signed_move": (current_mid - previous_mid) * side_sign,
        "utc_hour": torch.remainder(decision_s, 86400.0) / 3600.0,
    }
    return TradeLedger(
        config_id=next(iter(config_ids)),
        condition_ids=tuple(row["condition_id"] for row in filled),
        splits=tuple(row["split"] for row in filled),
        assets=tuple(row["asset"] for row in filled),
        sides=tuple(row["side"] for row in filled),
        state_bins=tuple(row["state_bin"] for row in filled),
        numeric=numeric,
    )


def _empty_metrics() -> dict[str, Any]:
    return {
        "fills": 0,
        "shares": 0.0,
        "zero_cost_pnl_usdc": 0.0,
        "primary_1c_pnl_usdc": 0.0,
        "stress_2c_pnl_usdc": 0.0,
        "zero_cost_profit_factor": None,
        "realized_edge_cents_per_share": None,
        "model_edge_cents_per_share": None,
        "forecast_error_cents_per_share": None,
        "win_rate": None,
        "average_fill_vwap": None,
        "average_forecast_probability": None,
    }


def summarize_ledger(
    ledger: TradeLedger, mask: torch.Tensor | None = None
) -> dict[str, Any]:
    if mask is None:
        mask = torch.ones(ledger.fills, dtype=torch.bool)
    if mask.dtype != torch.bool or mask.shape != (ledger.fills,):
        raise EdgeDiagnosticError("Stage 4k summary mask differs")
    fills = int(mask.sum().item())
    if fills == 0:
        return _empty_metrics()
    numeric = ledger.numeric
    shares = numeric["fill_shares"][mask]
    total_shares = shares.sum()
    zero = numeric["zero_cost_pnl"][mask]
    primary = numeric["primary_1c_pnl"][mask]
    stress = numeric["stress_2c_pnl"][mask]
    positive = zero[zero > 0].sum()
    negative = -zero[zero < 0].sum()
    profit_factor = None
    if negative > 0:
        profit_factor = float((positive / negative).item())
    elif positive > 0:
        profit_factor = "inf"
    fill_cost = numeric["fill_cost"][mask]
    fee = numeric["platform_fee"][mask]
    forecast = numeric["forecast_probability"][mask]
    model_pnl = forecast.mul(shares).sum() - fill_cost.sum() - fee.sum()
    zero_total = zero.sum()
    realized_edge = 100.0 * zero_total / total_shares
    model_edge = 100.0 * model_pnl / total_shares
    return {
        "fills": fills,
        "shares": float(total_shares.item()),
        "zero_cost_pnl_usdc": float(zero_total.item()),
        "primary_1c_pnl_usdc": float(primary.sum().item()),
        "stress_2c_pnl_usdc": float(stress.sum().item()),
        "zero_cost_profit_factor": profit_factor,
        "realized_edge_cents_per_share": float(realized_edge.item()),
        "model_edge_cents_per_share": float(model_edge.item()),
        "forecast_error_cents_per_share": float((realized_edge - model_edge).item()),
        "win_rate": float(numeric["outcome_side"][mask].mean().item()),
        "average_fill_vwap": float((fill_cost.sum() / total_shares).item()),
        "average_forecast_probability": float(
            (forecast.mul(shares).sum() / total_shares).item()
        ),
    }


def summarize_filled_rows(rows: Sequence[dict[str, str]]) -> dict[str, Any]:
    return summarize_ledger(build_trade_ledger(rows))


def _string_mask(values: Sequence[str], expected: str) -> torch.Tensor:
    return torch.tensor([value == expected for value in values], dtype=torch.bool)


def _split_mask(ledger: TradeLedger, split: str) -> torch.Tensor:
    return _string_mask(ledger.splits, split)


def _bucket_label(left: float, right: float, last: bool) -> str:
    closing = "]" if last else ")"
    return f"[{left:g},{right:g}{closing}"


def _segment_masks(
    ledger: TradeLedger, config: EdgeDiagnosticConfig
) -> list[tuple[str, str, torch.Tensor]]:
    segments: list[tuple[str, str, torch.Tensor]] = []
    categorical = {
        "asset": ledger.assets,
        "side": ledger.sides,
        "state_bin": ledger.state_bins,
    }
    for feature in config.categorical_features:
        values = categorical[feature]
        categories = sorted(
            set(values),
            key=lambda value: int(value) if feature == "state_bin" else value,
        )
        for category in categories:
            segments.append((feature, category, _string_mask(values, category)))
    for feature, edges in config.fixed_bins:
        values = ledger.numeric[feature]
        for index, (left, right) in enumerate(pairwise(edges)):
            mask = values.ge(left)
            mask &= values.le(right) if index == len(edges) - 2 else values.lt(right)
            if bool(mask.any()):
                segments.append(
                    (
                        feature,
                        _bucket_label(left, right, index == len(edges) - 2),
                        mask,
                    )
                )
    return segments


def _summarize_segments(
    ledger: TradeLedger, config: EdgeDiagnosticConfig
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    development_mask = _split_mask(ledger, "development_validation")
    final_mask = _split_mask(ledger, "final_period")
    segments = []
    for feature, bucket, mask in _segment_masks(ledger, config):
        overall = summarize_ledger(ledger, mask)
        development = summarize_ledger(ledger, mask & development_mask)
        final_period = summarize_ledger(ledger, mask & final_mask)
        transfer = None
        if (
            development["fills"] >= config.minimum_transfer_fills_per_split
            and final_period["fills"] >= config.minimum_transfer_fills_per_split
        ):
            transfer = (
                final_period["realized_edge_cents_per_share"]
                - development["realized_edge_cents_per_share"]
            )
        segments.append(
            {
                "feature": feature,
                "bucket": bucket,
                "overall": overall,
                "development": development,
                "final_period": final_period,
                "final_minus_dev_edge_cents_per_share": transfer,
            }
        )
    bucket_counts: dict[str, int] = {}
    for segment in segments:
        bucket_counts[segment["feature"]] = bucket_counts.get(segment["feature"], 0) + 1
    candidates = [
        segment
        for segment in segments
        if segment["overall"]["fills"] >= config.minimum_segment_fills
        and segment["overall"]["fills"] < ledger.fills
        and bucket_counts[segment["feature"]] >= 2
    ]
    positive = [
        item for item in candidates if item["overall"]["zero_cost_pnl_usdc"] > 0
    ]
    negative = [
        item for item in candidates if item["overall"]["zero_cost_pnl_usdc"] < 0
    ]
    edge = max(
        positive, key=lambda item: item["overall"]["zero_cost_pnl_usdc"], default=None
    )
    anti_edge = min(
        negative, key=lambda item: item["overall"]["zero_cost_pnl_usdc"], default=None
    )
    transfer_candidates = [
        item
        for item in candidates
        if item["final_minus_dev_edge_cents_per_share"] is not None
    ]
    worst_transfer = min(
        transfer_candidates,
        key=lambda item: item["final_minus_dev_edge_cents_per_share"],
        default=None,
    )
    return segments, edge, anti_edge, worst_transfer


def _validate_source(config: EdgeDiagnosticConfig) -> dict[str, dict[str, Any]]:
    result_path = config.source_run_dir / "result.json"
    manifest_path = config.source_run_dir / "artifact_manifest.json"
    if _sha256(result_path) != config.source_result_sha256:
        raise EdgeDiagnosticError("Stage 4k source result differs")
    if _sha256(manifest_path) != config.source_artifact_manifest_sha256:
        raise EdgeDiagnosticError("Stage 4k source artifact manifest differs")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("experiment_id") != config.source_experiment_id:
        raise EdgeDiagnosticError("Stage 4k source experiment differs")
    source_results = result.get("results", [])
    selected = select_successful_configuration_ids(
        source_results, config.final_minimum_fills
    )
    if selected != config.successful_configuration_ids:
        raise EdgeDiagnosticError(
            f"Stage 4k selected configuration set differs: {selected}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = {item["path"]: item for item in manifest.get("artifacts", [])}
    for config_id in selected:
        relative = f"{config_id}/decisions.csv"
        path = config.source_run_dir / relative
        record = records.get(relative)
        if (
            record is None
            or path.stat().st_size != int(record["bytes"])
            or _sha256(path) != record["sha256"]
        ):
            raise EdgeDiagnosticError(f"Stage 4k source artifact differs: {relative}")
    return {str(item["config_id"]): item for item in source_results}


def _source_qualification(item: dict[str, Any], minimum_final_fills: int) -> str:
    development = item["development"]
    final_period = item["final_period"]
    dev = float(development["stress_2c_net_pnl_usdc"]) > 0
    final_pf = final_period["profit_factor"]
    final = (
        int(final_period["fills"]) >= minimum_final_fills
        and float(final_period["net_pnl_usdc"]) > 0
        and final_pf is not None
        and float(final_pf) > 1
    )
    if dev and final:
        return "dev_stress_and_final"
    return "dev_stress" if dev else "final"


def _validate_split_metrics(
    metrics: dict[str, Any], source: dict[str, Any], label: str
) -> None:
    if (
        metrics["fills"] != int(source["fills"])
        or abs(metrics["primary_1c_pnl_usdc"] - float(source["net_pnl_usdc"])) > 1e-10
        or abs(metrics["stress_2c_pnl_usdc"] - float(source["stress_2c_net_pnl_usdc"]))
        > 1e-10
    ):
        raise EdgeDiagnosticError(f"Stage 4k source split differs: {label}")


def _overlaps(ledgers: Sequence[TradeLedger]) -> list[dict[str, Any]]:
    rows = []
    key_maps = []
    for ledger in ledgers:
        keys = {
            (split, condition_id, side): index
            for index, (split, condition_id, side) in enumerate(
                zip(ledger.splits, ledger.condition_ids, ledger.sides)
            )
        }
        key_maps.append((ledger, keys))
    for left_index, (left, left_keys) in enumerate(key_maps):
        for right, right_keys in key_maps[left_index + 1 :]:
            intersection = set(left_keys) & set(right_keys)
            union = set(left_keys) | set(right_keys)
            exact = 0
            for key in intersection:
                left_row = left_keys[key]
                right_row = right_keys[key]
                same = all(
                    abs(
                        float(left.numeric[field][left_row].item())
                        - float(right.numeric[field][right_row].item())
                    )
                    <= 1e-12
                    for field in (
                        "fill_vwap",
                        "fill_shares",
                        "zero_cost_pnl",
                    )
                )
                exact += int(same)
            rows.append(
                {
                    "left_config_id": left.config_id,
                    "right_config_id": right.config_id,
                    "left_fills": len(left_keys),
                    "right_fills": len(right_keys),
                    "overlap_fills": len(intersection),
                    "exact_economic_overlap_fills": exact,
                    "overlap_fraction_smaller": len(intersection)
                    / min(len(left_keys), len(right_keys)),
                    "jaccard": len(intersection) / len(union),
                }
            )
    return rows


def _compact_segment(segment: dict[str, Any] | None) -> dict[str, Any] | None:
    if segment is None:
        return None
    return {
        "feature": segment["feature"],
        "bucket": segment["bucket"],
        "overall": segment["overall"],
        "development": segment["development"],
        "final_period": segment["final_period"],
        "final_minus_dev_edge_cents_per_share": segment[
            "final_minus_dev_edge_cents_per_share"
        ],
    }


def _write_strategy_summary(path: Path, results: Sequence[dict[str, Any]]) -> None:
    fields = (
        "config_id",
        "qualification",
        "dev_fills",
        "dev_zero_cost_pnl_usdc",
        "dev_primary_1c_pnl_usdc",
        "dev_stress_2c_pnl_usdc",
        "dev_realized_edge_cents_per_share",
        "dev_model_edge_cents_per_share",
        "dev_forecast_error_cents_per_share",
        "final_fills",
        "final_zero_cost_pnl_usdc",
        "final_primary_1c_pnl_usdc",
        "final_stress_2c_pnl_usdc",
        "final_realized_edge_cents_per_share",
        "final_model_edge_cents_per_share",
        "final_forecast_error_cents_per_share",
        "edge_feature",
        "edge_bucket",
        "edge_fills",
        "edge_zero_cost_pnl_usdc",
        "edge_cents_per_share",
        "anti_edge_feature",
        "anti_edge_bucket",
        "anti_edge_fills",
        "anti_edge_zero_cost_pnl_usdc",
        "anti_edge_cents_per_share",
        "worst_transfer_feature",
        "worst_transfer_bucket",
        "worst_transfer_cents_per_share",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in results:
            dev = item["development"]
            final = item["final_period"]
            edge = item["edge_segment"]
            anti = item["anti_edge_segment"]
            transfer = item["worst_transfer_segment"]
            writer.writerow(
                {
                    "config_id": item["config_id"],
                    "qualification": item["qualification"],
                    "dev_fills": dev["fills"],
                    "dev_zero_cost_pnl_usdc": dev["zero_cost_pnl_usdc"],
                    "dev_primary_1c_pnl_usdc": dev["primary_1c_pnl_usdc"],
                    "dev_stress_2c_pnl_usdc": dev["stress_2c_pnl_usdc"],
                    "dev_realized_edge_cents_per_share": dev[
                        "realized_edge_cents_per_share"
                    ],
                    "dev_model_edge_cents_per_share": dev["model_edge_cents_per_share"],
                    "dev_forecast_error_cents_per_share": dev[
                        "forecast_error_cents_per_share"
                    ],
                    "final_fills": final["fills"],
                    "final_zero_cost_pnl_usdc": final["zero_cost_pnl_usdc"],
                    "final_primary_1c_pnl_usdc": final["primary_1c_pnl_usdc"],
                    "final_stress_2c_pnl_usdc": final["stress_2c_pnl_usdc"],
                    "final_realized_edge_cents_per_share": final[
                        "realized_edge_cents_per_share"
                    ],
                    "final_model_edge_cents_per_share": final[
                        "model_edge_cents_per_share"
                    ],
                    "final_forecast_error_cents_per_share": final[
                        "forecast_error_cents_per_share"
                    ],
                    "edge_feature": edge["feature"] if edge else None,
                    "edge_bucket": edge["bucket"] if edge else None,
                    "edge_fills": edge["overall"]["fills"] if edge else None,
                    "edge_zero_cost_pnl_usdc": edge["overall"]["zero_cost_pnl_usdc"]
                    if edge
                    else None,
                    "edge_cents_per_share": edge["overall"][
                        "realized_edge_cents_per_share"
                    ]
                    if edge
                    else None,
                    "anti_edge_feature": anti["feature"] if anti else None,
                    "anti_edge_bucket": anti["bucket"] if anti else None,
                    "anti_edge_fills": anti["overall"]["fills"] if anti else None,
                    "anti_edge_zero_cost_pnl_usdc": anti["overall"][
                        "zero_cost_pnl_usdc"
                    ]
                    if anti
                    else None,
                    "anti_edge_cents_per_share": anti["overall"][
                        "realized_edge_cents_per_share"
                    ]
                    if anti
                    else None,
                    "worst_transfer_feature": transfer["feature"] if transfer else None,
                    "worst_transfer_bucket": transfer["bucket"] if transfer else None,
                    "worst_transfer_cents_per_share": transfer[
                        "final_minus_dev_edge_cents_per_share"
                    ]
                    if transfer
                    else None,
                }
            )


def _write_segment_summary(path: Path, results: Sequence[dict[str, Any]]) -> None:
    metric_fields = (
        "fills",
        "shares",
        "zero_cost_pnl_usdc",
        "primary_1c_pnl_usdc",
        "stress_2c_pnl_usdc",
        "realized_edge_cents_per_share",
        "model_edge_cents_per_share",
        "forecast_error_cents_per_share",
        "win_rate",
        "average_fill_vwap",
    )
    fields = ["config_id", "feature", "bucket"]
    fields.extend(
        f"{split}_{metric}"
        for split in ("overall", "development", "final")
        for metric in metric_fields
    )
    fields.append("final_minus_dev_edge_cents_per_share")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in results:
            for segment in item["segments"]:
                row: dict[str, Any] = {
                    "config_id": item["config_id"],
                    "feature": segment["feature"],
                    "bucket": segment["bucket"],
                    "final_minus_dev_edge_cents_per_share": segment[
                        "final_minus_dev_edge_cents_per_share"
                    ],
                }
                for output_split, source_split in (
                    ("overall", "overall"),
                    ("development", "development"),
                    ("final", "final_period"),
                ):
                    for metric in metric_fields:
                        row[f"{output_split}_{metric}"] = segment[source_split][metric]
                writer.writerow(row)


def _write_overlap_summary(path: Path, overlaps: Sequence[dict[str, Any]]) -> None:
    fields = tuple(overlaps[0]) if overlaps else ()
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(overlaps)


def _render_diagnostics(path: Path, results: Sequence[dict[str, Any]]) -> None:
    labels = [item["config_id"] for item in results]
    figure = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=(
            "Development PnL ladder",
            "Final PnL ladder",
            "Realized intrinsic edge",
            "Forecast error: realized − model",
        ),
        vertical_spacing=0.16,
        horizontal_spacing=0.10,
    )
    scenarios = (
        ("zero_cost_pnl_usdc", "0¢", "#2ca02c"),
        ("primary_1c_pnl_usdc", "1¢", "#1f77b4"),
        ("stress_2c_pnl_usdc", "2¢", "#d62728"),
    )
    for column, split in ((1, "development"), (2, "final_period")):
        for field, name, color in scenarios:
            figure.add_trace(
                go.Bar(
                    x=labels,
                    y=[item[split][field] for item in results],
                    name=name,
                    legendgroup=name,
                    showlegend=column == 1,
                    marker_color=color,
                ),
                row=1,
                col=column,
            )
    for field, name, color in (
        ("development", "Development", "#9467bd"),
        ("final_period", "Final", "#ff7f0e"),
    ):
        figure.add_trace(
            go.Bar(
                x=labels,
                y=[item[field]["realized_edge_cents_per_share"] for item in results],
                name=f"{name} realized",
                legendgroup=f"{name} realized",
                marker_color=color,
            ),
            row=2,
            col=1,
        )
        figure.add_trace(
            go.Bar(
                x=labels,
                y=[item[field]["forecast_error_cents_per_share"] for item in results],
                name=f"{name} forecast error",
                legendgroup=f"{name} forecast error",
                marker_color=color,
                showlegend=False,
            ),
            row=2,
            col=2,
        )
    figure.update_layout(
        barmode="group",
        template="plotly_white",
        height=1000,
        title="Stage 4k PMXT edge / anti-edge diagnostic",
    )
    figure.update_yaxes(title_text="PnL, USDC", row=1, col=1)
    figure.update_yaxes(title_text="PnL, USDC", row=1, col=2)
    figure.update_yaxes(title_text="cents/share", row=2, col=1)
    figure.update_yaxes(title_text="cents/share", row=2, col=2)
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def run_stage4k_edge_diagnostic(
    config_path: str | Path,
    output_root: str | Path = "outputs/edge_diagnostics",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    source_dirty = bool(_git(["status", "--porcelain"]))
    if source_dirty:
        raise EdgeDiagnosticError("commit Stage 4k code/config before run")
    config = load_edge_diagnostic_config(config_path)
    source_by_id = _validate_source(config)
    ledgers = []
    results = []
    for config_id in config.successful_configuration_ids:
        rows = _load_rows(config.source_run_dir / config_id / "decisions.csv")
        ledger = build_trade_ledger(rows)
        if ledger.config_id != config_id:
            raise EdgeDiagnosticError("Stage 4k ledger config differs")
        ledgers.append(ledger)
        development = summarize_ledger(
            ledger, _split_mask(ledger, "development_validation")
        )
        final_period = summarize_ledger(ledger, _split_mask(ledger, "final_period"))
        _validate_split_metrics(
            development, source_by_id[config_id]["development"], f"{config_id}/dev"
        )
        _validate_split_metrics(
            final_period, source_by_id[config_id]["final_period"], f"{config_id}/final"
        )
        segments, edge, anti_edge, worst_transfer = _summarize_segments(ledger, config)
        results.append(
            {
                "config_id": config_id,
                "qualification": _source_qualification(
                    source_by_id[config_id], config.final_minimum_fills
                ),
                "development": development,
                "final_period": final_period,
                "edge_segment": _compact_segment(edge),
                "anti_edge_segment": _compact_segment(anti_edge),
                "worst_transfer_segment": _compact_segment(worst_transfer),
                "segments": segments,
            }
        )
    overlaps = _overlaps(ledgers)
    run_dir = Path(output_root) / (
        f"{started.strftime('%Y%m%dT%H%M%SZ')}_{config.experiment_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    result = {
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "source_commit": source_commit,
        "config_path": str(config_path),
        "config_sha256": config.config_sha256,
        "source_run_dir": str(config.source_run_dir),
        "source_result_sha256": config.source_result_sha256,
        "source_artifact_manifest_sha256": config.source_artifact_manifest_sha256,
        "successful_configuration_ids": list(config.successful_configuration_ids),
        "minimum_segment_fills": config.minimum_segment_fills,
        "minimum_transfer_fills_per_split": config.minimum_transfer_fills_per_split,
        "results": results,
        "overlaps": overlaps,
    }
    _write_json(run_dir / "result.json", result)
    _write_strategy_summary(run_dir / "strategy_summary.csv", results)
    _write_segment_summary(run_dir / "segment_summary.csv", results)
    _write_overlap_summary(run_dir / "overlap_summary.csv", overlaps)
    _render_diagnostics(run_dir / "edge_anti_edge.html", results)
    _write_json(
        run_dir / "run_record.json",
        {
            "schema_version": 1,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_seconds": time.monotonic() - started_monotonic,
            "source_commit": source_commit,
            "source_dirty": source_dirty,
            "config_sha256": config.config_sha256,
            "device": "cpu",
            "dtype": "float64",
            "torch_version": torch.__version__,
            "plotly_version": plotly.__version__,
            "python": platform.python_version(),
        },
    )
    artifacts = []
    for artifact in sorted(run_dir.iterdir()):
        if artifact.is_file() and artifact.name != "artifact_manifest.json":
            artifacts.append(
                {
                    "path": artifact.name,
                    "bytes": artifact.stat().st_size,
                    "sha256": _sha256(artifact),
                }
            )
    _write_json(
        run_dir / "artifact_manifest.json",
        {"schema_version": 1, "artifacts": artifacts},
    )
    return run_dir
