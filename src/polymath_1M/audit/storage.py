from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from .http import JsonResponse

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True)
class ManifestEntry:
    account_id: str
    endpoint: str
    url: str
    retrieved_at: str
    status: int
    sha256: str
    bytes: int
    relative_path: str


class AuditStorage:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.raw_dir = run_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_log_path = run_dir / "raw_manifest.jsonl"
        self.manifest = self._load_manifest()
        self._manifest_stream = self.manifest_log_path.open("a", encoding="utf-8")
        self._archive_lock = Lock()
        self.database_path = run_dir / "audit.sqlite3"
        self.connection = sqlite3.connect(self.database_path)
        self._create_schema()

    def archive(self, account_id: str, endpoint: str, response: JsonResponse) -> None:
        digest = hashlib.sha256(response.body).hexdigest()
        url_digest = hashlib.sha256(response.url.encode()).hexdigest()[:16]
        endpoint_dir = (
            self.raw_dir / _SAFE.sub("_", account_id) / _SAFE.sub("_", endpoint)
        )
        endpoint_dir.mkdir(parents=True, exist_ok=True)
        path = endpoint_dir / f"{url_digest}_{digest[:16]}.json"
        with self._archive_lock:
            if not path.exists():
                path.write_bytes(response.body)
            entry = ManifestEntry(
                account_id=account_id,
                endpoint=endpoint,
                url=response.url,
                retrieved_at=response.retrieved_at,
                status=response.status,
                sha256=digest,
                bytes=len(response.body),
                relative_path=str(path.relative_to(self.run_dir)),
            )
            self.manifest.append(entry)
            self._manifest_stream.write(_json(asdict(entry)) + "\n")
            self._manifest_stream.flush()

    def store_profile(
        self, account_id: str, source_address: str, profile: dict[str, Any]
    ) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO profiles
               (account_id, source_address, proxy_wallet, name, pseudonym, created_at, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                account_id,
                source_address,
                profile.get("proxyWallet"),
                profile.get("name"),
                profile.get("pseudonym"),
                profile.get("createdAt"),
                _json(profile),
            ),
        )
        self.connection.commit()

    def store_rows(
        self, table: str, account_id: str, rows: Iterable[dict[str, Any]]
    ) -> int:
        if table not in {"activity", "trades", "closed_positions"}:
            raise ValueError(f"unsupported account table: {table}")
        values = []
        for row in rows:
            raw_json = _json(row)
            values.append(
                (
                    account_id,
                    hashlib.sha256(raw_json.encode()).hexdigest(),
                    _int(row.get("timestamp")),
                    row.get("conditionId"),
                    row.get("asset"),
                    row.get("type"),
                    row.get("side"),
                    row.get("outcome"),
                    _float(row.get("size")),
                    _float(row.get("usdcSize")),
                    _float(row.get("price")),
                    _float(row.get("realizedPnl")),
                    _signed_cash(row) if table == "activity" else 0.0,
                    _reward_cash(row) if table == "activity" else 0.0,
                    row.get("transactionHash"),
                    row.get("slug"),
                    row.get("title"),
                    raw_json,
                )
            )
        changes_before = self.connection.total_changes
        self.connection.executemany(
            f"""INSERT OR IGNORE INTO {table}
                (account_id, row_hash, timestamp, condition_id, asset, activity_type,
                 side, outcome, size, usdc_size, price, realized_pnl, cash_flow, reward_cash,
                 transaction_hash, slug, title, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
        self.connection.commit()
        return self.connection.total_changes - changes_before

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.run_dir / name
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return path

    def write_csv(self, name: str, rows: list[dict[str, Any]]) -> Path:
        path = self.run_dir / name
        fieldnames = sorted({key for row in rows for key in row})
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def finalize_manifest(self, metadata: dict[str, Any]) -> Path:
        unique = {
            (entry.account_id, entry.endpoint, entry.url, entry.sha256): entry
            for entry in self.manifest
        }
        return self.write_json(
            "raw_manifest.json",
            {
                "metadata": metadata,
                "requests": [
                    asdict(entry)
                    for entry in sorted(
                        unique.values(),
                        key=lambda item: (
                            item.account_id,
                            item.endpoint,
                            item.url,
                            item.retrieved_at,
                        ),
                    )
                ],
            },
        )

    def collection_complete(self, account_id: str, endpoint: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM collection_state WHERE account_id = ? AND endpoint = ?",
                (account_id, endpoint),
            ).fetchone()
            is not None
        )

    def mark_collection_complete(
        self, account_id: str, endpoint: str, row_count: int
    ) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO collection_state
               (account_id, endpoint, row_count) VALUES (?, ?, ?)""",
            (account_id, endpoint, row_count),
        )
        self.connection.commit()

    def close(self) -> None:
        self._manifest_stream.close()
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS profiles (
                account_id TEXT PRIMARY KEY,
                source_address TEXT NOT NULL,
                proxy_wallet TEXT,
                name TEXT,
                pseudonym TEXT,
                created_at TEXT,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                account_id TEXT NOT NULL, row_hash TEXT NOT NULL, timestamp INTEGER,
                condition_id TEXT, asset TEXT, activity_type TEXT, side TEXT, outcome TEXT,
                size REAL, usdc_size REAL, price REAL, realized_pnl REAL,
                cash_flow REAL NOT NULL, reward_cash REAL NOT NULL,
                transaction_hash TEXT, slug TEXT, title TEXT, raw_json TEXT NOT NULL,
                PRIMARY KEY (account_id, row_hash)
            );
            CREATE TABLE IF NOT EXISTS trades (
                account_id TEXT NOT NULL, row_hash TEXT NOT NULL, timestamp INTEGER,
                condition_id TEXT, asset TEXT, activity_type TEXT, side TEXT, outcome TEXT,
                size REAL, usdc_size REAL, price REAL, realized_pnl REAL,
                cash_flow REAL NOT NULL, reward_cash REAL NOT NULL,
                transaction_hash TEXT, slug TEXT, title TEXT, raw_json TEXT NOT NULL,
                PRIMARY KEY (account_id, row_hash)
            );
            CREATE TABLE IF NOT EXISTS closed_positions (
                account_id TEXT NOT NULL, row_hash TEXT NOT NULL, timestamp INTEGER,
                condition_id TEXT, asset TEXT, activity_type TEXT, side TEXT, outcome TEXT,
                size REAL, usdc_size REAL, price REAL, realized_pnl REAL,
                cash_flow REAL NOT NULL, reward_cash REAL NOT NULL,
                transaction_hash TEXT, slug TEXT, title TEXT, raw_json TEXT NOT NULL,
                PRIMARY KEY (account_id, row_hash)
            );
            CREATE TABLE IF NOT EXISTS collection_state (
                account_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                PRIMARY KEY (account_id, endpoint)
            );
            CREATE INDEX IF NOT EXISTS activity_account_timestamp
                ON activity(account_id, timestamp);
            CREATE INDEX IF NOT EXISTS trades_account_timestamp
                ON trades(account_id, timestamp);
            CREATE INDEX IF NOT EXISTS closed_positions_account_timestamp
                ON closed_positions(account_id, timestamp);
            """
        )
        self.connection.commit()

    def _load_manifest(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        path = self.run_dir / "raw_manifest.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            entries.extend(
                ManifestEntry(**item) for item in payload.get("requests", [])
            )
        if self.manifest_log_path.exists():
            entries.extend(
                ManifestEntry(**json.loads(line))
                for line in self.manifest_log_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            )
        return entries


def build_raw_inventory(run_dir: str | Path) -> tuple[Path, Path]:
    root = Path(run_dir)
    raw_dir = root / "raw"
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"raw directory not found: {raw_dir}")
    known = _known_manifest_paths(root)
    inventory_temp = root / "raw_inventory.json.tmp"
    inventory_path = root / "raw_inventory.json"
    endpoint_counts: dict[str, int] = {}
    metadata_files = 0
    manifest_hash_mismatches = 0
    total_bytes = 0
    file_count = 0
    with inventory_temp.open("w", encoding="utf-8") as stream:
        stream.write('{"files":[\n')
        for path in sorted(raw_dir.rglob("*.json")):
            relative_path = str(path.relative_to(root))
            stat = path.stat()
            manifest_entry = known.get(relative_path)
            digest = _file_sha256(path)
            manifest_hash_matches = (
                digest == manifest_entry.sha256 if manifest_entry is not None else None
            )
            manifest_hash_mismatches += manifest_hash_matches is False
            account_id, endpoint = path.relative_to(raw_dir).parts[:2]
            endpoint_key = f"{account_id}/{endpoint}"
            endpoint_counts[endpoint_key] = endpoint_counts.get(endpoint_key, 0) + 1
            metadata_files += int(manifest_entry is not None)
            total_bytes += stat.st_size
            entry = {
                "account_id": account_id,
                "endpoint": endpoint,
                "relative_path": relative_path,
                "sha256": digest,
                "bytes": stat.st_size,
                "mtime_utc": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                "request_metadata_present": manifest_entry is not None,
                "manifest_hash_matches": manifest_hash_matches,
                "provenance_status": (
                    "request_manifest"
                    if manifest_entry is not None
                    else "recovered_file_only"
                ),
            }
            if file_count:
                stream.write(",\n")
            stream.write(_json(entry))
            file_count += 1
        stream.write("\n]}\n")
    inventory_temp.replace(inventory_path)
    summary = {
        "files": file_count,
        "bytes": total_bytes,
        "request_metadata_files": metadata_files,
        "request_metadata_coverage": metadata_files / file_count if file_count else 1.0,
        "all_file_bytes_rehashed": True,
        "manifest_hash_mismatches": manifest_hash_mismatches,
        "recovered_file_only": file_count - metadata_files,
        "endpoint_file_counts": dict(sorted(endpoint_counts.items())),
        "inventory_sha256": _file_sha256(inventory_path),
    }
    summary_path = root / "raw_inventory_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return inventory_path, summary_path


def _known_manifest_paths(root: Path) -> dict[str, ManifestEntry]:
    entries: list[ManifestEntry] = []
    manifest_path = root / "raw_manifest.json"
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries.extend(ManifestEntry(**item) for item in payload.get("requests", []))
    log_path = root / "raw_manifest.jsonl"
    if log_path.exists():
        entries.extend(
            ManifestEntry(**json.loads(line))
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return {entry.relative_path: entry for entry in entries}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _signed_cash(row: dict[str, Any]) -> float:
    amount = _float(row.get("usdcSize")) or 0.0
    activity_type = str(row.get("type") or "")
    side = str(row.get("side") or "")
    if activity_type == "TRADE":
        return -amount if side == "BUY" else amount if side == "SELL" else 0.0
    if activity_type == "SPLIT":
        return -amount
    if activity_type in {
        "MERGE",
        "REDEEM",
        "REWARD",
        "MAKER_REBATE",
        "TAKER_REBATE",
        "REFERRAL_REWARD",
        "YIELD",
    }:
        return amount
    return 0.0


def _reward_cash(row: dict[str, Any]) -> float:
    return (
        _float(row.get("usdcSize")) or 0.0
        if row.get("type")
        in {"REWARD", "MAKER_REBATE", "TAKER_REBATE", "REFERRAL_REWARD", "YIELD"}
        else 0.0
    )
