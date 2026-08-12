from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from .book import BookDataError, OrderBookStore
from .storage import iter_raw_frames


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_inventory(root: Path) -> list[str]:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return [f"manifest not found: {manifest_path}"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = {
        str(item["relative_path"]): item for item in manifest.get("raw_inventory", [])
    }
    actual = {
        str(path.relative_to(root)): path
        for path in (root / "raw").rglob("*")
        if path.is_file()
    }
    errors: list[str] = []
    if declared.keys() != actual.keys():
        errors.append(
            f"raw inventory paths differ: missing={sorted(declared.keys() - actual.keys())}, "
            f"extra={sorted(actual.keys() - declared.keys())}"
        )
    for relative_path in sorted(declared.keys() & actual.keys()):
        item = declared[relative_path]
        path = actual[relative_path]
        if path.stat().st_size != int(item["bytes"]):
            errors.append(f"raw size mismatch: {relative_path}")
        if _sha256(path) != str(item["sha256"]):
            errors.append(f"raw SHA-256 mismatch: {relative_path}")
    return errors


def _validate_frame_sequence(raw_dir: Path) -> tuple[int, list[str]]:
    records = 0
    expected_sequence = 0
    errors: list[str] = []
    for path in sorted(raw_dir.glob("*.frames.gz")):
        try:
            for frame_number, record in enumerate(iter_raw_frames(path), start=1):
                records += 1
                if record.sequence != expected_sequence:
                    errors.append(
                        f"{path.name}:{frame_number}: sequence {record.sequence}, "
                        f"expected {expected_sequence}"
                    )
                expected_sequence = record.sequence + 1
        except (EOFError, OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
    if records == 0:
        errors.append(f"no raw frames in {raw_dir}")
    return records, errors


def replay_run(run_dir: str | Path) -> Path:
    root = Path(run_dir)
    raw_dir = root / "raw" / "websocket" / "clob_market"
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"CLOB raw stream directory not found: {raw_dir}")
    live_path = root / "live_book_summary.json"
    if not live_path.is_file():
        raise FileNotFoundError(f"live book summary not found: {live_path}")
    books = OrderBookStore()
    records = 0
    inbound_messages = 0
    errors = _validate_inventory(root)
    expected_sequence = 0
    for path in sorted(raw_dir.glob("*.frames.gz")):
        try:
            for frame_number, record in enumerate(iter_raw_frames(path), start=1):
                records += 1
                sequence = record.sequence
                if sequence != expected_sequence:
                    errors.append(
                        f"{path.name}:{frame_number}: sequence {sequence}, "
                        f"expected {expected_sequence}"
                    )
                expected_sequence = sequence + 1
                if record.direction != "inbound":
                    continue
                raw = record.raw
                if raw.strip() in {"", "PONG"}:
                    continue
                inbound_messages += 1
                books.apply_raw(raw)
        except (
            EOFError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            BookDataError,
        ) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
    if records == 0:
        errors.append(f"no CLOB raw frames in {raw_dir}")
    rtds_records, rtds_errors = _validate_frame_sequence(
        root / "raw" / "websocket" / "rtds_reference"
    )
    errors.extend(rtds_errors)
    replayed = books.summary()
    live = json.loads(live_path.read_text(encoding="utf-8"))
    match = (
        not errors
        and inbound_messages > 0
        and replayed["global_digest"] == live.get("global_digest")
        and replayed["asset_count"] == live.get("asset_count")
        and replayed["initialized_asset_count"] == live.get("initialized_asset_count")
    )
    output = {
        "schema_version": 1,
        "status": "match" if match else "mismatch",
        "records": records,
        "inbound_messages": inbound_messages,
        "rtds_records": rtds_records,
        "live_global_digest": live.get("global_digest"),
        "replay_global_digest": replayed["global_digest"],
        "live_asset_count": live.get("asset_count"),
        "replay_asset_count": replayed["asset_count"],
        "errors": errors,
        "replayed_book_summary": replayed,
    }
    output_path = root / "replay_summary.json"
    _write_json(output_path, output)
    if not match:
        raise RuntimeError(f"deterministic replay mismatch; see {output_path}")
    return output_path
