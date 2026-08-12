from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.dataset as ds

from .config import load_kacho_dataset_config
from .download import validate_kacho_files
from .pmxt import load_pmxt_archive_config, query_pmxt_hour, replay_pmxt_book


class OverlapConfigError(ValueError):
    """The fixed PMXT/Kacho overlap smoke contract is invalid."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def run_pmxt_overlap_smoke(
    config_path: str | Path,
    output_root: str | Path = "outputs/overlap",
) -> Path:
    payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "experiment_id",
        "pmxt_config",
        "kacho_config",
        "kacho_root",
        "hour",
        "condition_id",
        "token_up",
        "token_down",
        "decision_timestamp_ms",
        "maximum_top_price_difference",
    }
    if payload.keys() != required:
        raise OverlapConfigError(
            f"overlap config fields differ: missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    pmxt_config = load_pmxt_archive_config(payload["pmxt_config"])
    kacho_config = load_kacho_dataset_config(payload["kacho_config"])
    dataset_dir, _ = validate_kacho_files(
        kacho_config, payload["kacho_root"], assets=["BTC"]
    )
    _, pmxt = query_pmxt_hour(pmxt_config, payload["hour"], [payload["condition_id"]])
    snapshot = replay_pmxt_book(
        pmxt,
        condition_id=payload["condition_id"],
        token_ids=(payload["token_up"], payload["token_down"]),
        decision_timestamp_ms=int(payload["decision_timestamp_ms"]),
    )
    tick_spec = next(
        item
        for item in kacho_config.files
        if item.asset == "BTC" and item.kind == "ticks"
    )
    ticks = ds.dataset(dataset_dir / tick_spec.path, format="parquet").to_table(
        columns=["condition_id", "t", "bu", "au", "bd", "ad"],
        filter=(
            (ds.field("condition_id") == payload["condition_id"])
            & (ds.field("t") == int(payload["decision_timestamp_ms"]) // 1000)
        ),
    )
    if ticks.num_rows != 1:
        raise OverlapConfigError(
            f"expected one exact Kacho overlap tick, received {ticks.num_rows}"
        )
    kacho = {key: float(ticks[key][0].as_py()) for key in ("bu", "au", "bd", "ad")}
    pmxt_top = {
        "bu": snapshot.bids[0].item(),
        "au": snapshot.asks[0].item(),
        "bd": snapshot.bids[1].item(),
        "ad": snapshot.asks[1].item(),
    }
    differences = {key: abs(pmxt_top[key] - kacho[key]) for key in kacho}
    maximum_difference = max(differences.values())
    crossed = bool(
        pc.any(pc.greater(ticks["bu"], ticks["au"])).as_py()
        or pc.any(pc.greater(ticks["bd"], ticks["ad"])).as_py()
    )
    passed = (
        snapshot.initialized
        and not crossed
        and maximum_difference <= float(payload["maximum_top_price_difference"])
    )
    started_at = datetime.now(UTC)
    run_dir = Path(output_root) / (
        f"{started_at.strftime('%Y%m%dT%H%M%SZ')}_{payload['experiment_id']}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    output = {
        "schema_version": 1,
        "status": "match" if passed else "mismatch",
        "condition_id": payload["condition_id"],
        "decision_timestamp_ms": payload["decision_timestamp_ms"],
        "pmxt_event_rows": snapshot.event_rows,
        "pmxt_ignored_pre_snapshot_rows": snapshot.ignored_pre_snapshot_rows,
        "pmxt_initial_snapshot_received_ms": snapshot.initial_snapshot_received_ms,
        "pmxt_initialized": snapshot.initialized,
        "pmxt_top": pmxt_top,
        "kacho_top": kacho,
        "absolute_differences": differences,
        "maximum_top_price_difference": maximum_difference,
        "allowed_difference": payload["maximum_top_price_difference"],
        "kacho_crossed": crossed,
        "pmxt_config_sha256": pmxt_config.config_sha256,
        "kacho_config_sha256": kacho_config.config_sha256,
        "kacho_manifest_sha256": _sha256(dataset_dir / "manifest.json"),
    }
    output_path = run_dir / "overlap_summary.json"
    _write_json(output_path, output)
    if not passed:
        raise RuntimeError(f"PMXT/Kacho overlap smoke mismatch; see {output_path}")
    return output_path
