from __future__ import annotations

import csv
import json
import math
import platform
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib
import torch

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from polymath_1M.strategy.model import (
    Evaluation,
    LookupModel,
    evaluate_batch,
    fit_lookup_model,
)
from polymath_1M.strategy.parameters import StrategyConfig, load_strategy_config

from .calibration import _git, _sha256, _write_json
from .config import load_screening_config
from .kacho_gamma import load_kacho_gamma_data, select_strategy_data
from .plateau_config import PlateauConfig, load_plateau_config
from .run import ScreeningData
from .walkforward import _timestamp, _window


class CapitalChartError(RuntimeError):
    """The Stage 4d capital chart cannot reproduce its selected proposal."""


@dataclass(frozen=True)
class CapitalLedger:
    trade_pnl: torch.Tensor
    position_outlay: torch.Tensor
    capital_before: torch.Tensor
    trade_pnl_fraction: torch.Tensor
    cumulative_pnl: torch.Tensor
    cumulative_pnl_fraction: torch.Tensor
    equity_after: torch.Tensor
    equity_peak: torch.Tensor
    drawdown: torch.Tensor
    drawdown_fraction: torch.Tensor
    position_fraction: torch.Tensor

    def __len__(self) -> int:
        return self.trade_pnl.numel()


@dataclass(frozen=True)
class ReplayTrades:
    condition_ids: tuple[str, ...]
    fold_ids: tuple[str, ...]
    market_start_s: torch.Tensor
    sides: torch.Tensor
    fill_cost: torch.Tensor
    platform_fee: torch.Tensor
    modeled_extra_cost: torch.Tensor
    ledger: CapitalLedger


def _capital_ledger(
    trade_pnl: torch.Tensor,
    position_outlay: torch.Tensor,
    starting_capital: float,
) -> CapitalLedger:
    if (
        trade_pnl.dtype != torch.float64
        or position_outlay.dtype != torch.float64
        or trade_pnl.ndim != 1
        or trade_pnl.shape != position_outlay.shape
        or trade_pnl.numel() == 0
    ):
        raise CapitalChartError("ledger inputs must be nonempty float64 vectors")
    if not math.isfinite(starting_capital) or starting_capital <= 0:
        raise CapitalChartError("starting capital must be finite and positive")
    if bool((~torch.isfinite(trade_pnl)).any().item()) or bool(
        ((~torch.isfinite(position_outlay)) | (position_outlay <= 0)).any().item()
    ):
        raise CapitalChartError("ledger contains invalid PnL or position values")
    cumulative_pnl = torch.cumsum(trade_pnl, dim=0)
    zero = torch.zeros(1, dtype=torch.float64, device=trade_pnl.device)
    capital_before = starting_capital + torch.cat((zero, cumulative_pnl[:-1]))
    if bool((capital_before <= 0).any().item()):
        raise CapitalChartError("strategy exhausted the available capital")
    if bool((position_outlay > capital_before).any().item()):
        raise CapitalChartError("position exceeds available capital")
    equity_after = starting_capital + cumulative_pnl
    equity_peak = torch.cummax(
        torch.cat(
            (
                torch.tensor(
                    [starting_capital], dtype=torch.float64, device=trade_pnl.device
                ),
                equity_after,
            )
        ),
        dim=0,
    ).values[1:]
    drawdown = equity_peak - equity_after
    return CapitalLedger(
        trade_pnl=trade_pnl,
        position_outlay=position_outlay,
        capital_before=capital_before,
        trade_pnl_fraction=trade_pnl / capital_before,
        cumulative_pnl=cumulative_pnl,
        cumulative_pnl_fraction=cumulative_pnl / starting_capital,
        equity_after=equity_after,
        equity_peak=equity_peak,
        drawdown=drawdown,
        drawdown_fraction=drawdown / equity_peak,
        position_fraction=position_outlay / capital_before,
    )


def _load_proposal(path: str | Path, config: PlateauConfig) -> dict[str, Any]:
    proposal_path = Path(path)
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    if proposal.get("status") != "selected" or not isinstance(
        proposal.get("selected"), dict
    ):
        raise CapitalChartError("proposal has no selected development candidate")
    if proposal.get("config_sha256") != config.config_sha256:
        raise CapitalChartError("proposal and Stage 4d config hashes differ")
    if proposal.get("holdout_rows_loaded") != 0:
        raise CapitalChartError("proposal does not prove zero loaded holdout rows")
    return proposal


def _evaluate_selected(
    validation: ScreeningData,
    model: LookupModel,
    strategy: StrategyConfig,
    selected: dict[str, Any],
    config: PlateauConfig,
) -> Evaluation:
    return evaluate_batch(
        validation.batch,
        model,
        minimum_support=int(selected["minimum_support"]),
        minimum_persistence=float(selected["minimum_persistence"]),
        minimum_ask=float(selected["minimum_ask"]),
        maximum_ask=float(selected["maximum_ask"]),
        minimum_net_edge=float(selected["minimum_net_edge"]),
        target_notional_usdc=strategy.target_notional_usdc,
        platform_fee_rate=validation.fee_rate,
        platform_fee_round_decimals=5,
        extra_cost_per_share=config.selection_extra_cost_per_share,
        require_market_favorite=strategy.require_market_favorite,
    )


def _replay_trades(
    data: ScreeningData,
    strategy: StrategyConfig,
    selected: dict[str, Any],
    config: PlateauConfig,
    edges: torch.Tensor,
    terminal_alpha: float,
    transition_alpha: float,
    starting_capital: float,
    device: torch.device,
) -> ReplayTrades:
    condition_ids: list[str] = []
    fold_ids: list[str] = []
    market_starts: list[torch.Tensor] = []
    sides: list[torch.Tensor] = []
    fill_cost: list[torch.Tensor] = []
    fees: list[torch.Tensor] = []
    extra_cost: list[torch.Tensor] = []
    pnl: list[torch.Tensor] = []
    expected_folds = selected["walkforward"]["folds"]
    for fold_index, fold in enumerate(config.folds):
        train = _window(data, fold.train_start, fold.train_end_exclusive).to(device)
        validation = _window(
            data, fold.validation_start, fold.validation_end_exclusive
        ).to(device)
        model = fit_lookup_model(
            train.batch,
            edges,
            terminal_alpha=terminal_alpha,
            transition_alpha=transition_alpha,
        )
        evaluation = _evaluate_selected(
            validation, model, strategy, selected, config
        )
        filled = torch.nonzero(evaluation.filled, as_tuple=False).flatten()
        expected = expected_folds[fold_index]
        replay_pnl = float(evaluation.net_pnl[filled].sum().item())
        if (
            int(filled.numel()) != int(expected["fills"])
            or abs(replay_pnl - float(expected["net_pnl_usdc"])) > 1e-9
        ):
            raise CapitalChartError("per-fold replay differs from selected proposal")
        cpu_indices = filled.detach().cpu().tolist()
        condition_ids.extend(validation.batch.condition_ids[index] for index in cpu_indices)
        fold_ids.extend(fold.fold_id for _ in cpu_indices)
        market_starts.append(validation.batch.market_start_s[filled].detach().cpu())
        sides.append(evaluation.side[filled].detach().cpu())
        fill_cost.append(evaluation.fill_cost[filled].detach().cpu())
        fees.append(evaluation.platform_fee[filled].detach().cpu())
        extra_cost.append(evaluation.extra_cost[filled].detach().cpu())
        pnl.append(evaluation.net_pnl[filled].detach().cpu())
    starts = torch.cat(market_starts)
    order = torch.argsort(starts, stable=True)
    order_list = order.tolist()
    sorted_ids = tuple(condition_ids[index] for index in order_list)
    sorted_folds = tuple(fold_ids[index] for index in order_list)
    starts = starts[order]
    if bool((starts[1:] <= starts[:-1]).any().item()):
        raise CapitalChartError("selected trades are not strictly chronological")
    sorted_fill_cost = torch.cat(fill_cost)[order]
    sorted_fees = torch.cat(fees)[order]
    sorted_extra = torch.cat(extra_cost)[order]
    sorted_pnl = torch.cat(pnl)[order]
    position = sorted_fill_cost + sorted_fees + sorted_extra
    ledger = _capital_ledger(sorted_pnl, position, starting_capital)
    expected_total = selected["walkforward"]
    if (
        len(ledger) != int(expected_total["pooled_fills"])
        or abs(
            float(ledger.cumulative_pnl[-1].item())
            - float(expected_total["pooled_net_pnl_usdc"])
        )
        > 1e-9
    ):
        raise CapitalChartError("pooled replay differs from selected proposal")
    return ReplayTrades(
        condition_ids=sorted_ids,
        fold_ids=sorted_folds,
        market_start_s=starts,
        sides=torch.cat(sides)[order],
        fill_cost=sorted_fill_cost,
        platform_fee=sorted_fees,
        modeled_extra_cost=sorted_extra,
        ledger=ledger,
    )


def _write_ledger_csv(path: Path, replay: ReplayTrades) -> None:
    ledger = replay.ledger
    fields = (
        "step",
        "market_start_utc",
        "condition_id",
        "fold_id",
        "side",
        "fill_cost_usdc",
        "platform_fee_usdc",
        "modeled_extra_cost_usdc",
        "position_outlay_usdc",
        "available_capital_before_usdc",
        "position_fraction_of_available",
        "trade_pnl_usdc",
        "trade_pnl_fraction_of_available",
        "cumulative_pnl_usdc",
        "cumulative_pnl_fraction_of_initial",
        "equity_after_usdc",
        "equity_peak_usdc",
        "drawdown_usdc",
        "drawdown_fraction_of_peak",
    )
    tensors = {
        "fill_cost_usdc": replay.fill_cost.tolist(),
        "platform_fee_usdc": replay.platform_fee.tolist(),
        "modeled_extra_cost_usdc": replay.modeled_extra_cost.tolist(),
        "position_outlay_usdc": ledger.position_outlay.tolist(),
        "available_capital_before_usdc": ledger.capital_before.tolist(),
        "position_fraction_of_available": ledger.position_fraction.tolist(),
        "trade_pnl_usdc": ledger.trade_pnl.tolist(),
        "trade_pnl_fraction_of_available": ledger.trade_pnl_fraction.tolist(),
        "cumulative_pnl_usdc": ledger.cumulative_pnl.tolist(),
        "cumulative_pnl_fraction_of_initial": ledger.cumulative_pnl_fraction.tolist(),
        "equity_after_usdc": ledger.equity_after.tolist(),
        "equity_peak_usdc": ledger.equity_peak.tolist(),
        "drawdown_usdc": ledger.drawdown.tolist(),
        "drawdown_fraction_of_peak": ledger.drawdown_fraction.tolist(),
    }
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for index in range(len(ledger)):
            row: dict[str, Any] = {
                "step": index + 1,
                "market_start_utc": datetime.fromtimestamp(
                    int(replay.market_start_s[index].item()), tz=UTC
                ).isoformat(),
                "condition_id": replay.condition_ids[index],
                "fold_id": replay.fold_ids[index],
                "side": "UP" if int(replay.sides[index].item()) == 0 else "DOWN",
            }
            row.update({name: values[index] for name, values in tensors.items()})
            writer.writerow(row)


def _fold_boundaries(fold_ids: tuple[str, ...]) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    previous = None
    for index, fold_id in enumerate(fold_ids):
        if fold_id != previous:
            result.append((index + 1, fold_id))
            previous = fold_id
    return result


def _render_chart(
    png_path: Path,
    svg_path: Path,
    replay: ReplayTrades,
    strategy_id: str,
    starting_capital: float,
) -> None:
    ledger = replay.ledger
    steps = torch.arange(1, len(ledger) + 1).tolist()
    cumulative = ledger.cumulative_pnl.tolist()
    cumulative_pct = (100 * ledger.cumulative_pnl_fraction).tolist()
    trade_pnl = ledger.trade_pnl.tolist()
    trade_pnl_pct = (100 * ledger.trade_pnl_fraction).tolist()
    drawdown = (-ledger.drawdown).tolist()
    drawdown_pct = (-100 * ledger.drawdown_fraction).tolist()
    positions = ledger.position_outlay.tolist()
    position_pct = (100 * ledger.position_fraction).tolist()
    trade_colors = ["#16875b" if value >= 0 else "#d64b4b" for value in trade_pnl]

    matplotlib.rcParams["svg.hashsalt"] = "polymath-stage4d-capital-chart"
    plt.style.use("seaborn-v0_8-whitegrid")
    figure, axes = plt.subplots(
        4,
        1,
        figsize=(16, 14),
        sharex=True,
        gridspec_kw={"height_ratios": [1.3, 1.0, 1.0, 1.0]},
    )
    figure.suptitle(
        f"{strategy_id}: development ledger, сценарий стартового капитала "
        f"{starting_capital:,.0f} USDC",
        fontsize=17,
        fontweight="bold",
    )

    axes[0].plot(steps, cumulative, color="#1261a0", linewidth=2.0, label="Cumulative PnL")
    axes[0].fill_between(steps, cumulative, 0, color="#4e9bd1", alpha=0.2)
    axes[0].axhline(0, color="#333333", linewidth=0.8)
    axes[0].set_ylabel("PnL, USDC")
    axes[0].set_title("Накопленный PnL")
    percent_axis = axes[0].twinx()
    percent_axis.plot(steps, cumulative_pct, color="#df8b1d", linewidth=1.2, alpha=0.8)
    percent_axis.set_ylabel("к начальному капиталу, %", color="#b46e13")

    axes[1].bar(steps, trade_pnl, width=1.0, color=trade_colors, alpha=0.8)
    axes[1].axhline(0, color="#333333", linewidth=0.8)
    axes[1].set_ylabel("PnL сделки, USDC")
    axes[1].set_title("PnL каждой сделки")
    trade_percent_axis = axes[1].twinx()
    trade_percent_axis.plot(
        steps,
        trade_pnl_pct,
        color="#6b4c9a",
        linewidth=0.7,
        alpha=0.75,
        label="PnL / available capital",
    )
    trade_percent_axis.set_ylabel("от доступного капитала, %", color="#6b4c9a")

    axes[2].plot(steps, drawdown, color="#c83e4d", linewidth=1.6)
    axes[2].fill_between(steps, drawdown, 0, color="#e76f7a", alpha=0.35)
    axes[2].axhline(0, color="#333333", linewidth=0.8)
    axes[2].set_ylabel("Drawdown, USDC")
    axes[2].set_title("Просадка от предыдущего peak equity")
    drawdown_percent_axis = axes[2].twinx()
    drawdown_percent_axis.plot(
        steps, drawdown_pct, color="#7a2430", linewidth=0.8, alpha=0.8
    )
    drawdown_percent_axis.set_ylabel("от peak equity, %", color="#7a2430")

    axes[3].bar(steps, positions, width=1.0, color="#4c9f70", alpha=0.65)
    axes[3].set_ylabel("Позиция, USDC")
    axes[3].set_title("Размер исполненной позиции")
    position_percent_axis = axes[3].twinx()
    position_percent_axis.plot(
        steps, position_pct, color="#8b5e34", linewidth=1.0, alpha=0.85
    )
    position_percent_axis.set_ylabel("от доступного капитала, %", color="#8b5e34")
    axes[3].set_xlabel("Номер сделки в chronological development replay")

    for axis in axes:
        for boundary, fold_id in _fold_boundaries(replay.fold_ids):
            if boundary > 1:
                axis.axvline(boundary - 0.5, color="#555555", linestyle="--", alpha=0.45)
        axis.margins(x=0.005)
    for boundary, fold_id in _fold_boundaries(replay.fold_ids):
        axes[0].text(
            boundary,
            0.96,
            fold_id,
            transform=axes[0].get_xaxis_transform(),
            fontsize=9,
            va="top",
            ha="left",
            color="#444444",
        )

    final_pnl = float(ledger.cumulative_pnl[-1].item())
    maximum_drawdown = float(ledger.drawdown.max().item())
    maximum_drawdown_pct = float(100 * ledger.drawdown_fraction.max().item())
    figure.text(
        0.5,
        0.948,
        f"Итог: {final_pnl:+.2f} USDC ({final_pnl / starting_capital:+.2%} от "
        f"начального капитала) | Max drawdown: -{maximum_drawdown:.2f} USDC "
        f"(-{maximum_drawdown_pct:.2f}%)",
        ha="center",
        fontsize=11,
    )
    figure.text(
        0.5,
        0.012,
        "Допущение: каждая позиция освобождается до следующей сделки; фактические "
        "задержки resolution/redemption исторический источник не содержит.",
        ha="center",
        fontsize=9,
        color="#555555",
    )
    figure.tight_layout(rect=(0.02, 0.03, 0.98, 0.94))
    figure.savefig(
        png_path,
        dpi=180,
        metadata={"Software": "polymath_1M"},
    )
    figure.savefig(
        svg_path,
        metadata={"Creator": "polymath_1M", "Date": None},
    )
    plt.close(figure)


def _summary(
    replay: ReplayTrades,
    starting_capital: float,
    selected: dict[str, Any],
) -> dict[str, Any]:
    ledger = replay.ledger
    maximum_drawdown_index = int(torch.argmax(ledger.drawdown).item())
    maximum_drawdown_fraction_index = int(
        torch.argmax(ledger.drawdown_fraction).item()
    )
    best_index = int(torch.argmax(ledger.trade_pnl).item())
    worst_index = int(torch.argmin(ledger.trade_pnl).item())
    return {
        "schema_version": 1,
        "strategy_id": selected["strategy_id"],
        "starting_capital_usdc": starting_capital,
        "capital_semantics": (
            "settlement ledger: previous trade PnL is realized and position capital "
            "is released before the next selected trade"
        ),
        "capital_limitation": (
            "historical source has no actual resolution/redemption timestamps; "
            "concurrent locked capital is not estimated"
        ),
        "trades": len(ledger),
        "net_pnl_usdc": float(ledger.cumulative_pnl[-1].item()),
        "ending_capital_usdc": float(ledger.equity_after[-1].item()),
        "return_on_initial_capital": float(
            ledger.cumulative_pnl_fraction[-1].item()
        ),
        "available_capital_before_usdc": {
            "minimum": float(ledger.capital_before.min().item()),
            "mean": float(ledger.capital_before.mean().item()),
            "maximum": float(ledger.capital_before.max().item()),
        },
        "maximum_drawdown_usdc": float(ledger.drawdown[maximum_drawdown_index].item()),
        "drawdown_fraction_of_peak_at_maximum_usdc": float(
            ledger.drawdown_fraction[maximum_drawdown_index].item()
        ),
        "maximum_drawdown_step": maximum_drawdown_index + 1,
        "maximum_drawdown_market_start_utc": datetime.fromtimestamp(
            int(replay.market_start_s[maximum_drawdown_index].item()), tz=UTC
        ).isoformat(),
        "maximum_drawdown_fraction_of_peak": float(
            ledger.drawdown_fraction[maximum_drawdown_fraction_index].item()
        ),
        "maximum_drawdown_fraction_step": maximum_drawdown_fraction_index + 1,
        "position_outlay_usdc": {
            "minimum": float(ledger.position_outlay.min().item()),
            "mean": float(ledger.position_outlay.mean().item()),
            "maximum": float(ledger.position_outlay.max().item()),
        },
        "position_fraction_of_available": {
            "minimum": float(ledger.position_fraction.min().item()),
            "mean": float(ledger.position_fraction.mean().item()),
            "maximum": float(ledger.position_fraction.max().item()),
        },
        "best_trade": {
            "step": best_index + 1,
            "pnl_usdc": float(ledger.trade_pnl[best_index].item()),
            "fraction_of_available": float(
                ledger.trade_pnl_fraction[best_index].item()
            ),
        },
        "worst_trade": {
            "step": worst_index + 1,
            "pnl_usdc": float(ledger.trade_pnl[worst_index].item()),
            "fraction_of_available": float(
                ledger.trade_pnl_fraction[worst_index].item()
            ),
        },
    }


def run_stage4d_capital_chart(
    config_path: str | Path,
    proposal_path: str | Path,
    *,
    starting_capital: float,
    output_root: str | Path = "outputs/charts",
) -> Path:
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    config = load_plateau_config(config_path)
    data_config = load_screening_config(config.data_config)
    proposal = _load_proposal(proposal_path, config)
    selected = proposal["selected"]
    strategy = load_strategy_config(selected["strategy_config"])
    if strategy.strategy_id != selected["strategy_id"]:
        raise CapitalChartError("selected strategy and config IDs differ")
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise CapitalChartError("frozen CUDA device is unavailable")
    all_data, provenance = load_kacho_gamma_data(
        config,
        data_config,
        end_exclusive_s=_timestamp(config.holdout_start),
    )
    if provenance.gamma_rows_loaded != provenance.loaded_markets:
        raise CapitalChartError("Gamma/Kacho development row counts differ")
    data = select_strategy_data(all_data, strategy)
    edges = torch.tensor(data_config.price_bin_edges, dtype=torch.float64, device=device)
    replay = _replay_trades(
        data,
        strategy,
        selected,
        config,
        edges,
        data_config.terminal_alpha,
        data_config.transition_alpha,
        starting_capital,
        device,
    )
    run_dir = Path(output_root) / (
        f"{started.strftime('%Y%m%dT%H%M%SZ')}_stage4d_"
        f"{strategy.strategy_id}_capital_{starting_capital:.0f}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    ledger_path = run_dir / "ledger.csv"
    summary_path = run_dir / "summary.json"
    png_path = run_dir / "pnl_drawdown_positions.png"
    svg_path = run_dir / "pnl_drawdown_positions.svg"
    _write_ledger_csv(ledger_path, replay)
    _write_json(summary_path, _summary(replay, starting_capital, selected))
    _render_chart(png_path, svg_path, replay, strategy.strategy_id, starting_capital)
    _write_json(
        run_dir / "run_record.json",
        {
            "schema_version": 1,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "runtime_seconds": time.monotonic() - started_monotonic,
            "source_commit": _git(["rev-parse", "HEAD"]),
            "source_dirty": bool(_git(["status", "--porcelain"])),
            "config_path": str(config_path),
            "config_sha256": config.config_sha256,
            "proposal_path": str(proposal_path),
            "proposal_sha256": _sha256(Path(proposal_path)),
            "proposal_source_commit": proposal["source_commit"],
            "holdout_rows_loaded": 0,
            "holdout_opened": False,
            "starting_capital_usdc": starting_capital,
            "device": str(device),
            "dtype": config.dtype,
            "torch_version": torch.__version__,
            "matplotlib_version": matplotlib.__version__,
            "python": platform.python_version(),
            "artifacts": {
                path.name: _sha256(path)
                for path in (ledger_path, summary_path, png_path, svg_path)
            },
        },
    )
    return run_dir
