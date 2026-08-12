from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import duckdb
import pyarrow.parquet as pq

from .config import load_screening_config


class OpenMarketSanityError(RuntimeError):
    """The pinned OpenMarket sanity check cannot be completed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_url(revision: str, table: str, date: str) -> str:
    return (
        "https://huggingface.co/datasets/gregyoung14/"
        f"openmarket-btc-polymarket/resolve/{revision}/{table}/"
        f"date={date}/part-000001.parquet"
    )


def run_openmarket_sanity(config_path: str | Path) -> Path:
    config = load_screening_config(config_path)
    root = Path(config.data_root)
    pmxt_manifest_path = root / "pmxt_dataset_manifest.json"
    if not pmxt_manifest_path.is_file():
        raise OpenMarketSanityError("build the complete PMXT dataset first")
    pmxt_manifest = json.loads(pmxt_manifest_path.read_text(encoding="utf-8"))
    if pmxt_manifest.get("config_sha256") != config.config_sha256:
        raise OpenMarketSanityError("PMXT and screening config hashes differ")

    coverage: list[dict[str, Any]] = []
    date = config.period_start.date()
    while date < config.period_end_exclusive.date():
        date_string = date.isoformat()
        url = _file_url(
            config.openmarket_revision, config.openmarket_table, date_string
        )
        request = Request(url, method="HEAD", headers={"User-Agent": "polymath-1m/1"})
        try:
            with urlopen(request, timeout=30) as response:
                coverage.append(
                    {
                        "date": date_string,
                        "url": response.geturl(),
                        "status": response.status,
                        "bytes": int(response.headers.get("Content-Length", 0)),
                        "etag": response.headers.get("ETag"),
                    }
                )
        except OSError as exc:
            coverage.append({"date": date_string, "url": url, "error": str(exc)})
        date += timedelta(days=1)

    comparison_date = "2026-04-17"
    comparison_slug = "btc-updown-15m-1776433500"
    universe = pq.read_table(root / "universe.parquet").to_pylist()
    candidates = [row for row in universe if row["slug"] == comparison_slug]
    if len(candidates) != 1:
        raise OpenMarketSanityError("fixed overlap slug is absent from Gamma universe")
    market = candidates[0]
    signal_ms = (
        int(market["market_end_ms"]) - config.decision_seconds_before_end * 1_000
    )
    execution_ms = signal_ms + config.execution_latency_ms
    openmarket_url = _file_url(
        config.openmarket_revision, config.openmarket_table, comparison_date
    )
    connection = duckdb.connect()
    try:
        openmarket = connection.execute(
            """
            WITH requested(cutoff_name, cutoff_ms) AS (
                VALUES ('signal', ?), ('execution', ?)
            )
            SELECT
                r.cutoff_name,
                upper(p.side_label) AS outcome,
                arg_max(p.best_bid, p.ingest_ts_ms) AS best_bid,
                arg_max(p.best_ask, p.ingest_ts_ms) AS best_ask,
                max(p.ingest_ts_ms) AS observed_ingest_ms
            FROM read_parquet(?) AS p
            CROSS JOIN requested AS r
            WHERE p.market_slug = ?
              AND p.ingest_ts_ms <= r.cutoff_ms
              AND p.best_bid IS NOT NULL
              AND p.best_ask IS NOT NULL
            GROUP BY r.cutoff_name, upper(p.side_label)
            ORDER BY r.cutoff_name, outcome
            """,
            [signal_ms, execution_ms, openmarket_url, comparison_slug],
        ).fetch_arrow_table()
    finally:
        connection.close()
    pmxt_hour = root / "pmxt_hours" / "hour=2026-04-17T13.parquet"
    if not pmxt_hour.is_file():
        raise OpenMarketSanityError("fixed overlap PMXT checkpoint is absent")
    pmxt_rows = [
        row
        for row in pq.read_table(pmxt_hour).to_pylist()
        if row["slug"] == comparison_slug
    ]
    if len(pmxt_rows) != 1:
        raise OpenMarketSanityError("fixed overlap PMXT row is not unique")
    pmxt = pmxt_rows[0]
    openmarket_rows = openmarket.to_pylist()
    index = {
        (str(row["cutoff_name"]), str(row["outcome"])): row for row in openmarket_rows
    }
    comparisons: list[dict[str, Any]] = []
    for cutoff, prefix in (("signal", "signal"), ("execution", "execution")):
        for outcome, suffix in (("UP", "up"), ("DOWN", "down")):
            other = index.get((cutoff, outcome))
            pmxt_bid = pmxt[f"signal_bid_{suffix}"] if cutoff == "signal" else None
            pmxt_ask = (
                pmxt[f"signal_ask_{suffix}"]
                if cutoff == "signal"
                else pmxt[f"execution_ask_prices_{suffix}"][0]
            )
            comparisons.append(
                {
                    "cutoff": prefix,
                    "outcome": outcome,
                    "pmxt_best_bid": pmxt_bid,
                    "pmxt_best_ask": pmxt_ask,
                    "openmarket_best_bid": None if other is None else other["best_bid"],
                    "openmarket_best_ask": None if other is None else other["best_ask"],
                    "ask_absolute_difference": (
                        None
                        if other is None
                        else abs(float(pmxt_ask) - float(other["best_ask"]))
                    ),
                }
            )
    result = {
        "schema_version": 1,
        "config_sha256": config.config_sha256,
        "openmarket_revision": config.openmarket_revision,
        "coverage": coverage,
        "comparison_slug": comparison_slug,
        "comparison": comparisons,
        "all_dates_available": all(item.get("status") == 200 for item in coverage),
        "all_comparison_rows_present": len(openmarket_rows) == 4,
        "note": "Independent source sanity only; OpenMarket rows never enter PnL.",
    }
    path = root / "openmarket_sanity.json"
    temporary = path.with_suffix(".json.part")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    result["artifact_sha256"] = _sha256(path)
    return path
