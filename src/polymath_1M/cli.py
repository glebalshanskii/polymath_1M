from __future__ import annotations

import argparse
from collections.abc import Sequence

from .audit.runner import run_profile_audit
from .audit.storage import build_raw_inventory
from .collector.replay import replay_run
from .collector.runner import run_collector


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
