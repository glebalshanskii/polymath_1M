from __future__ import annotations

import csv
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

from polymath_1M.historical.binance import BinanceKlines, load_binance_klines
from polymath_1M.historical.polymarket_chainlink import (
    PolymarketChainlinkSeries,
    load_polymarket_chainlink_series,
)
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


def _match_chainlink_values(
    decision_s: torch.Tensor, series: PolymarketChainlinkSeries
) -> torch.Tensor:
    indices = torch.searchsorted(series.timestamp_s, decision_s)
    safe = torch.clamp(indices, max=len(series) - 1)
    if bool((series.timestamp_s[safe] != decision_s).any().item()):
        raise TimeChartError(
            "Polymarket Chainlink context has no exact decision timestamp"
        )
    return series.value[safe]


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
    chainlink_at_decision: torch.Tensor,
    binance_at_decision: torch.Tensor,
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
        "polymarket_chainlink_btcusd_at_decision",
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
                    "polymarket_chainlink_btcusd_at_decision": float(
                        chainlink_at_decision[index].item()
                    ),
                    "btc_usdt_proxy_open_at_decision": float(
                        binance_at_decision[index].item()
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


def _write_binance_csv(path: Path, klines: BinanceKlines) -> None:
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


def _write_chainlink_csv(path: Path, series: PolymarketChainlinkSeries) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("timestamp_utc", "btc_usd"))
        for timestamp, value in zip(
            series.timestamp_s.tolist(), series.value.tolist(), strict=True
        ):
            writer.writerow(
                (datetime.fromtimestamp(int(timestamp), tz=UTC).isoformat(), value)
            )


def _render_chart(
    html_path: Path,
    timeline: SignalTimeline,
    klines: BinanceKlines,
    chainlink: PolymarketChainlinkSeries,
    chainlink_at_decision: torch.Tensor,
    binance_at_decision: torch.Tensor,
    selected: dict[str, Any],
    config: PlateauConfig,
    starting_capital: float,
) -> None:
    filled_indices, ledger = _trade_ledger(timeline, starting_capital)
    decision_dates = _utc_datetimes(timeline.decision_s)
    trade_dates = [decision_dates[index] for index in filled_indices.tolist()]
    chainlink_dates = _utc_datetimes(chainlink.timestamp_s)
    binance_dates = _utc_datetimes(klines.open_time_s)
    selected_sides = timeline.side[filled_indices]
    selected_pnl = timeline.net_pnl[filled_indices]
    position = ledger.position_outlay
    marker_sizes = 7 + 10 * position / position.max()

    subplot_titles = (
        "Polymarket Chainlink-family BTC/USD history — ряд интерфейса Polymarket",
        "Binance BTCUSDT — независимый spot-контекст для сравнения",
        "Фактический Gamma outcome и выбранная моделью сторона",
        "Выбор стороны: model expected payout и текущий Polymarket token ask",
        "Фильтры сигнала: net edge и Markov persistence",
        "Воронка решения: blue = gate passed; FINAL FILL = все условия",
        "PnL и drawdown по времени решения (PnL известен после settlement)",
        "Размер позиции: fixed target 10 USDC, фактический outlay по liquidity",
    )
    figure = make_subplots(
        rows=8,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.025,
        row_heights=[0.14, 0.14, 0.055, 0.12, 0.12, 0.105, 0.15, 0.15],
        specs=[
            [{}],
            [{}],
            [{}],
            [{}],
            [{"secondary_y": True}],
            [{}],
            [{"secondary_y": True}],
            [{"secondary_y": True}],
        ],
        subplot_titles=subplot_titles,
    )
    figure.add_trace(
        go.Scattergl(
            x=chainlink_dates,
            y=chainlink.value.tolist(),
            mode="lines",
            line={"color": "#1261a0", "width": 1.2},
            name="Polymarket Chainlink BTC/USD 1m",
            hovertemplate="%{x|%Y-%m-%d %H:%M:%S UTC}<br>BTC/USD %{y:,.2f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scattergl(
            x=binance_dates,
            y=klines.close.tolist(),
            mode="lines",
            line={"color": "#805e3b", "width": 1.0},
            name="Binance BTCUSDT 1m close",
            hovertemplate="%{x|%Y-%m-%d %H:%M:%S UTC}<br>BTCUSDT %{y:,.2f}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    combinations = (
        (0, True, "triangle-up", "#16875b", "UP trade, profit"),
        (0, False, "triangle-up", "#d64b4b", "UP trade, loss"),
        (1, True, "triangle-down", "#16875b", "DOWN trade, profit"),
        (1, False, "triangle-down", "#d64b4b", "DOWN trade, loss"),
    )
    trade_rows = filled_indices.tolist()
    for side, positive, symbol, color, label in combinations:
        mask = (selected_sides == side) & ((selected_pnl >= 0) == positive)
        subset = torch.nonzero(mask, as_tuple=False).flatten().tolist()
        market_rows = [trade_rows[index] for index in subset]
        customdata = [
            [
                timeline.condition_ids[market_index],
                timeline.fold_ids[market_index],
                "UP" if side == 0 else "DOWN",
                float(timeline.net_pnl[market_index].item()),
                float(ledger.position_outlay[trade_index].item()),
            ]
            for trade_index, market_index in zip(subset, market_rows, strict=True)
        ]
        for chart_row, values, show_legend in (
            (1, chainlink_at_decision[filled_indices][mask], True),
            (2, binance_at_decision[filled_indices][mask], False),
        ):
            figure.add_trace(
                go.Scattergl(
                    x=[trade_dates[index] for index in subset],
                    y=values.tolist(),
                    mode="markers",
                    marker={
                        "size": marker_sizes[mask].tolist(),
                        "symbol": symbol,
                        "color": color,
                        "line": {"color": "white", "width": 0.5},
                        "opacity": 0.88,
                    },
                    customdata=customdata,
                    name=label,
                    legendgroup=label,
                    showlegend=show_legend,
                    hovertemplate=(
                        "%{x|%Y-%m-%d %H:%M UTC}<br>price %{y:,.2f}"
                        "<br>%{customdata[2]} | PnL %{customdata[3]:+.3f} USDC"
                        "<br>outlay %{customdata[4]:.3f} USDC"
                        "<br>%{customdata[1]}<br>%{customdata[0]}<extra></extra>"
                    ),
                ),
                row=chart_row,
                col=1,
            )

    outcome_and_side = torch.stack(
        (timeline.outcome_up, (timeline.side == 0).to(dtype=torch.float64)), dim=0
    )
    figure.add_trace(
        go.Heatmap(
            x=decision_dates,
            y=["Gamma outcome", "model side"],
            z=outcome_and_side.tolist(),
            zmin=0,
            zmax=1,
            colorscale=[
                [0, "#d85858"],
                [0.499, "#d85858"],
                [0.5, "#31a36d"],
                [1, "#31a36d"],
            ],
            showscale=False,
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>%{y}: %{z}<extra>0=DOWN, 1=UP</extra>",
        ),
        row=3,
        col=1,
    )
    figure.add_trace(
        go.Scattergl(
            x=decision_dates,
            y=timeline.probability.tolist(),
            mode="lines",
            line={"color": "#4464ad", "width": 1.0},
            name="model expected payout",
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>payout %{y:.4f}<extra></extra>",
        ),
        row=4,
        col=1,
    )
    figure.add_trace(
        go.Scattergl(
            x=decision_dates,
            y=timeline.signal_ask.tolist(),
            mode="lines",
            line={"color": "#dc8a19", "width": 0.8},
            name="chosen-side ask",
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>ask %{y:.4f}<extra></extra>",
        ),
        row=4,
        col=1,
    )
    figure.add_trace(
        go.Scattergl(
            x=trade_dates,
            y=timeline.probability[filled_indices].tolist(),
            mode="markers",
            marker={"size": 5, "color": "#16875b"},
            name="filled signal",
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>payout %{y:.4f}<extra></extra>",
        ),
        row=4,
        col=1,
    )
    figure.add_hrect(
        y0=float(selected["minimum_ask"]),
        y1=float(selected["maximum_ask"]),
        fillcolor="#dc8a19",
        opacity=0.08,
        line_width=0,
        row=4,
        col=1,
    )
    figure.add_trace(
        go.Scattergl(
            x=decision_dates,
            y=timeline.net_edge.tolist(),
            mode="lines",
            line={"color": "#6d4aa2", "width": 1.0},
            name="net edge",
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>edge %{y:+.4f}<extra></extra>",
        ),
        row=5,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=[decision_dates[0], decision_dates[-1]],
            y=[float(selected["minimum_net_edge"])] * 2,
            mode="lines",
            line={"color": "#6d4aa2", "width": 1, "dash": "dash"},
            name=f"edge ≥ {float(selected['minimum_net_edge']):.2f}",
            hoverinfo="skip",
        ),
        row=5,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scattergl(
            x=decision_dates,
            y=timeline.persistence.tolist(),
            mode="lines",
            line={"color": "#267a73", "width": 0.8},
            name="persistence",
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>persistence %{y:.4f}<extra></extra>",
        ),
        row=5,
        col=1,
        secondary_y=True,
    )
    figure.add_trace(
        go.Scatter(
            x=[decision_dates[0], decision_dates[-1]],
            y=[float(selected["minimum_persistence"])] * 2,
            mode="lines",
            line={"color": "#267a73", "width": 1, "dash": "dot"},
            name=f"persistence ≥ {float(selected['minimum_persistence']):.2f}",
            hoverinfo="skip",
        ),
        row=5,
        col=1,
        secondary_y=True,
    )
    figure.add_trace(
        go.Heatmap(
            x=decision_dates,
            y=list(GATE_NAMES),
            z=timeline.gates.T.to(dtype=torch.int64).tolist(),
            zmin=0,
            zmax=1,
            colorscale=[
                [0, "#eadfda"],
                [0.499, "#eadfda"],
                [0.5, "#2a78a5"],
                [1, "#2a78a5"],
            ],
            showscale=False,
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>%{y}: %{z}<extra>1=passed</extra>",
        ),
        row=6,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=trade_dates,
            y=ledger.cumulative_pnl.tolist(),
            mode="lines",
            fill="tozeroy",
            fillcolor="rgba(78,155,209,0.18)",
            line={"color": "#1261a0", "width": 2},
            name="cumulative modeled PnL",
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>PnL %{y:+.3f} USDC<extra></extra>",
        ),
        row=7,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=trade_dates,
            y=(-ledger.drawdown).tolist(),
            mode="lines",
            line={"color": "#c83e4d", "width": 1.3},
            name="drawdown",
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>drawdown %{y:.3f} USDC<extra></extra>",
        ),
        row=7,
        col=1,
        secondary_y=True,
    )
    pnl_colors = ["#16875b" if value >= 0 else "#d64b4b" for value in ledger.trade_pnl]
    figure.add_trace(
        go.Scattergl(
            x=trade_dates,
            y=ledger.position_outlay.tolist(),
            mode="markers",
            marker={
                "size": marker_sizes.tolist(),
                "color": pnl_colors,
                "line": {"color": "white", "width": 0.5},
                "opacity": 0.82,
            },
            customdata=torch.stack(
                (ledger.trade_pnl, 100 * ledger.position_fraction), dim=1
            ).tolist(),
            name="cash position outlay",
            hovertemplate=(
                "%{x|%Y-%m-%d %H:%M UTC}<br>outlay %{y:.4f} USDC"
                "<br>PnL %{customdata[0]:+.4f} USDC"
                "<br>%{customdata[1]:.4f}% available capital<extra></extra>"
            ),
        ),
        row=8,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=[decision_dates[0], decision_dates[-1]],
            y=[float(selected["target_notional_usdc"])] * 2,
            mode="lines",
            line={"color": "#555555", "width": 1, "dash": "dash"},
            name="10 USDC target notional",
            hoverinfo="skip",
        ),
        row=8,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=trade_dates,
            y=(100 * ledger.position_fraction).tolist(),
            mode="lines",
            line={"color": "#8b5e34", "width": 1},
            name="outlay / available capital",
            hovertemplate="%{x|%Y-%m-%d %H:%M UTC}<br>%{y:.4f}%<extra></extra>",
        ),
        row=8,
        col=1,
        secondary_y=True,
    )

    minimum = decision_dates[0]
    maximum = decision_dates[-1]
    for fold in config.folds:
        if minimum < fold.validation_start < maximum:
            for row in range(1, 9):
                figure.add_vline(
                    x=fold.validation_start,
                    line={"color": "#575757", "width": 1, "dash": "dash"},
                    opacity=0.45,
                    row=row,
                    col=1,
                )
        if fold.validation_start < maximum and fold.validation_end_exclusive > minimum:
            figure.add_annotation(
                x=max(fold.validation_start, minimum),
                y=0.998,
                xref="x",
                yref="paper",
                text=fold.fold_id,
                showarrow=False,
                xanchor="left",
                yanchor="top",
                font={"size": 11, "color": "#444444"},
            )
    figure.update_yaxes(title_text="BTC/USD", row=1, col=1)
    figure.update_yaxes(title_text="BTCUSDT", row=2, col=1)
    figure.update_yaxes(title_text="outcome / side", row=3, col=1)
    figure.update_yaxes(
        title_text="probability / ask", range=[-0.02, 1.02], row=4, col=1
    )
    figure.update_yaxes(title_text="net edge", row=5, col=1, secondary_y=False)
    figure.update_yaxes(title_text="persistence", row=5, col=1, secondary_y=True)
    figure.update_yaxes(title_text="gate", row=6, col=1)
    figure.update_yaxes(title_text="PnL, USDC", row=7, col=1, secondary_y=False)
    figure.update_yaxes(title_text="drawdown, USDC", row=7, col=1, secondary_y=True)
    figure.update_yaxes(title_text="outlay, USDC", row=8, col=1, secondary_y=False)
    figure.update_yaxes(
        title_text="available capital, %", row=8, col=1, secondary_y=True
    )
    figure.update_xaxes(
        type="date",
        range=[minimum, maximum],
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikethickness=1,
    )
    figure.update_xaxes(title_text="UTC time", row=8, col=1)
    figure.update_layout(
        title={
            "text": (
                "Stage 4d BTC 5m — development replay по UTC"
                f"<br><sup>{len(ledger)} сделок | PnL "
                f"{float(ledger.cumulative_pnl[-1]):+.2f} USDC | "
                f"сценарный капитал {starting_capital:,.0f} USDC</sup>"
            ),
            "x": 0.5,
            "xanchor": "center",
            "y": 0.995,
            "yanchor": "top",
        },
        template="plotly_white",
        height=2_650,
        autosize=True,
        hovermode="x unified",
        margin={"l": 100, "r": 100, "t": 200, "b": 120},
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.005,
            "xanchor": "left",
            "x": 0,
            "font": {"size": 10},
        },
    )
    figure.add_annotation(
        x=0.5,
        y=-0.035,
        xref="paper",
        yref="paper",
        text=(
            "Обе price series — visual context и не входят в текущую модель. "
            "Polymarket Chainlink history — minute frontend proxy, не raw RTDS/signed report. "
            "Position target fixed at 10 USDC; outlay = fill cost + fee."
        ),
        showarrow=False,
        font={"size": 11, "color": "#555555"},
    )
    figure.write_html(
        str(html_path),
        include_plotlyjs=True,
        full_html=True,
        auto_open=False,
        config={
            "displaylogo": False,
            "responsive": True,
            "scrollZoom": True,
            "toImageButtonOptions": {
                "format": "png",
                "filename": "stage4d_btc_dual_price_signals",
                "width": 1_800,
                "height": 2_650,
                "scale": 1,
            },
        },
    )


def _summary(
    timeline: SignalTimeline,
    selected: dict[str, Any],
    starting_capital: float,
    chainlink_provenance: dict[str, Any],
    binance_provenance: dict[str, Any],
    chainlink_at_decision: torch.Tensor,
    binance_at_decision: torch.Tensor,
) -> dict[str, Any]:
    filled_indices, ledger = _trade_ledger(timeline, starting_capital)
    status_counts = torch.bincount(
        timeline.status_code, minlength=len(STATUS_NAMES)
    ).tolist()
    return {
        "schema_version": 2,
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
            "polymarket_chainlink": {
                **_compact_context_provenance(chainlink_provenance),
                "semantic": (
                    "visualization-only minute Polymarket frontend history from "
                    "the Chainlink price family; not raw RTDS or a signed report"
                ),
            },
            "binance": {
                **_compact_context_provenance(binance_provenance),
                "semantic": (
                    "visualization-only Binance BTCUSDT independent spot proxy"
                ),
            },
            "both_are_strategy_inputs": False,
            "chainlink_minus_binance_at_decision": {
                "minimum": float(
                    (chainlink_at_decision - binance_at_decision).min().item()
                ),
                "mean": float(
                    (chainlink_at_decision - binance_at_decision).mean().item()
                ),
                "maximum": float(
                    (chainlink_at_decision - binance_at_decision).max().item()
                ),
            },
        },
        "holdout_rows_loaded": 0,
        "holdout_opened": False,
    }


def _compact_context_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    """Keep audit anchors without duplicating every raw-file record."""
    return {key: value for key, value in provenance.items() if key != "files"}


def run_stage4d_time_chart(
    config_path: str | Path,
    proposal_path: str | Path,
    binance_config_path: str | Path,
    chainlink_config_path: str | Path,
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
    klines, binance_provenance = load_binance_klines(
        binance_config_path,
        data_root,
        start_s=context_start_s,
        end_exclusive_s=development_end_s,
    )
    expected_minutes = (development_end_s - context_start_s) // 60
    if len(klines) != expected_minutes:
        raise TimeChartError("Binance visualization context is not minute-complete")
    chainlink, chainlink_provenance = load_polymarket_chainlink_series(
        chainlink_config_path, data_root
    )
    if len(chainlink) != expected_minutes:
        raise TimeChartError(
            "Polymarket Chainlink visualization context is not minute-complete"
        )
    binance_at_decision = _match_decision_opens(timeline.decision_s, klines)
    chainlink_at_decision = _match_chainlink_values(timeline.decision_s, chainlink)
    run_dir = Path(output_root) / (
        f"{started.strftime('%Y%m%dT%H%M%SZ')}_stage4d_"
        f"{strategy.strategy_id}_plotly_dual_price"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    signals_path = run_dir / "signals.csv"
    binance_path = run_dir / "binance_btcusdt_context.csv"
    chainlink_path = run_dir / "polymarket_chainlink_btcusd_context.csv"
    summary_path = run_dir / "summary.json"
    html_path = run_dir / "time_pnl_dual_price_outcomes_signals.html"
    _write_signals_csv(
        signals_path,
        timeline,
        chainlink_at_decision,
        binance_at_decision,
        starting_capital,
    )
    _write_binance_csv(binance_path, klines)
    _write_chainlink_csv(chainlink_path, chainlink)
    _write_json(
        summary_path,
        _summary(
            timeline,
            selected,
            starting_capital,
            chainlink_provenance,
            binance_provenance,
            chainlink_at_decision,
            binance_at_decision,
        ),
    )
    _render_chart(
        html_path,
        timeline,
        klines,
        chainlink,
        chainlink_at_decision,
        binance_at_decision,
        selected,
        config,
        starting_capital,
    )
    artifacts = (
        signals_path,
        binance_path,
        chainlink_path,
        summary_path,
        html_path,
    )
    _write_json(
        run_dir / "run_record.json",
        {
            "schema_version": 2,
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
            "binance_config_sha256": binance_provenance["config_sha256"],
            "chainlink_config_path": str(chainlink_config_path),
            "chainlink_config_sha256": chainlink_provenance["config_sha256"],
            "binance_data_provenance": _compact_context_provenance(binance_provenance),
            "chainlink_data_provenance": _compact_context_provenance(
                chainlink_provenance
            ),
            "market_data_provenance": market_provenance.__dict__,
            "holdout_rows_loaded": 0,
            "holdout_opened": False,
            "starting_capital_usdc": starting_capital,
            "device": str(device),
            "dtype": config.dtype,
            "torch_version": torch.__version__,
            "plotly_version": plotly.__version__,
            "python": platform.python_version(),
            "artifacts": {path.name: _sha256(path) for path in artifacts},
        },
    )
    return run_dir
