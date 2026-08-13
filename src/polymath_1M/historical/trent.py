from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import torch

from polymath_1M.domain import DecisionBatch


class TrentDataError(RuntimeError):
    """The pinned Trent BTC 5m source is incomplete or violates its contract."""


@dataclass(frozen=True)
class TrentConfig:
    dataset_id: str
    revision: str
    license: str
    base_url: str
    file_glob: str
    expected_files: int
    period_start_s: int
    period_end_exclusive_s: int
    max_workers: int
    config_sha256: str

    def dataset_dir(self, root: str | Path) -> Path:
        return Path(root) / self.dataset_id.replace("/", "--") / self.revision


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(value: Any, name: str) -> int:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise TrentDataError(f"{name} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise TrentDataError(f"{name} must include timezone")
    return int(parsed.astimezone(UTC).timestamp())


def load_trent_config(path: str | Path) -> TrentConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "dataset_id",
        "revision",
        "license",
        "base_url",
        "file_glob",
        "expected_files",
        "period_start",
        "period_end_exclusive",
        "max_workers",
    }
    if payload.keys() != expected:
        raise TrentDataError(
            f"Trent config fields differ: missing={sorted(expected - payload.keys())}, "
            f"extra={sorted(payload.keys() - expected)}"
        )
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    config = TrentConfig(
        dataset_id=str(payload["dataset_id"]),
        revision=str(payload["revision"]),
        license=str(payload["license"]),
        base_url=str(payload["base_url"]).rstrip("/"),
        file_glob=str(payload["file_glob"]),
        expected_files=int(payload["expected_files"]),
        period_start_s=_timestamp(payload["period_start"], "period_start"),
        period_end_exclusive_s=_timestamp(
            payload["period_end_exclusive"], "period_end_exclusive"
        ),
        max_workers=int(payload["max_workers"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )
    if (
        payload["schema_version"] != 1
        or config.dataset_id != "trentmkelly/polymarket_crypto_derivatives"
        or len(config.revision) != 40
        or config.license != "CC-BY-SA-4.0"
        or config.file_glob != "btc5m_*/steps.parquet"
        or config.expected_files != 8_803
        or not 1 <= config.max_workers <= 32
        or config.period_start_s >= config.period_end_exclusive_s
    ):
        raise TrentDataError("Trent config differs from the audited early BTC source")
    return config


def _repository_paths(config: TrentConfig) -> list[str]:
    url = (
        "https://huggingface.co/api/datasets/"
        f"{config.dataset_id}/revision/{config.revision}"
    )
    request = urllib.request.Request(
        url, headers={"User-Agent": "polymath_1M/0.1 pinned-source-downloader"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    paths = sorted(
        item["rfilename"]
        for item in payload.get("siblings", [])
        if str(item.get("rfilename", "")).startswith("btc5m_")
        and str(item["rfilename"]).endswith("/steps.parquet")
    )
    if len(paths) != config.expected_files:
        raise TrentDataError(
            f"pinned repository has {len(paths)} BTC 5m steps files, "
            f"expected {config.expected_files}"
        )
    if "2026-02-21_15-55-00" not in paths[0] or "2026-03-24_20-05-00" not in paths[-1]:
        raise TrentDataError("pinned Trent path boundaries differ")
    return paths


def _download_one(config: TrentConfig, relative: str, target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"{config.base_url}/{config.revision}/{urllib.parse.quote(relative)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "polymath_1M/0.1 pinned-source-downloader"}
    )
    temporary = target.with_suffix(".parquet.part")
    with (
        urllib.request.urlopen(request, timeout=120) as response,
        temporary.open("wb") as stream,
    ):
        while chunk := response.read(1024 * 1024):
            stream.write(chunk)
    os.replace(temporary, target)
    return {
        "path": relative,
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
    }


def download_trent_steps(
    config_path: str | Path,
    data_root: str | Path = "data/historical",
) -> Path:
    config = load_trent_config(config_path)
    dataset_dir = config.dataset_dir(data_root)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    paths = _repository_paths(config)
    manifest_path = dataset_dir / "manifest.json"
    existing: dict[str, dict[str, Any]] = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("config_sha256") == config.config_sha256:
            existing = {str(item["path"]): item for item in manifest.get("files", [])}
    records: dict[str, dict[str, Any]] = {}
    pending: list[tuple[str, Path]] = []
    for relative in paths:
        target = dataset_dir / relative
        record = existing.get(relative)
        if (
            record is not None
            and target.is_file()
            and target.stat().st_size == int(record["bytes"])
            and _sha256(target) == record["sha256"]
        ):
            records[relative] = record
        else:
            pending.append((relative, target))
    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        futures = {
            executor.submit(_download_one, config, relative, target): relative
            for relative, target in pending
        }
        for future in as_completed(futures):
            relative = futures[future]
            records[relative] = future.result()
    if set(records) != set(paths):
        raise TrentDataError("Trent download is incomplete")
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "config_path": str(config_path),
        "config_sha256": config.config_sha256,
        "dataset_id": config.dataset_id,
        "revision": config.revision,
        "license": config.license,
        "period_start": datetime.fromtimestamp(
            config.period_start_s, tz=UTC
        ).isoformat(),
        "period_end_exclusive": datetime.fromtimestamp(
            config.period_end_exclusive_s, tz=UTC
        ).isoformat(),
        "files": [records[path] for path in paths],
    }
    temporary = manifest_path.with_suffix(".json.part")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)
    return manifest_path


def load_trent_decision_batch(
    config_path: str | Path,
    data_root: str | Path,
    *,
    decision_seconds_before_end: int,
    transition_horizon_seconds: int,
    execution_latency_seconds: int,
    target_notional_usdc: float,
) -> tuple[DecisionBatch, dict[str, Any]]:
    config = load_trent_config(config_path)
    dataset_dir = config.dataset_dir(data_root)
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.is_file():
        raise TrentDataError("download the pinned Trent steps first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("config_sha256") != config.config_sha256
        or len(manifest.get("files", [])) != config.expected_files
    ):
        raise TrentDataError("Trent manifest differs from frozen config")
    for record in manifest["files"]:
        path = dataset_dir / record["path"]
        if not path.is_file() or _sha256(path) != record["sha256"]:
            raise TrentDataError(f"Trent source file changed: {path}")
    glob = str(dataset_dir / config.file_glob).replace("'", "''")
    decision_offset_ms = (300 - decision_seconds_before_end) * 1_000
    previous_offset_ms = (
        300 - decision_seconds_before_end - transition_horizon_seconds
    ) * 1_000
    execution_offset_ms = (
        300 - decision_seconds_before_end + execution_latency_seconds
    ) * 1_000
    if previous_offset_ms <= 0 or execution_latency_seconds < 0:
        raise TrentDataError("Trent snapshot offsets are invalid")
    connection = duckdb.connect()
    connection.execute("SET threads TO 8")
    table = connection.execute(
        f"""
        WITH raw AS MATERIALIZED (
            SELECT
                filename,
                CAST(epoch(strptime(
                    regexp_extract(filename, '(\\d{{4}}-\\d{{2}}-\\d{{2}}_\\d{{2}}-\\d{{2}}-\\d{{2}})', 1),
                    '%Y-%m-%d_%H-%M-%S'
                )) AS BIGINT) AS market_start_s,
                ts,
                up_best_bid,
                up_best_ask,
                up_mid,
                up_ask_size_total,
                down_best_bid,
                down_best_ask,
                down_mid,
                down_ask_size_total
            FROM read_parquet('{glob}', filename=true)
        )
        SELECT
            regexp_extract(filename, '(btc5m_[^/]+)', 1) AS condition_id,
            market_start_s,
            count(*) AS n_ticks,
            arg_max(up_mid, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {previous_offset_ms}
            ) AS previous_mid_up,
            max(ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {previous_offset_ms}
            ) AS previous_ts,
            arg_max(up_best_bid, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
            ) AS current_bid_up,
            arg_max(up_best_ask, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
            ) AS current_ask_up,
            arg_max(up_mid, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
            ) AS current_mid_up,
            arg_max(down_best_bid, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
            ) AS current_bid_down,
            arg_max(down_best_ask, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
            ) AS current_ask_down,
            arg_max(down_mid, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
            ) AS current_mid_down,
            max(ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
            ) AS current_ts,
            arg_min(up_best_ask, ts) FILTER (
                WHERE ts >= market_start_s * 1000 + {execution_offset_ms}
            ) AS execution_ask_up,
            arg_min(up_ask_size_total, ts) FILTER (
                WHERE ts >= market_start_s * 1000 + {execution_offset_ms}
            ) AS execution_size_up,
            arg_min(down_best_ask, ts) FILTER (
                WHERE ts >= market_start_s * 1000 + {execution_offset_ms}
            ) AS execution_ask_down,
            arg_min(down_ask_size_total, ts) FILTER (
                WHERE ts >= market_start_s * 1000 + {execution_offset_ms}
            ) AS execution_size_down,
            min(ts) FILTER (
                WHERE ts >= market_start_s * 1000 + {execution_offset_ms}
            ) AS execution_ts,
            arg_max(up_mid, ts) AS terminal_mid_up
        FROM raw
        GROUP BY filename, market_start_s
        ORDER BY market_start_s, condition_id
        """
    ).fetch_arrow_table()
    connection.close()
    if table.num_rows != config.expected_files:
        raise TrentDataError(
            f"Trent adapter loaded {table.num_rows} markets, expected {config.expected_files}"
        )

    def floats(name: str) -> torch.Tensor:
        return torch.tensor(
            [
                float("nan") if value is None else value
                for value in table[name].to_pylist()
            ],
            dtype=torch.float64,
        )

    def integers(name: str) -> torch.Tensor:
        return torch.tensor(
            [-1 if value is None else value for value in table[name].to_pylist()],
            dtype=torch.int64,
        )

    start = integers("market_start_s")
    end = start + 300
    decision = end - decision_seconds_before_end
    previous = decision - transition_horizon_seconds
    bids = torch.stack((floats("current_bid_up"), floats("current_bid_down")), dim=1)
    asks = torch.stack((floats("current_ask_up"), floats("current_ask_down")), dim=1)
    current_mid = torch.stack(
        (floats("current_mid_up"), floats("current_mid_down")), dim=1
    )
    execution_asks = torch.stack(
        (floats("execution_ask_up"), floats("execution_ask_down")), dim=1
    )
    total_sizes = torch.stack(
        (floats("execution_size_up"), floats("execution_size_down")), dim=1
    )
    executable_sizes = torch.where(
        total_sizes * execution_asks >= target_notional_usdc,
        total_sizes,
        torch.zeros_like(total_sizes),
    )
    previous_ts = integers("previous_ts")
    current_ts = integers("current_ts")
    execution_ts = integers("execution_ts")
    timely = (
        (previous_ts <= previous * 1_000)
        & (previous_ts >= previous * 1_000 - 1_000)
        & (current_ts <= decision * 1_000)
        & (current_ts >= decision * 1_000 - 1_000)
        & (execution_ts >= (decision + execution_latency_seconds) * 1_000)
        & (execution_ts <= (decision + execution_latency_seconds) * 1_000 + 1_000)
    )
    finite = (
        torch.isfinite(bids).all(dim=1)
        & torch.isfinite(asks).all(dim=1)
        & torch.isfinite(current_mid).all(dim=1)
        & torch.isfinite(execution_asks).all(dim=1)
        & torch.isfinite(total_sizes).all(dim=1)
    )
    bounded = (
        (bids >= 0).all(dim=1)
        & (bids <= 1).all(dim=1)
        & (asks > 0).all(dim=1)
        & (asks <= 1).all(dim=1)
        & (execution_asks > 0).all(dim=1)
        & (execution_asks <= 1).all(dim=1)
        & (total_sizes >= 0).all(dim=1)
    )
    snapshot_valid = timely & finite & bounded & (bids <= asks).all(dim=1)
    terminal = floats("terminal_mid_up")
    outcomes = torch.where(
        torch.isfinite(terminal),
        (terminal > 0.5).to(torch.float64),
        torch.full_like(terminal, float("nan")),
    )
    batch = DecisionBatch(
        condition_ids=tuple(str(value) for value in table["condition_id"].to_pylist()),
        assets=("BTC",) * table.num_rows,
        market_start_s=start,
        market_end_s=end,
        decision_s=decision,
        previous_s=previous,
        outcome_up=outcomes,
        n_ticks=integers("n_ticks"),
        previous_mid_up=floats("previous_mid_up"),
        current_mid_up=floats("current_mid_up"),
        current_mid=current_mid,
        bids=bids,
        asks=asks,
        ask_depth_prices=execution_asks.unsqueeze(2),
        ask_depth_sizes=executable_sizes.unsqueeze(2),
        snapshot_valid=snapshot_valid & torch.isfinite(outcomes),
        label_source="trent_final_token_mid_inferred_development_only",
    )
    return (
        batch,
        {
            "dataset_id": config.dataset_id,
            "revision": config.revision,
            "license": config.license,
            "config_sha256": config.config_sha256,
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "files": config.expected_files,
            "markets": len(batch),
            "valid_markets": int(batch.snapshot_valid.sum().item()),
            "execution_semantics": (
                "best ask with total-ask-size availability approximation"
            ),
        },
    )
