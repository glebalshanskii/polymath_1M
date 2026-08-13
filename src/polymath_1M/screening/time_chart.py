from __future__ import annotations

import csv
import math
import platform
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib
import torch
from matplotlib.colors import ListedColormap

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from polymath_1M.historical.binance import BinanceKlines, load_binance_klines
from polymath_1M.strategy.model import STATUS_NAMES, fit_lookup_model
from polymath_1M.strategy.parameters import StrategyConfig, load_strategy_config

from .calibration import _base_evaluation, _git, _sha256, _write_json
from .capital_chart import _capital_ledger, _evaluate_selected, _load_proposal
from .config import load_screening_config
from .kacho_gamma import load_kacho_gamma_data, select_strategy_data
from .plateau_config import PlateauConfig, load_plateau_config
from .run import ScreeningData
from .walkforward import _timestamp, _window


class TimeChartError(RuntimeError):
    """The Stage 4d time/signal chart cannot reproduce its source result."""


GATE_NAMES = (
    "valid snapshot",
    "support >= threshold",
    "ask in range",
    "persistence >= threshold",
    "net edge >= threshold",
    "executable top-of-book",
    "FINAL FILL",
)


@dataclass(frozen=True)
class SignalTimeline:
    condition_ids: tuple[str, ...]
    fold_ids: tuple[str, ...]
    market_start_s: torch.Tensor
    market_end_s: torch.Tensor
    decision_s: torch.Tensor
    outcome_up: torch.Tensor
    state_bin: torch.Tensor
    probability: torch.Tensor
    support: torch.Tensor
    persistence: torch.Tensor
    side: torch.Tensor
    signal_ask: torch.Tensor
    fill_vwap: torch.Tensor
    fill_shares: torch.Tensor
    fill_cost: torch.Tensor
    platform_fee: torch.Tensor
    modeled_extra_cost: torch.Tensor
    net_edge: torch.Tensor
    net_pnl: torch.Tensor
    filled: torch.Tensor
    status_code: torch.Tensor
    gates: torch.Tensor

    def __len__(self) -> int:
        return self.decision_s.numel()


def _tensor_cat(items: list[torch.Tensor]) -> torch.Tensor:
    return torch.cat([item.detach().cpu() for item in items])


def _build_timeline(
    data: ScreeningData,
    strategy: StrategyConfig,
    selected: dict[str, Any],
    config: PlateauConfig,
    edges: torch.Tensor,
    terminal_alpha: float,
    transition_alpha: float,
    device: torch.device,
) -> SignalTimeline:
    condition_ids: list[str] = []
    fold_ids: list[str] = []
    tensors: dict[str, list[torch.Tensor]] = {
        name: []
        for name in (
            "market_start_s",
            "market_end_s",
            "decision_s",
            "outcome_up",
            "state_bin",
            "probability",
            "support",
            "persistence",
            "side",
            "signal_ask",
            "fill_vwap",
            "fill_shares",
            "fill_cost",
            "platform_fee",
            "modeled_extra_cost",
            "net_edge",
            "net_pnl",
            "filled",
            "status_code",
            "gates",
        )
    }
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
        base = _base_evaluation(
            validation,
            model,
            strategy,
            cost=config.selection_extra_cost_per_share,
        )
        evaluation = _evaluate_selected(validation, model, strategy, selected, config)
        expected = expected_folds[fold_index]
        if (
            int(evaluation.filled.sum().item()) != int(expected["fills"])
            or abs(
                float(evaluation.net_pnl[evaluation.filled].sum().item())
                - float(expected["net_pnl_usdc"])
            )
            > 1e-9
        ):
            raise TimeChartError("per-fold signal replay differs from proposal")
        executable = (
            base.filled
            & torch.isfinite(base.fill_vwap)
            & (base.fill_vwap <= float(selected["maximum_ask"]))
        )
        gates = torch.stack(
            (
                validation.batch.snapshot_valid,
                base.support >= int(selected["minimum_support"]),
                (base.signal_ask >= float(selected["minimum_ask"]))
                & (base.signal_ask <= float(selected["maximum_ask"])),
                base.persistence >= float(selected["minimum_persistence"]),
                base.net_edge >= float(selected["minimum_net_edge"]),
                executable,
                evaluation.filled,
            ),
            dim=0,
        )
        reconstructed = gates[:-1].all(dim=0)
        if not torch.equal(reconstructed, evaluation.filled):
            raise TimeChartError("displayed gates do not reconstruct final fills")
        condition_ids.extend(validation.batch.condition_ids)
        fold_ids.extend(fold.fold_id for _ in range(len(validation.batch)))
        source = {
            "market_start_s": validation.batch.market_start_s,
            "market_end_s": validation.batch.market_end_s,
            "decision_s": validation.batch.decision_s,
            "outcome_up": validation.batch.outcome_up,
            "state_bin": evaluation.state_bin,
            "probability": evaluation.probability,
            "support": evaluation.support,
            "persistence": evaluation.persistence,
            "side": evaluation.side,
            "signal_ask": evaluation.signal_ask,
            "fill_vwap": evaluation.fill_vwap,
            "fill_shares": evaluation.fill_shares,
            "fill_cost": evaluation.fill_cost,
            "platform_fee": evaluation.platform_fee,
            "modeled_extra_cost": evaluation.extra_cost,
            "net_edge": evaluation.net_edge,
            "net_pnl": evaluation.net_pnl,
            "filled": evaluation.filled,
            "status_code": evaluation.status_code,
            "gates": gates.T,
        }
        for name, value in source.items():
            tensors[name].append(value)
    combined = {name: _tensor_cat(values) for name, values in tensors.items()}
    order = torch.argsort(combined["decision_s"], stable=True)
    order_list = order.tolist()
    for name, value in combined.items():
        combined[name] = value.index_select(0, order)
    decision_s = combined["decision_s"]
    if bool((decision_s[1:] <= decision_s[:-1]).any().item()):
        raise TimeChartError("validation decisions are not strictly chronological")
    sorted_conditions = tuple(condition_ids[index] for index in order_list)
    sorted_folds = tuple(fold_ids[index] for index in order_list)
    timeline = SignalTimeline(
        condition_ids=sorted_conditions,
        fold_ids=sorted_folds,
        **combined,
    )
    expected_total = selected["walkforward"]
    if (
        len(timeline) != 4_608
        or int(timeline.filled.sum().item()) != int(expected_total["pooled_fills"])
        or abs(
            float(timeline.net_pnl[timeline.filled].sum().item())
            - float(expected_total["pooled_net_pnl_usdc"])
        )
        > 1e-9
    ):
        raise TimeChartError("pooled signal replay differs from proposal")
    return timeline


def _match_decision_opens(
    decision_s: torch.Tensor, klines: BinanceKlines
) -> torch.Tensor:
    indices = torch.searchsorted(klines.open_time_s, decision_s)
    safe = torch.clamp(indices, max=len(klines) - 1)
    if bool((klines.open_time_s[safe] != decision_s).any().item()):
        raise TimeChartError("BTC context has no exact open at a decision timestamp")
    return klines.open[safe]


def _utc_datetimes(values: torch.Tensor) -> list[datetime]:
    return [datetime.fromtimestamp(int(value), tz=UTC) for value in values.tolist()]


def _trade_ledger(
    timeline: SignalTimeline, starting_capital: float
) -> tuple[torch.Tensor, Any]:
    filled = torch.nonzero(timeline.filled, as_tuple=False).flatten()
    position_outlay = timeline.fill_cost[filled] + timeline.platform_fee[filled]
    ledger = _capital_ledger(
        timeline.net_pnl[filled], position_outlay, starting_capital
    )
    return filled, ledger


def _write_signals_csv(
    path: Path,
    timeline: SignalTimeline,
    btc_at_decision: torch.Tensor,
    starting_capital: float,
) -> None:
    filled_indices, ledger = _trade_ledger(timeline, starting_capital)
    trade_lookup = {
        int(market_index): trade_index
        for trade_index, market_index in enumerate(filled_indices.tolist())
    }
    fields = (
        "market_start_utc",
        "market_end_utc",
        "decision_utc",
        "condition_id",
        "fold_id",
        "btc_usdt_proxy_open_at_decision",
        "outcome",
        "outcome_up",
        "chosen_side",
        "state_bin",
        "predicted_terminal_payout",
        "signal_ask",
        "net_edge_after_fees_and_haircut",
        "persistence",
        "support",
        "gate_snapshot_valid",
        "gate_support",
        "gate_ask_range",
        "gate_persistence",
        "gate_net_edge",
        "gate_executable_top",
        "filled",
        "status",
        "trade_step",
        "fill_vwap",
        "fill_shares",
        "fill_cost_usdc",
        "platform_fee_usdc",
        "modeled_extra_cost_usdc",
        "position_outlay_usdc",
        "position_fraction_of_available",
        "trade_pnl_usdc",
        "cumulative_pnl_usdc",
        "drawdown_usdc",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for index in range(len(timeline)):
            trade_index = trade_lookup.get(index)
            trade_values: dict[str, float | int | str] = {
                "trade_step": "",
                "position_outlay_usdc": "",
                "position_fraction_of_available": "",
                "cumulative_pnl_usdc": "",
                "drawdown_usdc": "",
            }
            if trade_index is not None:
                trade_values = {
                    "trade_step": trade_index + 1,
                    "position_outlay_usdc": float(
                        ledger.position_outlay[trade_index].item()
                    ),
                    "position_fraction_of_available": float(
                        ledger.position_fraction[trade_index].item()
                    ),
                    "cumulative_pnl_usdc": float(
                        ledger.cumulative_pnl[trade_index].item()
                    ),
                    "drawdown_usdc": float(ledger.drawdown[trade_index].item()),
                }
            gates = timeline.gates[index].tolist()
            status = int(timeline.status_code[index].item())
            writer.writerow(
                {
                    "market_start_utc": datetime.fromtimestamp(
                        int(timeline.market_start_s[index].item()), tz=UTC
                    ).isoformat(),
                    "market_end_utc": datetime.fromtimestamp(
                        int(timeline.market_end_s[index].item()), tz=UTC
                    ).isoformat(),
                    "decision_utc": datetime.fromtimestamp(
                        int(timeline.decision_s[index].item()), tz=UTC
                    ).isoformat(),
                    "condition_id": timeline.condition_ids[index],
                    "fold_id": timeline.fold_ids[index],
                    "btc_usdt_proxy_open_at_decision": float(
                        btc_at_decision[index].item()
                    ),
                    "outcome": (
                        "UP"
                        if float(timeline.outcome_up[index].item()) == 1
                        else "DOWN"
                    ),
                    "outcome_up": float(timeline.outcome_up[index].item()),
                    "chosen_side": (
                        "UP" if int(timeline.side[index].item()) == 0 else "DOWN"
                    ),
                    "state_bin": int(timeline.state_bin[index].item()),
                    "predicted_terminal_payout": float(
                        timeline.probability[index].item()
                    ),
                    "signal_ask": float(timeline.signal_ask[index].item()),
                    "net_edge_after_fees_and_haircut": float(
                        timeline.net_edge[index].item()
                    ),
                    "persistence": float(timeline.persistence[index].item()),
                    "support": int(timeline.support[index].item()),
                    "gate_snapshot_valid": gates[0],
                    "gate_support": gates[1],
                    "gate_ask_range": gates[2],
                    "gate_persistence": gates[3],
                    "gate_net_edge": gates[4],
                    "gate_executable_top": gates[5],
                    "filled": gates[6],
                    "status": STATUS_NAMES[status],
                    "fill_vwap": float(timeline.fill_vwap[index].item()),
                    "fill_shares": float(timeline.fill_shares[index].item()),
                    "fill_cost_usdc": float(timeline.fill_cost[index].item()),
                    "platform_fee_usdc": float(timeline.platform_fee[index].item()),
                    "modeled_extra_cost_usdc": float(
                        timeline.modeled_extra_cost[index].item()
                    ),
                    "trade_pnl_usdc": float(timeline.net_pnl[index].item()),
                    **trade_values,
                }
            )


def _write_btc_csv(path: Path, klines: BinanceKlines) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("open_time_utc", "open", "high", "low", "close"))
        for row in zip(
            klines.open_time_s.tolist(),
            klines.open.tolist(),
            klines.high.tolist(),
            klines.low.tolist(),
            klines.close.tolist(),
            strict=True,
        ):
            writer.writerow(
                (
                    datetime.fromtimestamp(int(row[0]), tz=UTC).isoformat(),
                    *row[1:],
                )
            )


def _add_fold_boundaries(
    axes: list[Any], config: PlateauConfig, minimum: datetime, maximum: datetime
) -> None:
    for fold in config.folds:
        start = fold.validation_start
        end = fold.validation_end_exclusive
        if start < maximum and end > minimum:
            label_time = max(start, minimum)
            for axis in axes:
                if start > minimum:
                    axis.axvline(start, color="#575757", linestyle="--", alpha=0.42)
            axes[0].text(
                label_time,
                0.97,
                fold.fold_id,
                transform=axes[0].get_xaxis_transform(),
                va="top",
                ha="left",
                fontsize=8,
                color="#444444",
            )


def _render_chart(
    png_path: Path,
    svg_path: Path,
    timeline: SignalTimeline,
    klines: BinanceKlines,
    btc_at_decision: torch.Tensor,
    selected: dict[str, Any],
    config: PlateauConfig,
    starting_capital: float,
) -> None:
    filled_indices, ledger = _trade_ledger(timeline, starting_capital)
    decision_dates = _utc_datetimes(timeline.decision_s)
    trade_dates = [decision_dates[index] for index in filled_indices.tolist()]
    btc_dates = _utc_datetimes(klines.open_time_s)
    selected_sides = timeline.side[filled_indices]
    selected_pnl = timeline.net_pnl[filled_indices]
    selected_btc = btc_at_decision[filled_indices]
    position = ledger.position_outlay
    marker_sizes = 25 + 65 * position / position.max()

    matplotlib.rcParams["svg.hashsalt"] = "polymath-stage4d-time-signal-chart"
    plt.style.use("seaborn-v0_8-whitegrid")
    figure, axes_array = plt.subplots(
        7,
        1,
        figsize=(20, 23),
        sharex=True,
        gridspec_kw={"height_ratios": [1.45, 0.42, 1.05, 1.05, 0.75, 1.15, 1.05]},
    )
    axes = list(axes_array)
    figure.suptitle(
        "Stage 4d BTC 5m — development replay по реальному UTC-времени",
        fontsize=18,
        fontweight="bold",
    )
    figure.text(
        0.5,
        0.967,
        f"235 сделок | PnL {float(ledger.cumulative_pnl[-1]):+.2f} USDC | "
        f"сценарный капитал {starting_capital:,.0f} USDC",
        ha="center",
        fontsize=11,
    )

    axes[0].plot(
        btc_dates,
        klines.close.tolist(),
        color="#304b70",
        linewidth=0.8,
        label="Binance BTCUSDT 1m close (visual proxy)",
    )
    combinations = (
        (0, True, "^", "#16875b", "UP trade, profit"),
        (0, False, "^", "#d64b4b", "UP trade, loss"),
        (1, True, "v", "#16875b", "DOWN trade, profit"),
        (1, False, "v", "#d64b4b", "DOWN trade, loss"),
    )
    for side, positive, marker, color, label in combinations:
        mask = (selected_sides == side) & ((selected_pnl >= 0) == positive)
        indices = torch.nonzero(mask, as_tuple=False).flatten().tolist()
        axes[0].scatter(
            [trade_dates[index] for index in indices],
            selected_btc[mask].tolist(),
            s=marker_sizes[mask].tolist(),
            marker=marker,
            color=color,
            edgecolor="white",
            linewidth=0.35,
            alpha=0.88,
            label=label,
            zorder=3,
        )
    axes[0].set_ylabel("BTCUSDT")
    axes[0].set_title(
        "Цена BTC и сделки — BTCUSDT только визуальный контекст, НЕ Chainlink и НЕ вход модели"
    )
    axes[0].legend(loc="upper left", ncol=3, fontsize=8)

    date_numbers = mdates.date2num(decision_dates)
    five_minutes = 5 / (24 * 60)
    outcome_and_side = torch.stack(
        (timeline.outcome_up, (timeline.side == 0).to(dtype=torch.float64)), dim=0
    )
    axes[1].imshow(
        outcome_and_side.tolist(),
        aspect="auto",
        interpolation="nearest",
        extent=(date_numbers[0], date_numbers[-1] + five_minutes, 0, 2),
        cmap=ListedColormap(["#d85858", "#31a36d"]),
        vmin=0,
        vmax=1,
        origin="upper",
    )
    axes[1].set_yticks([1.5, 0.5], ["Gamma outcome", "model side"])
    axes[1].set_title(
        "Фактический исход и выбранная моделью сторона: DOWN (red) / UP (green)"
    )
    axes[1].grid(False)

    axes[2].plot(
        decision_dates,
        timeline.probability.tolist(),
        color="#4464ad",
        linewidth=0.65,
        alpha=0.85,
        label="model expected payout, chosen side",
    )
    axes[2].plot(
        decision_dates,
        timeline.signal_ask.tolist(),
        color="#dc8a19",
        linewidth=0.55,
        alpha=0.72,
        label="ask, chosen side",
    )
    axes[2].scatter(
        trade_dates,
        timeline.probability[filled_indices].tolist(),
        s=10,
        color="#16875b",
        label="filled signal",
        zorder=3,
    )
    axes[2].axhspan(
        float(selected["minimum_ask"]),
        float(selected["maximum_ask"]),
        color="#dc8a19",
        alpha=0.08,
    )
    axes[2].set_ylim(-0.02, 1.02)
    axes[2].set_ylabel("probability / price")
    axes[2].set_title(
        "Сторона выбирается по max net edge: прогноз payout и текущий ask"
    )
    axes[2].legend(loc="upper left", ncol=3, fontsize=8)

    axes[3].plot(
        decision_dates,
        timeline.net_edge.tolist(),
        color="#6d4aa2",
        linewidth=0.65,
        label="net edge",
    )
    axes[3].axhline(
        float(selected["minimum_net_edge"]),
        color="#6d4aa2",
        linestyle="--",
        linewidth=1.0,
        label=f"edge >= {float(selected['minimum_net_edge']):.2f}",
    )
    axes[3].scatter(
        trade_dates,
        timeline.net_edge[filled_indices].tolist(),
        s=9,
        color="#16875b",
        zorder=3,
    )
    axes[3].set_ylabel("net edge")
    persistence_axis = axes[3].twinx()
    persistence_axis.plot(
        decision_dates,
        timeline.persistence.tolist(),
        color="#267a73",
        linewidth=0.55,
        alpha=0.72,
        label="persistence",
    )
    persistence_axis.axhline(
        float(selected["minimum_persistence"]),
        color="#267a73",
        linestyle=":",
        linewidth=1.0,
        label=f"persistence >= {float(selected['minimum_persistence']):.2f}",
    )
    persistence_axis.set_ylabel("persistence", color="#267a73")
    axes[3].set_title("Фильтры качества сигнала: net edge и Markov persistence")
    handles, labels = axes[3].get_legend_handles_labels()
    other_handles, other_labels = persistence_axis.get_legend_handles_labels()
    axes[3].legend(
        handles + other_handles,
        labels + other_labels,
        loc="upper left",
        ncol=4,
        fontsize=8,
    )

    axes[4].imshow(
        timeline.gates.T.to(dtype=torch.int64).tolist(),
        aspect="auto",
        interpolation="nearest",
        extent=(date_numbers[0], date_numbers[-1] + five_minutes, len(GATE_NAMES), 0),
        cmap=ListedColormap(["#eadfda", "#2a78a5"]),
        vmin=0,
        vmax=1,
    )
    axes[4].set_yticks(
        [index + 0.5 for index in range(len(GATE_NAMES))], GATE_NAMES, fontsize=8
    )
    axes[4].set_title(
        "Воронка решения: blue = gate passed; FINAL FILL = все условия одновременно"
    )
    axes[4].grid(False)

    trade_pnl = ledger.trade_pnl
    axes[5].plot(
        trade_dates,
        ledger.cumulative_pnl.tolist(),
        color="#1261a0",
        linewidth=1.8,
        label="cumulative modeled PnL",
    )
    axes[5].fill_between(
        trade_dates, ledger.cumulative_pnl.tolist(), 0, color="#4e9bd1", alpha=0.18
    )
    axes[5].axhline(0, color="#333333", linewidth=0.8)
    axes[5].set_ylabel("PnL, USDC")
    drawdown_axis = axes[5].twinx()
    drawdown_axis.plot(
        trade_dates,
        (-ledger.drawdown).tolist(),
        color="#c83e4d",
        linewidth=1.0,
        label="drawdown",
    )
    drawdown_axis.set_ylabel("drawdown, USDC", color="#a72f3c")
    axes[5].set_title(
        "PnL и просадка по времени решения (PnL известен после settlement)"
    )
    handles, labels = axes[5].get_legend_handles_labels()
    other_handles, other_labels = drawdown_axis.get_legend_handles_labels()
    axes[5].legend(
        handles + other_handles, labels + other_labels, loc="upper left", fontsize=8
    )

    colors = ["#16875b" if value >= 0 else "#d64b4b" for value in trade_pnl]
    axes[6].scatter(
        trade_dates,
        ledger.position_outlay.tolist(),
        s=marker_sizes.tolist(),
        color=colors,
        alpha=0.72,
        edgecolor="white",
        linewidth=0.3,
        label="cash outlay; color = PnL sign",
    )
    axes[6].axhline(
        float(selected["target_notional_usdc"]),
        color="#555555",
        linestyle="--",
        linewidth=0.9,
        label="10 USDC target notional",
    )
    axes[6].set_ylabel("position outlay, USDC")
    position_axis = axes[6].twinx()
    position_axis.plot(
        trade_dates,
        (100 * ledger.position_fraction).tolist(),
        color="#8b5e34",
        linewidth=0.7,
        alpha=0.8,
        label="outlay / available capital",
    )
    position_axis.set_ylabel("of available capital, %", color="#8b5e34")
    axes[6].set_title(
        "Размер позиции: fixed target 10 USDC, фактический размер ограничен top-of-book liquidity"
    )
    handles, labels = axes[6].get_legend_handles_labels()
    other_handles, other_labels = position_axis.get_legend_handles_labels()
    axes[6].legend(
        handles + other_handles, labels + other_labels, loc="upper left", fontsize=8
    )

    minimum = decision_dates[0]
    maximum = decision_dates[-1]
    _add_fold_boundaries(axes, config, minimum, maximum)
    locator = mdates.AutoDateLocator(minticks=8, maxticks=18, tz=UTC)
    axes[-1].xaxis.set_major_locator(locator)
    axes[-1].xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=UTC))
    axes[-1].set_xlabel("UTC time")
    for axis in axes:
        axis.set_xlim(minimum, maximum)
        axis.margins(x=0)
    figure.text(
        0.5,
        0.008,
        "BTCUSDT — visualization-only proxy; authoritative UP/DOWN labels come from Gamma/Polymarket rules. "
        "Размер позиции не зависит от probability/edge: target fixed at 10 USDC; outlay = fill cost + fee.",
        ha="center",
        fontsize=9,
        color="#555555",
    )
    figure.tight_layout(rect=(0.025, 0.025, 0.985, 0.955))
    figure.savefig(png_path, dpi=180, metadata={"Software": "polymath_1M"})
    figure.savefig(svg_path, metadata={"Creator": "polymath_1M", "Date": None})
    plt.close(figure)


def _summary(
    timeline: SignalTimeline,
    selected: dict[str, Any],
    starting_capital: float,
    btc_provenance: dict[str, Any],
) -> dict[str, Any]:
    filled_indices, ledger = _trade_ledger(timeline, starting_capital)
    status_counts = torch.bincount(
        timeline.status_code, minlength=len(STATUS_NAMES)
    ).tolist()
    return {
        "schema_version": 1,
        "strategy_id": selected["strategy_id"],
        "period": {
            "first_decision_utc": datetime.fromtimestamp(
                int(timeline.decision_s[0].item()), tz=UTC
            ).isoformat(),
            "last_decision_utc": datetime.fromtimestamp(
                int(timeline.decision_s[-1].item()), tz=UTC
            ).isoformat(),
        },
        "development_markets": len(timeline),
        "trades": int(filled_indices.numel()),
        "net_pnl_usdc": float(ledger.cumulative_pnl[-1].item()),
        "maximum_drawdown_usdc": float(ledger.drawdown.max().item()),
        "starting_capital_usdc": starting_capital,
        "return_on_scenario_initial_capital": float(
            ledger.cumulative_pnl[-1].item() / starting_capital
        ),
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
        "side_rule": "choose UP or DOWN with maximum net edge",
        "sizing_rule": (
            "fixed 10 USDC target notional; actual top-of-book fill cost plus "
            "platform fee; no scaling by probability, edge, persistence or support"
        ),
        "thresholds": {
            key: selected[key]
            for key in (
                "minimum_support",
                "minimum_ask",
                "maximum_ask",
                "minimum_net_edge",
                "minimum_persistence",
                "target_notional_usdc",
            )
        },
        "gate_pass_counts": {
            name: int(timeline.gates[:, index].sum().item())
            for index, name in enumerate(GATE_NAMES)
        },
        "status_counts": {
            name: int(status_counts[index]) for index, name in enumerate(STATUS_NAMES)
        },
        "outcome_source": "Gamma authoritative binary outcome for each BTC 5m market",
        "btc_price_context": {
            **btc_provenance,
            "semantic": (
                "visualization-only Binance BTCUSDT proxy; not Chainlink resolution "
                "price and not used by the strategy"
            ),
        },
        "holdout_rows_loaded": 0,
        "holdout_opened": False,
    }


def run_stage4d_time_chart(
    config_path: str | Path,
    proposal_path: str | Path,
    binance_config_path: str | Path,
    *,
    starting_capital: float,
    data_root: str | Path = "data/historical",
    output_root: str | Path = "outputs/charts",
) -> Path:
    if not math.isfinite(starting_capital) or starting_capital <= 0:
        raise TimeChartError("starting capital must be finite and positive")
    started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    config = load_plateau_config(config_path)
    data_config = load_screening_config(config.data_config)
    proposal = _load_proposal(proposal_path, config)
    selected = proposal["selected"]
    strategy = load_strategy_config(selected["strategy_config"])
    if strategy.strategy_id != selected["strategy_id"] or strategy.assets != ("BTC",):
        raise TimeChartError("time chart requires the selected BTC-only strategy")
    device = torch.device(config.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise TimeChartError("frozen CUDA device is unavailable")
    development_end_s = _timestamp(config.holdout_start)
    all_data, market_provenance = load_kacho_gamma_data(
        config, data_config, end_exclusive_s=development_end_s
    )
    if market_provenance.gamma_rows_loaded != market_provenance.loaded_markets:
        raise TimeChartError("Gamma/Kacho development row counts differ")
    data = select_strategy_data(all_data, strategy)
    edges = torch.tensor(
        data_config.price_bin_edges, dtype=torch.float64, device=device
    )
    timeline = _build_timeline(
        data,
        strategy,
        selected,
        config,
        edges,
        data_config.terminal_alpha,
        data_config.transition_alpha,
        device,
    )
    context_start_s = int(config.folds[0].validation_start.timestamp())
    klines, btc_provenance = load_binance_klines(
        binance_config_path,
        data_root,
        start_s=context_start_s,
        end_exclusive_s=development_end_s,
    )
    expected_minutes = (development_end_s - context_start_s) // 60
    if len(klines) != expected_minutes:
        raise TimeChartError("BTC visualization context is not minute-complete")
    btc_at_decision = _match_decision_opens(timeline.decision_s, klines)
    run_dir = Path(output_root) / (
        f"{started.strftime('%Y%m%dT%H%M%SZ')}_stage4d_"
        f"{strategy.strategy_id}_time_signals"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    signals_path = run_dir / "signals.csv"
    btc_path = run_dir / "btc_context.csv"
    summary_path = run_dir / "summary.json"
    png_path = run_dir / "time_pnl_btc_outcomes_signals.png"
    svg_path = run_dir / "time_pnl_btc_outcomes_signals.svg"
    _write_signals_csv(signals_path, timeline, btc_at_decision, starting_capital)
    _write_btc_csv(btc_path, klines)
    _write_json(
        summary_path,
        _summary(timeline, selected, starting_capital, btc_provenance),
    )
    _render_chart(
        png_path,
        svg_path,
        timeline,
        klines,
        btc_at_decision,
        selected,
        config,
        starting_capital,
    )
    artifacts = (signals_path, btc_path, summary_path, png_path, svg_path)
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
            "binance_config_path": str(binance_config_path),
            "binance_config_sha256": btc_provenance["config_sha256"],
            "market_data_provenance": market_provenance.__dict__,
            "holdout_rows_loaded": 0,
            "holdout_opened": False,
            "starting_capital_usdc": starting_capital,
            "device": str(device),
            "dtype": config.dtype,
            "torch_version": torch.__version__,
            "matplotlib_version": matplotlib.__version__,
            "python": platform.python_version(),
            "artifacts": {path.name: _sha256(path) for path in artifacts},
        },
    )
    return run_dir
