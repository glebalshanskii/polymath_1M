from __future__ import annotations

import csv
import hashlib
import json
import platform
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import plotly
import plotly.graph_objects as go
import torch
from plotly.subplots import make_subplots

from .positive_retest import EXPECTED_CONFIG_IDS, _git, _sha256


class CostSensitivityError(RuntimeError):
    """The Stage 4j same-fill cost sensitivity cannot be executed."""


@dataclass(frozen=True)
class CostSensitivityConfig:
    experiment_id: str
    source_run_dir: Path
    source_result_sha256: str
    source_artifact_manifest_sha256: str
    source_experiment_id: str
    configuration_ids: tuple[str, ...]
    minimum_final_fills: int
    extra_cost_per_share: float
    preserve_platform_fee: bool
    preserve_signals_and_fills: bool
    config_sha256: str


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_cost_sensitivity_config(path: str | Path) -> CostSensitivityConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "experiment_id",
        "source_run_dir",
        "source_result_sha256",
        "source_artifact_manifest_sha256",
        "source_experiment_id",
        "configuration_ids",
        "minimum_final_fills",
        "extra_cost_per_share",
        "preserve_platform_fee",
        "preserve_signals_and_fills",
    }
    if payload.keys() != expected or payload["schema_version"] != 1:
        raise CostSensitivityError("Stage 4j config fields or schema differ")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    config = CostSensitivityConfig(
        experiment_id=str(payload["experiment_id"]),
        source_run_dir=Path(payload["source_run_dir"]),
        source_result_sha256=str(payload["source_result_sha256"]),
        source_artifact_manifest_sha256=str(payload["source_artifact_manifest_sha256"]),
        source_experiment_id=str(payload["source_experiment_id"]),
        configuration_ids=tuple(str(value) for value in payload["configuration_ids"]),
        minimum_final_fills=int(payload["minimum_final_fills"]),
        extra_cost_per_share=float(payload["extra_cost_per_share"]),
        preserve_platform_fee=bool(payload["preserve_platform_fee"]),
        preserve_signals_and_fills=bool(payload["preserve_signals_and_fills"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )
    if (
        config.experiment_id != "stage4j_same_fill_zero_extra_cost"
        or config.source_experiment_id
        != "stage4i_pmxt_positive_retest_min20_20260422_20260518"
        or config.configuration_ids != EXPECTED_CONFIG_IDS
        or config.minimum_final_fills != 20
        or config.extra_cost_per_share != 0.0
        or not config.preserve_platform_fee
        or not config.preserve_signals_and_fills
        or len(config.source_result_sha256) != 64
        or len(config.source_artifact_manifest_sha256) != 64
    ):
        raise CostSensitivityError("Stage 4j frozen contract differs")
    return config


def summarize_zero_extra_cost(rows: Sequence[dict[str, str]]) -> dict[str, Any]:
    filled = [row for row in rows if row["filled"] == "True"]
    if not filled:
        return {
            "fills": 0,
            "zero_extra_cost_pnl_usdc": 0.0,
            "primary_1c_pnl_usdc": 0.0,
            "stress_2c_pnl_usdc": 0.0,
            "profit_factor": None,
            "profit_factor_infinite": False,
            "cash_turnover_usdc": 0.0,
            "return_on_turnover": None,
        }

    def tensor(name: str) -> torch.Tensor:
        return torch.tensor([float(row[name]) for row in filled], dtype=torch.float64)

    gross = tensor("gross_pnl")
    fee = tensor("platform_fee")
    shares = tensor("fill_shares")
    fill_cost = tensor("fill_cost")
    extra = tensor("extra_cost")
    primary = tensor("net_pnl")
    stress = tensor("stress_2c_net_pnl")
    zero_cost = gross - fee
    if not torch.allclose(zero_cost, primary + extra, atol=1e-12, rtol=0):
        raise CostSensitivityError("zero-cost versus primary identity failed")
    if not torch.allclose(zero_cost, stress + 0.02 * shares, atol=1e-12, rtol=0):
        raise CostSensitivityError("zero-cost versus stress identity failed")
    positive = zero_cost[zero_cost > 0].sum()
    negative = -zero_cost[zero_cost < 0].sum()
    infinite = bool(negative == 0 and positive > 0)
    profit_factor = None
    if negative > 0:
        profit_factor = float((positive / negative).item())
    turnover = fill_cost.sum() + fee.sum()
    return {
        "fills": len(filled),
        "zero_extra_cost_pnl_usdc": float(zero_cost.sum().item()),
        "primary_1c_pnl_usdc": float(primary.sum().item()),
        "stress_2c_pnl_usdc": float(stress.sum().item()),
        "profit_factor": profit_factor,
        "profit_factor_infinite": infinite,
        "cash_turnover_usdc": float(turnover.item()),
        "return_on_turnover": float((zero_cost.sum() / turnover).item()),
    }


def _profit_factor_above_one(summary: dict[str, Any]) -> bool:
    return bool(summary["profit_factor_infinite"]) or bool(
        summary["profit_factor"] is not None and summary["profit_factor"] > 1
    )


def _load_decisions(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _validate_source_artifacts(config: CostSensitivityConfig) -> None:
    manifest_path = config.source_run_dir / "artifact_manifest.json"
    if _sha256(manifest_path) != config.source_artifact_manifest_sha256:
        raise CostSensitivityError("Stage 4j source artifact manifest differs")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = {item["path"]: item for item in manifest.get("artifacts", [])}
    for config_id in config.configuration_ids:
        relative = f"{config_id}/decisions.csv"
        record = records.get(relative)
        path = config.source_run_dir / relative
        if (
            record is None
            or path.stat().st_size != int(record["bytes"])
            or _sha256(path) != record["sha256"]
        ):
            raise CostSensitivityError(f"Stage 4j source artifact differs: {relative}")


def _validate_source_summary(
    recalculated: dict[str, Any], source: dict[str, Any], split: str
) -> None:
    expected_primary = float(source["net_pnl_usdc"])
    expected_stress = float(source["stress_2c_net_pnl_usdc"])
    if (
        recalculated["fills"] != int(source["fills"])
        or abs(recalculated["primary_1c_pnl_usdc"] - expected_primary) > 1e-10
        or abs(recalculated["stress_2c_pnl_usdc"] - expected_stress) > 1e-10
    ):
        raise CostSensitivityError(f"source ledger mismatch for {split}")


def _render_comparison(path: Path, results: Sequence[dict[str, Any]]) -> None:
    labels = [item["config_id"] for item in results]
    figure = make_subplots(
        rows=2,
        cols=1,
        subplot_titles=("Development PnL", "Final PnL"),
        vertical_spacing=0.14,
    )
    for row_index, split in ((1, "development"), (2, "final_period")):
        for key, name, color in (
            ("zero_extra_cost_pnl_usdc", "0¢ extra", "#2ca02c"),
            ("primary_1c_pnl_usdc", "1¢ primary", "#1f77b4"),
            ("stress_2c_pnl_usdc", "2¢ stress", "#d62728"),
        ):
            figure.add_trace(
                go.Bar(
                    x=labels,
                    y=[item[split][key] for item in results],
                    name=name,
                    legendgroup=name,
                    showlegend=row_index == 1,
                    marker_color=color,
                ),
                row=row_index,
                col=1,
            )
    figure.update_layout(
        barmode="group",
        template="plotly_white",
        height=900,
        title="Stage 4j same-fill cost sensitivity",
        yaxis_title="PnL, USDC",
        yaxis2_title="PnL, USDC",
    )
    figure.write_html(path, include_plotlyjs=True, full_html=True)


def run_stage4j_zero_cost(
    config_path: str | Path,
    output_root: str | Path = "outputs/cost_sensitivity",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    source_commit = _git(["rev-parse", "HEAD"])
    source_dirty = bool(_git(["status", "--porcelain"]))
    if source_dirty:
        raise CostSensitivityError("commit Stage 4j code/config before run")
    config = load_cost_sensitivity_config(config_path)
    _validate_source_artifacts(config)
    source_result_path = config.source_run_dir / "result.json"
    if _sha256(source_result_path) != config.source_result_sha256:
        raise CostSensitivityError("Stage 4j source result SHA-256 differs")
    source_result = json.loads(source_result_path.read_text(encoding="utf-8"))
    if source_result.get("experiment_id") != config.source_experiment_id:
        raise CostSensitivityError("Stage 4j source experiment differs")
    source_by_id = {
        item["config_id"]: item for item in source_result.get("results", [])
    }
    if tuple(source_by_id) != config.configuration_ids:
        raise CostSensitivityError("Stage 4j source configuration order differs")

    results: list[dict[str, Any]] = []
    for config_id in config.configuration_ids:
        decisions_path = config.source_run_dir / config_id / "decisions.csv"
        rows = _load_decisions(decisions_path)
        development_rows = [
            row for row in rows if row["split"] == "development_validation"
        ]
        final_rows = [row for row in rows if row["split"] == "final_period"]
        development = summarize_zero_extra_cost(development_rows)
        final_period = summarize_zero_extra_cost(final_rows)
        source_item = source_by_id[config_id]
        _validate_source_summary(development, source_item["development"], "development")
        _validate_source_summary(final_period, source_item["final_period"], "final")
        fold_pnl = []
        for fold_id in ("fold_1", "fold_2", "fold_3", "fold_4"):
            fold = summarize_zero_extra_cost(
                [row for row in development_rows if row["fold_id"] == fold_id]
            )
            fold_pnl.append(
                {
                    "fold_id": fold_id,
                    "zero_extra_cost_pnl_usdc": fold["zero_extra_cost_pnl_usdc"],
                }
            )
        positive_folds = sum(item["zero_extra_cost_pnl_usdc"] > 0 for item in fold_pnl)
        dev_zero_cost_positive = development["zero_extra_cost_pnl_usdc"] > 0
        development_stable = (
            dev_zero_cost_positive
            and _profit_factor_above_one(development)
            and positive_folds >= 3
        )
        final_positive = (
            final_period["fills"] >= config.minimum_final_fills
            and final_period["zero_extra_cost_pnl_usdc"] > 0
            and _profit_factor_above_one(final_period)
        )
        results.append(
            {
                "config_id": config_id,
                "development": development,
                "development_folds": fold_pnl,
                "positive_development_folds": positive_folds,
                "final_period": final_period,
                "gates": {
                    "dev_zero_cost_positive": dev_zero_cost_positive,
                    "development_stable": development_stable,
                    "final_positive": final_positive,
                },
                "successful_literal_or": dev_zero_cost_positive or final_positive,
                "successful_stable_or": development_stable or final_positive,
            }
        )

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
        "scenario": {
            "extra_cost_per_share": config.extra_cost_per_share,
            "preserve_platform_fee": config.preserve_platform_fee,
            "preserve_signals_and_fills": config.preserve_signals_and_fills,
            "minimum_final_fills": config.minimum_final_fills,
        },
        "results": results,
        "successful_literal_or": [
            item["config_id"] for item in results if item["successful_literal_or"]
        ],
        "successful_stable_or": [
            item["config_id"] for item in results if item["successful_stable_or"]
        ],
    }
    _write_json(run_dir / "result.json", result)
    with (run_dir / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        fields = (
            "config_id",
            "dev_fills",
            "dev_zero_cost_pnl_usdc",
            "dev_profit_factor",
            "positive_development_folds",
            "final_fills",
            "final_zero_cost_pnl_usdc",
            "final_profit_factor",
            "successful_literal_or",
            "successful_stable_or",
        )
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in results:
            writer.writerow(
                {
                    "config_id": item["config_id"],
                    "dev_fills": item["development"]["fills"],
                    "dev_zero_cost_pnl_usdc": item["development"][
                        "zero_extra_cost_pnl_usdc"
                    ],
                    "dev_profit_factor": item["development"]["profit_factor"],
                    "positive_development_folds": item["positive_development_folds"],
                    "final_fills": item["final_period"]["fills"],
                    "final_zero_cost_pnl_usdc": item["final_period"][
                        "zero_extra_cost_pnl_usdc"
                    ],
                    "final_profit_factor": item["final_period"]["profit_factor"],
                    "successful_literal_or": item["successful_literal_or"],
                    "successful_stable_or": item["successful_stable_or"],
                }
            )
    _render_comparison(run_dir / "cost_comparison.html", results)
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
    for path in sorted(run_dir.iterdir()):
        if path.is_file() and path.name != "artifact_manifest.json":
            artifacts.append(
                {
                    "path": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    _write_json(
        run_dir / "artifact_manifest.json",
        {"schema_version": 1, "artifacts": artifacts},
    )
    return run_dir
