from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import torch

from polymath_1M.audit.http import UrllibJsonTransport
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


@dataclass(frozen=True)
class TrentGammaConfig:
    dataset_config: str
    gamma_base_url: str
    gamma_series_id: str
    period_start_s: int
    period_end_exclusive_s: int
    page_limit: int
    expected_fee_rate: float
    expected_fee_exponent: float
    config_sha256: str


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


def load_trent_gamma_config(path: str | Path) -> TrentGammaConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "dataset_config",
        "gamma_base_url",
        "gamma_series_id",
        "period_start",
        "period_end_exclusive",
        "page_limit",
        "expected_fee_rate",
        "expected_fee_exponent",
    }
    if payload.keys() != expected:
        raise TrentDataError(
            f"Trent Gamma fields differ: missing={sorted(expected - payload.keys())}, "
            f"extra={sorted(payload.keys() - expected)}"
        )
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    config = TrentGammaConfig(
        dataset_config=str(payload["dataset_config"]),
        gamma_base_url=str(payload["gamma_base_url"]).rstrip("/"),
        gamma_series_id=str(payload["gamma_series_id"]),
        period_start_s=_timestamp(payload["period_start"], "period_start"),
        period_end_exclusive_s=_timestamp(
            payload["period_end_exclusive"], "period_end_exclusive"
        ),
        page_limit=int(payload["page_limit"]),
        expected_fee_rate=float(payload["expected_fee_rate"]),
        expected_fee_exponent=float(payload["expected_fee_exponent"]),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )
    if (
        payload["schema_version"] != 1
        or config.gamma_base_url != "https://gamma-api.polymarket.com"
        or config.gamma_series_id != "10684"
        or config.page_limit != 100
        or config.expected_fee_rate != 0.25
        or config.expected_fee_exponent != 2.0
        or config.period_start_s >= config.period_end_exclusive_s
    ):
        raise TrentDataError("Trent Gamma config differs from the audited source")
    dataset = load_trent_config(config.dataset_config)
    if (
        config.period_start_s != dataset.period_start_s
        or config.period_end_exclusive_s != dataset.period_end_exclusive_s
    ):
        raise TrentDataError("Trent and Gamma periods differ")
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


def _local_record(relative: str, target: Path) -> dict[str, Any] | None:
    if not target.is_file() or target.stat().st_size == 0:
        return None
    return {
        "path": relative,
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
    }


def _download_one(config: TrentConfig, relative: str, target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"{config.base_url}/{config.revision}/{urllib.parse.quote(relative)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "polymath_1M/0.1 pinned-source-downloader"}
    )
    temporary = target.with_suffix(".parquet.part")
    for attempt in range(6):
        try:
            with (
                urllib.request.urlopen(request, timeout=120) as response,
                temporary.open("wb") as stream,
            ):
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            os.replace(temporary, target)
            record = _local_record(relative, target)
            if record is None:
                raise TrentDataError(f"downloaded empty Trent file: {relative}")
            return record
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == 5:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after is not None and retry_after.isdecimal()
                else 2**attempt
            )
        except URLError:
            if attempt == 5:
                raise
            delay = 2**attempt
        time.sleep(min(max(delay, 1.0), 30.0))
    raise AssertionError("unreachable")


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
        elif record is None and (local_record := _local_record(relative, target)):
            records[relative] = local_record
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


def _source_market_starts(config: TrentConfig) -> dict[str, int]:
    pattern = re.compile(
        r"^btc5m_market(\d+)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_all/"
        r"steps\.parquet$"
    )
    result: dict[str, int] = {}
    for relative in _repository_paths(config):
        match = pattern.fullmatch(relative)
        if match is None:
            raise TrentDataError(f"unexpected Trent source path: {relative}")
        market_id, timestamp = match.groups()
        start = datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=UTC)
        if market_id in result:
            raise TrentDataError(f"duplicate Trent market ID: {market_id}")
        result[market_id] = int(start.timestamp())
    return result


def _json_array(value: Any, name: str) -> list[Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list):
        raise TrentDataError(f"{name} is not an array")
    return decoded


def download_trent_gamma_outcomes(
    config_path: str | Path,
    data_root: str | Path = "data/historical",
) -> Path:
    config = load_trent_gamma_config(config_path)
    dataset = load_trent_config(config.dataset_config)
    dataset_dir = dataset.dataset_dir(data_root)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = dataset_dir / "gamma_manifest.json"
    outcomes_path = dataset_dir / "gamma_outcomes.parquet"
    if manifest_path.is_file() and outcomes_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("config_sha256") != config.config_sha256
            or manifest.get("outcomes_sha256") != _sha256(outcomes_path)
        ):
            raise TrentDataError("existing Trent Gamma archive differs")
        return manifest_path

    source_starts = _source_market_starts(dataset)
    raw_dir = dataset_dir / "gamma_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    transport = UrllibJsonTransport(timeout_seconds=30, retries=5)
    rows_by_market: dict[str, dict[str, Any]] = {}
    raw_inventory: list[dict[str, Any]] = []
    cursor: str | None = None
    page = 0
    while True:
        params: dict[str, object] = {
            "series_id": config.gamma_series_id,
            "closed": True,
            "limit": config.page_limit,
            "end_date_min": datetime.fromtimestamp(
                config.period_start_s, tz=UTC
            ).isoformat(),
            "end_date_max": datetime.fromtimestamp(
                config.period_end_exclusive_s, tz=UTC
            ).isoformat(),
        }
        if cursor is not None:
            params["after_cursor"] = cursor
        response = transport.get_json(config.gamma_base_url, "/events/keyset", params)
        if not isinstance(response.data, dict) or not isinstance(
            response.data.get("events"), list
        ):
            raise TrentDataError("Gamma keyset response is invalid")
        events = response.data["events"]
        raw_path = raw_dir / f"page_{page:05d}.json"
        temporary_raw = raw_path.with_suffix(".json.part")
        temporary_raw.write_bytes(response.body)
        os.replace(temporary_raw, raw_path)
        raw_inventory.append(
            {
                "path": str(raw_path.relative_to(dataset_dir)),
                "url": response.url,
                "retrieved_at": response.retrieved_at,
                "bytes": raw_path.stat().st_size,
                "sha256": _sha256(raw_path),
                "rows": len(events),
            }
        )
        for event in events:
            if not isinstance(event, dict):
                raise TrentDataError("Gamma event is not an object")
            start_s = _timestamp(event.get("startTime"), "event.startTime")
            if not config.period_start_s <= start_s < config.period_end_exclusive_s:
                continue
            markets = event.get("markets")
            if not isinstance(markets, list) or len(markets) != 1:
                raise TrentDataError("Gamma BTC 5m event does not have one market")
            market = markets[0]
            market_id = str(market.get("id"))
            expected_start = source_starts.get(market_id)
            if expected_start is None:
                continue
            if expected_start != start_s:
                raise TrentDataError(f"Gamma start differs for market {market_id}")
            outcomes = _json_array(market.get("outcomes"), "outcomes")
            prices = tuple(
                float(value)
                for value in _json_array(market.get("outcomePrices"), "outcomePrices")
            )
            if outcomes != ["Up", "Down"] or prices not in {
                (1.0, 0.0),
                (0.0, 1.0),
            }:
                raise TrentDataError(f"Gamma outcome differs for market {market_id}")
            schedule = market.get("feeSchedule")
            if not isinstance(schedule, dict):
                raise TrentDataError(f"Gamma fee schedule missing for {market_id}")
            fee_rate = float(schedule.get("rate"))
            fee_exponent = float(schedule.get("exponent"))
            if (
                fee_rate != config.expected_fee_rate
                or fee_exponent != config.expected_fee_exponent
                or not bool(schedule.get("takerOnly"))
            ):
                raise TrentDataError(f"Gamma fee schedule differs for {market_id}")
            condition_id = str(market.get("conditionId") or "").lower()
            if len(condition_id) != 66 or not condition_id.startswith("0x"):
                raise TrentDataError(f"Gamma condition ID invalid for {market_id}")
            if market_id in rows_by_market:
                raise TrentDataError(f"duplicate Gamma market {market_id}")
            rows_by_market[market_id] = {
                "market_id": market_id,
                "condition_id": condition_id,
                "market_start_s": start_s,
                "outcome_up": prices[0],
                "fee_rate": fee_rate,
                "fee_exponent": fee_exponent,
            }
        next_cursor = response.data.get("next_cursor")
        if not events or not next_cursor:
            break
        if str(next_cursor) == cursor:
            raise TrentDataError("Gamma cursor did not advance")
        cursor = str(next_cursor)
        page += 1
    missing = sorted(set(source_starts) - set(rows_by_market))
    if missing:
        raise TrentDataError(f"Gamma is missing {len(missing)} Trent outcomes")
    rows = sorted(rows_by_market.values(), key=lambda row: row["market_start_s"])
    temporary_outcomes = outcomes_path.with_suffix(".parquet.part")
    pq.write_table(pa.Table.from_pylist(rows), temporary_outcomes, compression="zstd")
    os.replace(temporary_outcomes, outcomes_path)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "config_path": str(config_path),
        "config_sha256": config.config_sha256,
        "gamma_base_url": config.gamma_base_url,
        "gamma_series_id": config.gamma_series_id,
        "period_start": datetime.fromtimestamp(
            config.period_start_s, tz=UTC
        ).isoformat(),
        "period_end_exclusive": datetime.fromtimestamp(
            config.period_end_exclusive_s, tz=UTC
        ).isoformat(),
        "markets": len(rows),
        "outcomes_path": outcomes_path.name,
        "outcomes_sha256": _sha256(outcomes_path),
        "expected_fee_rate": config.expected_fee_rate,
        "expected_fee_exponent": config.expected_fee_exponent,
        "raw_inventory": raw_inventory,
    }
    temporary_manifest = manifest_path.with_suffix(".json.part")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_manifest, manifest_path)
    return manifest_path


def load_trent_gamma_outcomes(
    config_path: str | Path,
    data_root: str | Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    config = load_trent_gamma_config(config_path)
    dataset = load_trent_config(config.dataset_config)
    dataset_dir = dataset.dataset_dir(data_root)
    manifest_path = dataset_dir / "gamma_manifest.json"
    outcomes_path = dataset_dir / "gamma_outcomes.parquet"
    if not manifest_path.is_file() or not outcomes_path.is_file():
        raise TrentDataError("download the authoritative Trent Gamma outcomes first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("config_sha256") != config.config_sha256
        or manifest.get("outcomes_sha256") != _sha256(outcomes_path)
    ):
        raise TrentDataError("Trent Gamma outcome archive differs")
    table = pq.read_table(outcomes_path)
    if table.num_rows != dataset.expected_files:
        raise TrentDataError("Trent Gamma outcome count differs")
    rows = {str(row["market_id"]): row for row in table.to_pylist()}
    if len(rows) != table.num_rows:
        raise TrentDataError("Trent Gamma market IDs are duplicated")
    return rows, {
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "outcomes_path": str(outcomes_path),
        "outcomes_sha256": _sha256(outcomes_path),
        "markets": len(rows),
        "label_source": "gamma_authoritative_resolved_outcome",
        "fee_rate": config.expected_fee_rate,
        "fee_exponent": config.expected_fee_exponent,
    }


def load_trent_decision_batch(
    config_path: str | Path,
    outcome_config_path: str | Path,
    data_root: str | Path,
    *,
    decision_seconds_before_end: int,
    transition_horizon_seconds: int,
    execution_latency_seconds: int,
    target_notional_usdc: float,
) -> tuple[DecisionBatch, dict[str, Any]]:
    config = load_trent_config(config_path)
    gamma, gamma_provenance = load_trent_gamma_outcomes(
        outcome_config_path, data_root
    )
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
                down_ask_size_total,
                up_mid IS NOT NULL AS previous_valid,
                up_best_bid IS NOT NULL
                    AND up_best_ask IS NOT NULL
                    AND up_mid IS NOT NULL
                    AND down_best_bid IS NOT NULL
                    AND down_best_ask IS NOT NULL
                    AND down_mid IS NOT NULL AS top_valid,
                up_best_ask IS NOT NULL
                    AND up_ask_size_total IS NOT NULL
                    AND down_best_ask IS NOT NULL
                    AND down_ask_size_total IS NOT NULL AS execution_valid
            FROM read_parquet('{glob}', filename=true)
        )
        SELECT
            regexp_extract(filename, 'btc5m_market(\\d+)_', 1) AS market_id,
            market_start_s,
            count(*) AS n_ticks,
            arg_max(up_mid, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {previous_offset_ms}
                  AND previous_valid
            ) AS previous_mid_up,
            max(ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {previous_offset_ms}
                  AND previous_valid
            ) AS previous_ts,
            arg_max(up_best_bid, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
                  AND top_valid
            ) AS current_bid_up,
            arg_max(up_best_ask, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
                  AND top_valid
            ) AS current_ask_up,
            arg_max(up_mid, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
                  AND top_valid
            ) AS current_mid_up,
            arg_max(down_best_bid, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
                  AND top_valid
            ) AS current_bid_down,
            arg_max(down_best_ask, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
                  AND top_valid
            ) AS current_ask_down,
            arg_max(down_mid, ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
                  AND top_valid
            ) AS current_mid_down,
            max(ts) FILTER (
                WHERE ts <= market_start_s * 1000 + {decision_offset_ms}
                  AND top_valid
            ) AS current_ts,
            arg_min(up_best_ask, ts) FILTER (
                WHERE ts >= market_start_s * 1000 + {execution_offset_ms}
                  AND execution_valid
            ) AS execution_ask_up,
            arg_min(up_ask_size_total, ts) FILTER (
                WHERE ts >= market_start_s * 1000 + {execution_offset_ms}
                  AND execution_valid
            ) AS execution_size_up,
            arg_min(down_best_ask, ts) FILTER (
                WHERE ts >= market_start_s * 1000 + {execution_offset_ms}
                  AND execution_valid
            ) AS execution_ask_down,
            arg_min(down_ask_size_total, ts) FILTER (
                WHERE ts >= market_start_s * 1000 + {execution_offset_ms}
                  AND execution_valid
            ) AS execution_size_down,
            min(ts) FILTER (
                WHERE ts >= market_start_s * 1000 + {execution_offset_ms}
                  AND execution_valid
            ) AS execution_ts
        FROM raw
        GROUP BY filename, market_start_s
        ORDER BY market_start_s, market_id
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
    market_ids = tuple(str(value) for value in table["market_id"].to_pylist())
    if any(market_id not in gamma for market_id in market_ids):
        raise TrentDataError("Gamma outcome is missing from adapted Trent rows")
    outcomes = torch.tensor(
        [float(gamma[market_id]["outcome_up"]) for market_id in market_ids],
        dtype=torch.float64,
    )
    condition_ids = tuple(str(gamma[value]["condition_id"]) for value in market_ids)
    batch = DecisionBatch(
        condition_ids=condition_ids,
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
        snapshot_valid=snapshot_valid,
        label_source="gamma_authoritative_resolved_outcome",
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
            "gamma": gamma_provenance,
        },
    )
