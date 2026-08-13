from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import DatasetFile, KachoDatasetConfig, load_kacho_dataset_config


class DatasetIntegrityError(RuntimeError):
    """Downloaded bytes do not match the pinned dataset contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path, spec: DatasetFile) -> tuple[int, str]:
    size = path.stat().st_size
    if size != spec.bytes:
        raise DatasetIntegrityError(
            f"{path} has {size} bytes; pinned size is {spec.bytes}"
        )
    digest = _sha256(path)
    if digest != spec.sha256:
        raise DatasetIntegrityError(
            f"{path} SHA-256 {digest} does not match pinned {spec.sha256}"
        )
    return size, digest


def _download_file(
    config: KachoDatasetConfig,
    spec: DatasetFile,
    destination: Path,
    *,
    retries: int,
) -> None:
    url = f"{config.base_url}/{config.revision}/{spec.path}"
    temporary = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": "polymath-1M/0.1"})
            with urlopen(request, timeout=60) as response, temporary.open("wb") as out:
                while chunk := response.read(4 * 1024 * 1024):
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            _verify(temporary, spec)
            os.replace(temporary, destination)
            return
        except (
            DatasetIntegrityError,
            HTTPError,
            OSError,
            TimeoutError,
            URLError,
        ) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2**attempt, 8))
    raise DatasetIntegrityError(f"failed to download pinned {spec.path}: {last_error}")


def _load_existing_manifest(path: Path, config: KachoDatasetConfig) -> dict[str, Any]:
    if not path.is_file():
        return {}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (
        manifest.get("dataset_id") != config.dataset_id
        or manifest.get("revision") != config.revision
        or manifest.get("config_sha256") != config.config_sha256
    ):
        raise DatasetIntegrityError(
            f"existing manifest {path} belongs to a different dataset contract"
        )
    return {str(item["path"]): item for item in manifest.get("files", [])}


def _write_manifest(
    path: Path,
    config: KachoDatasetConfig,
    inventory: dict[str, dict[str, Any]],
) -> None:
    payload = {
        "schema_version": 1,
        "dataset_id": config.dataset_id,
        "revision": config.revision,
        "license": config.license,
        "config_sha256": config.config_sha256,
        "updated_at": datetime.now(UTC).isoformat(),
        "files": [inventory[key] for key in sorted(inventory)],
    }
    temporary = path.with_suffix(".json.part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def download_kacho_dataset(
    config_path: str | Path,
    data_root: str | Path,
    *,
    assets: tuple[str, ...] | list[str] | None = None,
    kinds: tuple[str, ...] | list[str] | None = None,
    retries: int = 3,
) -> Path:
    """Download and verify an exact subset of the pinned Kacho release."""

    config = load_kacho_dataset_config(config_path)
    normalized_assets = (
        None if assets is None else tuple(asset.upper() for asset in assets)
    )
    selected = config.select(normalized_assets, kinds)
    dataset_dir = config.dataset_dir(data_root)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = dataset_dir / "manifest.json"
    inventory = _load_existing_manifest(manifest_path, config)
    changed = not manifest_path.is_file()
    for spec in selected:
        destination = dataset_dir / spec.path
        reused = False
        if destination.is_file():
            _verify(destination, spec)
            reused = True
        else:
            _download_file(config, spec, destination, retries=retries)
        size, digest = _verify(destination, spec)
        next_item = {
            "asset": spec.asset,
            "kind": spec.kind,
            "path": spec.path,
            "bytes": size,
            "sha256": digest,
            "source_url": (f"{config.base_url}/{config.revision}/{spec.path}"),
            "verified_at": datetime.now(UTC).isoformat(),
            "reused": reused,
        }
        existing = inventory.get(spec.path)
        if existing is None or any(
            existing.get(key) != next_item[key]
            for key in ("asset", "kind", "path", "bytes", "sha256", "source_url")
        ):
            inventory[spec.path] = next_item
            changed = True
    if changed:
        _write_manifest(manifest_path, config, inventory)
    return manifest_path


def validate_kacho_files(
    config: KachoDatasetConfig,
    data_root: str | Path,
    *,
    assets: tuple[str, ...] | list[str],
) -> tuple[Path, dict[str, dict[str, Any]]]:
    dataset_dir = config.dataset_dir(data_root)
    manifest_path = dataset_dir / "manifest.json"
    inventory = _load_existing_manifest(manifest_path, config)
    for spec in config.select(assets):
        path = dataset_dir / spec.path
        if not path.is_file():
            raise FileNotFoundError(
                f"pinned dataset file is missing: {path}; run kacho-download first"
            )
        size, digest = _verify(path, spec)
        item = inventory.get(spec.path)
        if item is None or int(item.get("bytes", -1)) != size:
            raise DatasetIntegrityError(f"manifest does not inventory {spec.path}")
        if str(item.get("sha256")) != digest:
            raise DatasetIntegrityError(f"manifest SHA-256 differs for {spec.path}")
    return dataset_dir, inventory
