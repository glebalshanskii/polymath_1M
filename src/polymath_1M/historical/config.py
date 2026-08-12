from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class HistoricalConfigError(ValueError):
    """A pinned historical dataset config is incomplete or unsafe."""


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DatasetFile:
    asset: str
    kind: str
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class KachoDatasetConfig:
    schema_version: int
    dataset_id: str
    revision: str
    license: str
    base_url: str
    files: tuple[DatasetFile, ...]
    config_sha256: str

    @property
    def assets(self) -> tuple[str, ...]:
        return tuple(sorted({item.asset for item in self.files}))

    def select(
        self,
        assets: tuple[str, ...] | list[str] | None = None,
        kinds: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[DatasetFile, ...]:
        selected_assets = set(self.assets if assets is None else assets)
        selected_kinds = {"markets", "ticks"} if kinds is None else set(kinds)
        unknown_assets = selected_assets - set(self.assets)
        if unknown_assets:
            raise HistoricalConfigError(
                f"unknown Kacho assets: {sorted(unknown_assets)}"
            )
        if not selected_kinds or selected_kinds - {"markets", "ticks"}:
            raise HistoricalConfigError(
                f"invalid Kacho file kinds: {sorted(selected_kinds)}"
            )
        chosen = tuple(
            item
            for item in self.files
            if item.asset in selected_assets and item.kind in selected_kinds
        )
        expected = len(selected_assets) * len(selected_kinds)
        if len(chosen) != expected:
            raise HistoricalConfigError(
                f"config has {len(chosen)} selected files, expected {expected}"
            )
        return chosen

    def dataset_dir(self, root: str | Path) -> Path:
        safe_id = self.dataset_id.replace("/", "--")
        return Path(root) / safe_id / self.revision


def load_kacho_dataset_config(path: str | Path) -> KachoDatasetConfig:
    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "dataset_id",
        "revision",
        "license",
        "base_url",
        "files",
    }
    missing = required - payload.keys()
    if missing:
        raise HistoricalConfigError(f"missing dataset config fields: {sorted(missing)}")
    if payload["schema_version"] != 1:
        raise HistoricalConfigError("only Kacho dataset schema_version=1 is supported")
    if len(payload["revision"]) != 40:
        raise HistoricalConfigError("dataset revision must be an immutable 40-char SHA")
    files: list[DatasetFile] = []
    seen: set[tuple[str, str]] = set()
    for raw in payload["files"]:
        item = DatasetFile(
            asset=str(raw["asset"]).upper(),
            kind=str(raw["kind"]),
            path=str(raw["path"]),
            bytes=int(raw["bytes"]),
            sha256=str(raw["sha256"]).lower(),
        )
        key = (item.asset, item.kind)
        if key in seen:
            raise HistoricalConfigError(f"duplicate dataset file mapping: {key}")
        seen.add(key)
        if item.kind not in {"markets", "ticks"}:
            raise HistoricalConfigError(f"unsupported file kind: {item.kind}")
        if Path(item.path).name != item.path or item.bytes <= 0:
            raise HistoricalConfigError(f"unsafe dataset file metadata: {item.path}")
        if len(item.sha256) != 64:
            raise HistoricalConfigError(f"invalid SHA-256 for {item.path}")
        files.append(item)
    config = KachoDatasetConfig(
        schema_version=1,
        dataset_id=str(payload["dataset_id"]),
        revision=str(payload["revision"]),
        license=str(payload["license"]),
        base_url=str(payload["base_url"]).rstrip("/"),
        files=tuple(files),
        config_sha256=_canonical_hash(payload),
    )
    for asset in config.assets:
        config.select([asset])
    return config
