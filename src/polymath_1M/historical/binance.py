from __future__ import annotations

import csv
import hashlib
import json
import os
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import torch


class BinanceContextError(RuntimeError):
    """Pinned Binance visualization context is missing or invalid."""


@dataclass(frozen=True)
class BinanceFileSpec:
    url: str
    sha256: str

    @property
    def filename(self) -> str:
        return Path(urlparse(self.url).path).name


@dataclass(frozen=True)
class BinanceContextConfig:
    schema_version: int
    dataset_id: str
    provider: str
    symbol: str
    interval: str
    context_only: bool
    files: tuple[BinanceFileSpec, ...]
    config_sha256: str


@dataclass(frozen=True)
class BinanceKlines:
    open_time_s: torch.Tensor
    open: torch.Tensor
    high: torch.Tensor
    low: torch.Tensor
    close: torch.Tensor

    def __len__(self) -> int:
        return self.open_time_s.numel()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_binance_context_config(path: str | Path) -> BinanceContextConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "dataset_id",
        "provider",
        "symbol",
        "interval",
        "context_only",
        "files",
    }
    if payload.keys() != required:
        raise BinanceContextError(
            f"Binance config fields differ: missing={sorted(required - payload.keys())}, "
            f"extra={sorted(payload.keys() - required)}"
        )
    if (
        payload["schema_version"] != 1
        or payload["provider"] != "Binance Public Data"
        or payload["symbol"] != "BTCUSDT"
        or payload["interval"] != "1m"
        or payload["context_only"] is not True
    ):
        raise BinanceContextError("Binance source must be pinned BTCUSDT 1m context")
    raw_files = payload["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise BinanceContextError("Binance context requires pinned source files")
    files: list[BinanceFileSpec] = []
    for item in raw_files:
        if not isinstance(item, dict) or item.keys() != {"url", "sha256"}:
            raise BinanceContextError("Binance file spec is invalid")
        url = str(item["url"])
        digest = str(item["sha256"]).lower()
        if (
            not url.startswith("https://data.binance.vision/data/spot/")
            or "/klines/BTCUSDT/1m/BTCUSDT-1m-2026-" not in url
            or len(digest) != 64
            or any(value not in "0123456789abcdef" for value in digest)
        ):
            raise BinanceContextError("Binance URL or SHA-256 is invalid")
        files.append(BinanceFileSpec(url=url, sha256=digest))
    if len({item.filename for item in files}) != len(files):
        raise BinanceContextError("Binance context contains duplicate files")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return BinanceContextConfig(
        schema_version=1,
        dataset_id=str(payload["dataset_id"]),
        provider=str(payload["provider"]),
        symbol=str(payload["symbol"]),
        interval=str(payload["interval"]),
        context_only=True,
        files=tuple(files),
        config_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _download(url: str, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "polymath_1M/0.1 pinned-context-downloader"},
    )
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            temporary.open("wb") as stream,
        ):
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def download_binance_context(
    config_path: str | Path,
    data_root: str | Path = "data/historical",
) -> Path:
    config = load_binance_context_config(config_path)
    dataset_dir = Path(data_root) / config.dataset_id
    dataset_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for spec in config.files:
        destination = dataset_dir / spec.filename
        if not destination.is_file() or _sha256(destination) != spec.sha256:
            _download(spec.url, destination)
        actual = _sha256(destination)
        if actual != spec.sha256:
            raise BinanceContextError(
                f"Binance SHA-256 mismatch for {spec.filename}: {actual}"
            )
        records.append(
            {
                "url": spec.url,
                "path": str(destination),
                "bytes": destination.stat().st_size,
                "sha256": actual,
            }
        )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "config_path": str(config_path),
        "config_sha256": config.config_sha256,
        "provider": config.provider,
        "symbol": config.symbol,
        "interval": config.interval,
        "context_only": True,
        "files": records,
    }
    manifest_path = dataset_dir / "manifest.json"
    temporary = manifest_path.with_suffix(".json.part")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)
    return manifest_path


def _epoch_seconds(raw: int) -> int:
    if raw >= 100_000_000_000_000:
        return raw // 1_000_000
    if raw >= 100_000_000_000:
        return raw // 1_000
    raise BinanceContextError("Binance timestamp has unsupported precision")


def load_binance_klines(
    config_path: str | Path,
    data_root: str | Path,
    *,
    start_s: int,
    end_exclusive_s: int,
) -> tuple[BinanceKlines, dict[str, Any]]:
    config = load_binance_context_config(config_path)
    if end_exclusive_s <= start_s:
        raise BinanceContextError("Binance context interval is empty")
    dataset_dir = Path(data_root) / config.dataset_id
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BinanceContextError("download pinned Binance context first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("config_sha256") != config.config_sha256
        or manifest.get("context_only") is not True
    ):
        raise BinanceContextError("Binance manifest differs from pinned config")
    timestamps: list[int] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    files: list[dict[str, Any]] = []
    for spec in config.files:
        path = dataset_dir / spec.filename
        actual = _sha256(path)
        if actual != spec.sha256:
            raise BinanceContextError(f"Binance file changed: {spec.filename}")
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".csv")]
            if len(names) != 1 or Path(names[0]).name != names[0]:
                raise BinanceContextError("Binance archive layout is invalid")
            with archive.open(names[0], "r") as raw_stream:
                rows = csv.reader(
                    (line.decode("utf-8") for line in raw_stream),
                    delimiter=",",
                )
                for row in rows:
                    if len(row) != 12:
                        raise BinanceContextError("Binance kline row width differs")
                    timestamp = _epoch_seconds(int(row[0]))
                    if start_s <= timestamp < end_exclusive_s:
                        timestamps.append(timestamp)
                        opens.append(float(row[1]))
                        highs.append(float(row[2]))
                        lows.append(float(row[3]))
                        closes.append(float(row[4]))
        files.append(
            {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": actual,
            }
        )
    order = torch.argsort(torch.tensor(timestamps, dtype=torch.int64), stable=True)
    time_tensor = torch.tensor(timestamps, dtype=torch.int64)[order]
    values = [
        torch.tensor(items, dtype=torch.float64)[order]
        for items in (opens, highs, lows, closes)
    ]
    if time_tensor.numel() == 0 or bool(
        (time_tensor[1:] - time_tensor[:-1] != 60).any().item()
    ):
        raise BinanceContextError("Binance 1m context is empty or has time gaps")
    finite = torch.isfinite(torch.stack(values)).all(dim=0)
    positive = torch.stack(values) > 0
    if not bool(finite.all().item()) or not bool(positive.all().item()):
        raise BinanceContextError("Binance prices are nonfinite or nonpositive")
    return (
        BinanceKlines(
            open_time_s=time_tensor,
            open=values[0],
            high=values[1],
            low=values[2],
            close=values[3],
        ),
        {
            "config_sha256": config.config_sha256,
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "provider": config.provider,
            "symbol": config.symbol,
            "interval": config.interval,
            "context_only": True,
            "rows": int(time_tensor.numel()),
            "files": files,
        },
    )
