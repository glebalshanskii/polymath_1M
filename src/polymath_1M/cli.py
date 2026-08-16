from __future__ import annotations

import argparse
from collections.abc import Sequence

from .audit.runner import run_profile_audit
from .audit.storage import build_raw_inventory
from .collector.replay import replay_run
from .collector.runner import run_collector
from .historical.binance import download_binance_context
from .historical.download import download_kacho_dataset
from .historical.overlap import run_pmxt_overlap_smoke
from .historical.pmxt_store import build_pmxt_store_pilot
from .historical.polymarket_chainlink import (
    download_polymarket_chainlink_context,
)
from .historical.trent import download_trent_gamma_outcomes, download_trent_steps
from .screening.calibration import run_stage4b_calibration
from .screening.capital_chart import run_stage4d_capital_chart
from .screening.cost_sensitivity import run_stage4j_zero_cost
from .screening.edge_diagnostics import run_stage4k_edge_diagnostic
from .screening.holdout import run_stage4b_test
from .screening.literal_markov import run_stage4f_literal_markov
from .screening.openmarket import run_openmarket_sanity
from .screening.plateau import run_stage4d_calibration
from .screening.pmxt_dataset import build_stage4_pmxt_dataset
from .screening.positive_retest import run_stage4i_positive_retest
from .screening.practical_chain import (
    build_practical_chain_cache,
    run_stage4h_practical_chain,
)
from .screening.regime_holdout import run_stage4e_holdout
from .screening.regime_models import run_stage4e_development
from .screening.regime_robustness import run_stage4e_early_robustness
from .screening.run import run_stage4_screening
from .screening.terminal_markov import run_stage4g_terminal_markov
from .screening.time_chart import run_stage4d_time_chart
from .screening.universe import build_stage4_universe
from .screening.walkforward import run_stage4c_calibration
from .strategy.backtest import run_kacho_backtest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polymath_1M")
    subparsers = parser.add_subparsers(dest="command")
    audit = subparsers.add_parser(
        "profile-audit", help="run the read-only Murtazin profile audit"
    )
    audit.add_argument(
        "--config",
        default="cfg/audits/murtazin_profiles.json",
        help="path to the audit JSON config",
    )
    audit.add_argument(
        "--output-root",
        default="outputs/profile_audit",
        help="directory for raw responses and audit artifacts",
    )
    audit.add_argument(
        "--resume-run",
        help="existing failed run directory to resume from endpoint checkpoints",
    )
    inventory = subparsers.add_parser(
        "profile-audit-inventory",
        help="hash every archived response and report request-metadata coverage",
    )
    inventory.add_argument(
        "--run-dir", required=True, help="existing profile-audit run"
    )
    collector = subparsers.add_parser(
        "collector-run",
        help="collect Gamma metadata, Polymarket CLOB L2, and RTDS reference prices",
    )
    collector.add_argument(
        "--config",
        default="cfg/collectors/stage2_polymarket.json",
        help="path to the collector JSON config",
    )
    collector.add_argument(
        "--output-root",
        default="outputs/collector",
        help="directory for raw market-data runs",
    )
    collector.add_argument(
        "--duration-seconds",
        type=int,
        help="override the configured run duration (the committed config is 24 hours)",
    )
    replay = subparsers.add_parser(
        "collector-replay",
        help="rebuild all L2 books from a collector run and compare its final digest",
    )
    replay.add_argument("--run-dir", required=True, help="existing collector run")
    download = subparsers.add_parser(
        "kacho-download",
        help="download and SHA-256 verify a pinned Kacho historical subset",
    )
    download.add_argument(
        "--config",
        default="cfg/datasets/kacho_5m.json",
        help="path to the pinned Kacho dataset config",
    )
    download.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory for third-party historical files",
    )
    download.add_argument(
        "--assets",
        default="BTC",
        help="comma-separated subset of BTC,ETH,SOL,XRP",
    )
    download.add_argument(
        "--kinds",
        default="markets,ticks",
        help="comma-separated subset of markets,ticks",
    )
    backtest = subparsers.add_parser(
        "backtest-kacho",
        help="run the Stage 3 causal development backtest on pinned Kacho data",
    )
    backtest.add_argument(
        "--config",
        default="cfg/experiments/stage3_kacho_5m_tiny.json",
        help="path to the executable backtest config",
    )
    backtest.add_argument(
        "--output-root",
        default="outputs/backtests",
        help="ignored directory for backtest artifacts",
    )
    overlap = subparsers.add_parser(
        "pmxt-overlap-smoke",
        help="compare one exact Kacho tick with PMXT full-L2 replay",
    )
    overlap.add_argument(
        "--config",
        default="cfg/experiments/stage3_pmxt_overlap_smoke.json",
        help="path to the pinned overlap smoke config",
    )
    overlap.add_argument(
        "--output-root",
        default="outputs/overlap",
        help="ignored directory for overlap artifacts",
    )
    store_pilot = subparsers.add_parser(
        "pmxt-store-pilot",
        help="build the one-day full-resolution local PMXT Parquet store pilot",
    )
    store_pilot.add_argument(
        "--config",
        default="cfg/experiments/pmxt_parquet_pilot_20260608.json",
        help="path to the frozen one-day store pilot config",
    )
    store_pilot.add_argument(
        "--resume",
        action="store_true",
        help="resume hours completed by an interrupted run of the same config",
    )
    universe = subparsers.add_parser(
        "stage4-build-universe",
        help="archive Gamma pages and build the frozen Stage 4 market universe",
    )
    universe.add_argument(
        "--config",
        default="cfg/experiments/stage4_pmxt_screening.json",
        help="path to the frozen Stage 4 screening config",
    )
    pmxt_dataset = subparsers.add_parser(
        "stage4-build-pmxt",
        help="build restartable causal PMXT snapshots for the Stage 4 universe",
    )
    pmxt_dataset.add_argument(
        "--config",
        default="cfg/experiments/stage4_pmxt_screening.json",
        help="path to the frozen Stage 4 screening config",
    )
    screening = subparsers.add_parser(
        "stage4-screen",
        help="select and test Stage 4 strategies on frozen PMXT snapshots",
    )
    screening.add_argument(
        "--config",
        default="cfg/experiments/stage4_pmxt_screening.json",
        help="path to the frozen Stage 4 screening config",
    )
    screening.add_argument(
        "--output-root",
        default="outputs/screening",
        help="ignored directory for Stage 4 screening artifacts",
    )
    sanity = subparsers.add_parser(
        "stage4-openmarket-sanity",
        help="compare one PMXT market with the pinned independent OpenMarket archive",
    )
    sanity.add_argument(
        "--config",
        default="cfg/experiments/stage4_pmxt_screening.json",
        help="path to the frozen Stage 4 screening config",
    )
    calibration = subparsers.add_parser(
        "stage4b-calibrate",
        help="calibrate practical signal gates on train/validation only",
    )
    calibration.add_argument(
        "--config",
        default="cfg/experiments/stage4b_signal_calibration.json",
        help="path to the frozen Stage 4b calibration config",
    )
    calibration.add_argument(
        "--output-root",
        default="outputs/calibration",
        help="ignored directory for Stage 4b calibration artifacts",
    )
    holdout = subparsers.add_parser(
        "stage4b-test",
        help="run the frozen Stage 4b strategy once on untouched test",
    )
    holdout.add_argument(
        "--config",
        default="cfg/experiments/stage4b_selected.json",
        help="path to the committed Stage 4b selected config",
    )
    holdout.add_argument(
        "--output-root",
        default="outputs/screening",
        help="ignored directory for the one-shot Stage 4b test artifact",
    )
    walkforward = subparsers.add_parser(
        "stage4c-calibrate",
        help="run frozen expanding walk-forward calibration without holdout",
    )
    walkforward.add_argument(
        "--config",
        default="cfg/experiments/stage4c_walkforward.json",
        help="path to the frozen Stage 4c walk-forward config",
    )
    walkforward.add_argument(
        "--output-root",
        default="outputs/calibration",
        help="ignored directory for Stage 4c calibration artifacts",
    )
    plateau = subparsers.add_parser(
        "stage4d-calibrate",
        help="run development-only uniform-grid plateau calibration",
    )
    plateau.add_argument(
        "--config",
        default="cfg/experiments/stage4d_uniform_plateau.json",
        help="path to the frozen Stage 4d plateau config",
    )
    plateau.add_argument(
        "--output-root",
        default="outputs/calibration",
        help="ignored directory for Stage 4d development artifacts",
    )
    capital_chart = subparsers.add_parser(
        "stage4d-capital-chart",
        help="render the selected Stage 4d development capital ledger",
    )
    capital_chart.add_argument(
        "--config",
        default="cfg/experiments/stage4d_uniform_plateau.json",
        help="path to the frozen Stage 4d plateau config",
    )
    capital_chart.add_argument(
        "--proposal",
        required=True,
        help="path to the selected Stage 4d development proposal",
    )
    capital_chart.add_argument(
        "--starting-capital",
        type=float,
        required=True,
        help="explicit scenario capital in USDC; no project default is assumed",
    )
    capital_chart.add_argument(
        "--output-root",
        default="outputs/charts",
        help="ignored directory for chart, ledger and summary artifacts",
    )
    binance_context = subparsers.add_parser(
        "binance-context-download",
        help="download SHA-256 pinned BTCUSDT context through development only",
    )
    binance_context.add_argument(
        "--config",
        default="cfg/datasets/binance_btcusdt_1m_202604_202605.json",
        help="path to the pinned visualization-only Binance config",
    )
    binance_context.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory for third-party historical files",
    )
    chainlink_context = subparsers.add_parser(
        "polymarket-chainlink-context-download",
        help="archive minute Polymarket Chainlink frontend history before holdout",
    )
    chainlink_context.add_argument(
        "--config",
        default="cfg/datasets/polymarket_chainlink_btcusd_1m_stage4d.json",
        help="development-only Polymarket Chainlink context config",
    )
    chainlink_context.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory for raw frontend responses and manifest",
    )
    time_chart = subparsers.add_parser(
        "stage4d-time-chart",
        help="render Stage 4d BTC outcomes, signals and PnL against UTC time",
    )
    time_chart.add_argument(
        "--config",
        default="cfg/experiments/stage4d_uniform_plateau.json",
        help="path to the frozen Stage 4d plateau config",
    )
    time_chart.add_argument(
        "--proposal",
        required=True,
        help="path to the selected Stage 4d development proposal",
    )
    time_chart.add_argument(
        "--binance-config",
        default="cfg/datasets/binance_btcusdt_1m_202604_202605.json",
        help="pinned Binance BTCUSDT visualization-context config",
    )
    time_chart.add_argument(
        "--chainlink-config",
        default="cfg/datasets/polymarket_chainlink_btcusd_1m_stage4d.json",
        help="Polymarket Chainlink frontend history context config",
    )
    time_chart.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory containing the pinned Binance files",
    )
    time_chart.add_argument(
        "--starting-capital",
        type=float,
        required=True,
        help="explicit scenario capital in USDC; no project default is assumed",
    )
    time_chart.add_argument(
        "--output-root",
        default="outputs/charts",
        help="ignored directory for chart and exact signal ledgers",
    )
    regime_models = subparsers.add_parser(
        "stage4e-develop",
        help="run the frozen sequential BTC 5m regime-model comparison",
    )
    regime_models.add_argument(
        "--config",
        default="cfg/experiments/stage4e_regime_models.json",
        help="path to the frozen Stage 4e model-sequence config",
    )
    regime_models.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory containing pinned Kacho and Chainlink inputs",
    )
    regime_models.add_argument(
        "--output-root",
        default="outputs/regime_models",
        help="ignored directory for per-model diagnostics and proposal",
    )
    trent_download = subparsers.add_parser(
        "trent-steps-download",
        help="download and hash the pinned early BTC 5m Trent steps archive",
    )
    trent_download.add_argument(
        "--config",
        default="cfg/datasets/trent_btc5m_steps_stage4e.json",
        help="path to the pinned Trent dataset config",
    )
    trent_download.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory for third-party historical files",
    )
    trent_gamma = subparsers.add_parser(
        "trent-gamma-download",
        help="archive authoritative Gamma outcomes for the early Trent markets",
    )
    trent_gamma.add_argument(
        "--config",
        default="cfg/datasets/trent_btc5m_gamma_stage4e.json",
        help="path to the pinned early Gamma outcome config",
    )
    trent_gamma.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory for the Gamma outcome archive",
    )
    early_robustness = subparsers.add_parser(
        "stage4e-early-robustness",
        help="compare Stage 4e baseline/candidate on the early Trent source",
    )
    early_robustness.add_argument(
        "--config",
        default="cfg/experiments/stage4e_trent_robustness.json",
        help="path to the frozen source-specific robustness config",
    )
    early_robustness.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory containing pinned Trent and Chainlink inputs",
    )
    early_robustness.add_argument(
        "--output-root",
        default="outputs/regime_models",
        help="ignored directory for source-specific robustness artifacts",
    )
    regime_holdout = subparsers.add_parser(
        "stage4e-holdout",
        help="run the selected Stage 4e model once on the frozen holdout",
    )
    regime_holdout.add_argument(
        "--config",
        default="cfg/experiments/stage4e_selected.json",
        help="path to the selected Stage 4e model and holdout contract",
    )
    regime_holdout.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory containing pinned Kacho and Chainlink inputs",
    )
    regime_holdout.add_argument(
        "--output-root",
        default="outputs/holdout",
        help="ignored directory for the one-shot holdout artifacts",
    )
    literal_markov = subparsers.add_parser(
        "stage4f-literal-markov",
        help="run the frozen source-defined Markov entry rules",
    )
    literal_markov.add_argument(
        "--config",
        default="cfg/experiments/stage4f_literal_markov.json",
        help="path to the frozen Stage 4f literal-Markov config",
    )
    literal_markov.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory containing pinned Kacho and Trent inputs",
    )
    literal_markov.add_argument(
        "--output-root",
        default="outputs/literal_markov",
        help="ignored directory for literal-Markov diagnostics",
    )
    terminal_markov = subparsers.add_parser(
        "stage4g-terminal-markov",
        help="run the frozen market-anchored terminal Markov strategy",
    )
    terminal_markov.add_argument(
        "--config",
        default="cfg/experiments/stage4g_terminal_markov.json",
        help="path to the frozen Stage 4g terminal-Markov config",
    )
    terminal_markov.add_argument(
        "--data-root",
        default="data/historical",
        help="ignored directory containing pinned Kacho and Trent inputs",
    )
    terminal_markov.add_argument(
        "--output-root",
        default="outputs/terminal_markov",
        help="ignored directory for terminal-Markov diagnostics",
    )
    practical_chain_cache = subparsers.add_parser(
        "stage4h-build-cache",
        help="build causal PMXT top snapshots for the practical chain",
    )
    practical_chain_cache.add_argument(
        "--config",
        default="cfg/experiments/stage4h_practical_chain.json",
        help="path to the frozen Stage 4h practical-chain config",
    )
    practical_chain = subparsers.add_parser(
        "stage4h-practical-chain",
        help="run the frozen unsmoothed time-inhomogeneous chain",
    )
    practical_chain.add_argument(
        "--config",
        default="cfg/experiments/stage4h_practical_chain.json",
        help="path to the frozen Stage 4h practical-chain config",
    )
    practical_chain.add_argument(
        "--output-root",
        default="outputs/practical_chain",
        help="ignored directory for Stage 4h diagnostics",
    )
    positive_retest = subparsers.add_parser(
        "stage4i-positive-retest",
        help="retest every historically positive fixed model on PMXT v2",
    )
    positive_retest.add_argument(
        "--config",
        default="cfg/experiments/stage4i_pmxt_positive_retest.json",
        help="path to the frozen Stage 4i PMXT retest config",
    )
    positive_retest.add_argument(
        "--output-root",
        default="outputs/positive_retest",
        help="ignored directory for Stage 4i diagnostics",
    )
    zero_cost = subparsers.add_parser(
        "stage4j-zero-cost",
        help="revalue fixed Stage 4i fills without synthetic per-share costs",
    )
    zero_cost.add_argument(
        "--config",
        default="cfg/experiments/stage4j_zero_extra_cost.json",
        help="path to the frozen Stage 4j cost-sensitivity config",
    )
    zero_cost.add_argument(
        "--output-root",
        default="outputs/cost_sensitivity",
        help="ignored directory for Stage 4j diagnostics",
    )
    edge_diagnostic = subparsers.add_parser(
        "stage4k-edge-diagnostic",
        help="diagnose PMXT edge and anti-edge on fixed Stage 4i fills",
    )
    edge_diagnostic.add_argument(
        "--config",
        default="cfg/experiments/stage4k_edge_anti_edge.json",
        help="path to the frozen Stage 4k edge diagnostic config",
    )
    edge_diagnostic.add_argument(
        "--output-root",
        default="outputs/edge_diagnostics",
        help="ignored directory for Stage 4k diagnostics",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    if args.command == "profile-audit":
        run_dir = run_profile_audit(
            args.config,
            args.output_root,
            resume_run=args.resume_run,
        )
        print(run_dir)
    elif args.command == "profile-audit-inventory":
        inventory_path, summary_path = build_raw_inventory(args.run_dir)
        print(inventory_path)
        print(summary_path)
    elif args.command == "collector-run":
        run_dir = run_collector(
            args.config,
            args.output_root,
            duration_seconds=args.duration_seconds,
        )
        print(run_dir)
    elif args.command == "collector-replay":
        print(replay_run(args.run_dir))
    elif args.command == "kacho-download":
        assets = tuple(value.strip().upper() for value in args.assets.split(","))
        kinds = tuple(value.strip() for value in args.kinds.split(","))
        print(
            download_kacho_dataset(
                args.config,
                args.data_root,
                assets=assets,
                kinds=kinds,
            )
        )
    elif args.command == "backtest-kacho":
        print(run_kacho_backtest(args.config, args.output_root))
    elif args.command == "pmxt-overlap-smoke":
        print(run_pmxt_overlap_smoke(args.config, args.output_root))
    elif args.command == "pmxt-store-pilot":
        print(build_pmxt_store_pilot(args.config, resume=args.resume))
    elif args.command == "stage4-build-universe":
        print(build_stage4_universe(args.config))
    elif args.command == "stage4-build-pmxt":
        print(build_stage4_pmxt_dataset(args.config))
    elif args.command == "stage4-screen":
        print(run_stage4_screening(args.config, args.output_root))
    elif args.command == "stage4-openmarket-sanity":
        print(run_openmarket_sanity(args.config))
    elif args.command == "stage4b-calibrate":
        print(run_stage4b_calibration(args.config, args.output_root))
    elif args.command == "stage4b-test":
        print(run_stage4b_test(args.config, args.output_root))
    elif args.command == "stage4c-calibrate":
        print(run_stage4c_calibration(args.config, args.output_root))
    elif args.command == "stage4d-calibrate":
        print(run_stage4d_calibration(args.config, args.output_root))
    elif args.command == "stage4d-capital-chart":
        print(
            run_stage4d_capital_chart(
                args.config,
                args.proposal,
                starting_capital=args.starting_capital,
                output_root=args.output_root,
            )
        )
    elif args.command == "binance-context-download":
        print(download_binance_context(args.config, args.data_root))
    elif args.command == "polymarket-chainlink-context-download":
        print(download_polymarket_chainlink_context(args.config, args.data_root))
    elif args.command == "stage4d-time-chart":
        print(
            run_stage4d_time_chart(
                args.config,
                args.proposal,
                args.binance_config,
                args.chainlink_config,
                starting_capital=args.starting_capital,
                data_root=args.data_root,
                output_root=args.output_root,
            )
        )
    elif args.command == "stage4e-develop":
        print(
            run_stage4e_development(
                args.config,
                data_root=args.data_root,
                output_root=args.output_root,
            )
        )
    elif args.command == "trent-steps-download":
        print(download_trent_steps(args.config, args.data_root))
    elif args.command == "trent-gamma-download":
        print(download_trent_gamma_outcomes(args.config, args.data_root))
    elif args.command == "stage4e-early-robustness":
        print(
            run_stage4e_early_robustness(
                args.config,
                data_root=args.data_root,
                output_root=args.output_root,
            )
        )
    elif args.command == "stage4e-holdout":
        print(
            run_stage4e_holdout(
                args.config,
                data_root=args.data_root,
                output_root=args.output_root,
            )
        )
    elif args.command == "stage4f-literal-markov":
        print(
            run_stage4f_literal_markov(
                args.config,
                data_root=args.data_root,
                output_root=args.output_root,
            )
        )
    elif args.command == "stage4g-terminal-markov":
        print(
            run_stage4g_terminal_markov(
                args.config,
                data_root=args.data_root,
                output_root=args.output_root,
            )
        )
    elif args.command == "stage4h-build-cache":
        print(build_practical_chain_cache(args.config))
    elif args.command == "stage4h-practical-chain":
        print(run_stage4h_practical_chain(args.config, args.output_root))
    elif args.command == "stage4i-positive-retest":
        print(run_stage4i_positive_retest(args.config, args.output_root))
    elif args.command == "stage4j-zero-cost":
        print(run_stage4j_zero_cost(args.config, args.output_root))
    elif args.command == "stage4k-edge-diagnostic":
        print(run_stage4k_edge_diagnostic(args.config, args.output_root))
