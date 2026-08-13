from __future__ import annotations

import argparse
from collections.abc import Sequence

from .audit.runner import run_profile_audit
from .audit.storage import build_raw_inventory
from .collector.replay import replay_run
from .collector.runner import run_collector
from .historical.download import download_kacho_dataset
from .historical.overlap import run_pmxt_overlap_smoke
from .screening.calibration import run_stage4b_calibration
from .screening.holdout import run_stage4b_test
from .screening.openmarket import run_openmarket_sanity
from .screening.pmxt_dataset import build_stage4_pmxt_dataset
from .screening.run import run_stage4_screening
from .screening.universe import build_stage4_universe
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
