from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
import pyarrow.parquet as pq
import torch

from polymath_1M.historical.config import load_kacho_dataset_config
from polymath_1M.historical.kacho import load_kacho_decision_batch
from polymath_1M.strategy.parameters import StrategyConfig

from .config import ScreeningConfig
from .run import ScreeningData
from .walkforward_config import WalkForwardConfig


class KachoGammaDataError(RuntimeError):
    """Pinned Kacho prices and authoritative Gamma labels do not align."""


@dataclass(frozen=True)
class KachoGammaProvenance:
    dataset_manifest: str
    dataset_manifest_sha256: str
    dataset_config_sha256: str
    dataset_revision: str
    gamma_universe_manifest: str
    gamma_universe_manifest_sha256: str
    gamma_universe_sha256: str
    gamma_rows_loaded: int
    loaded_markets: int
    valid_markets: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_gamma_rows(
    universe_path: Path,
    *,
    assets: tuple[str, ...],
    period_start_s: int,
    end_exclusive_s: int,
) -> dict[str, dict[str, object]]:
    rows = pq.read_table(
        universe_path,
        columns=["condition_id", "asset", "duration", "outcome_up", "fee_rate"],
        filters=[
            ("market_start_ms", ">=", period_start_s * 1_000),
            ("market_start_ms", "<", end_exclusive_s * 1_000),
            ("duration", "=", "5m"),
        ],
    ).to_pylist()
    allowed = set(assets)
    selected = [row for row in rows if str(row["asset"]) in allowed]
    result = {str(row["condition_id"]): row for row in selected}
    if len(result) != len(selected):
        raise KachoGammaDataError("duplicate Gamma condition IDs in selected period")
    return result


def load_kacho_gamma_data(
    config: WalkForwardConfig,
    market_config: ScreeningConfig,
    *,
    end_exclusive_s: int,
) -> tuple[ScreeningData, KachoGammaProvenance]:
    period_start_s = int(market_config.period_start.timestamp())
    period_end_s = int(market_config.period_end_exclusive.timestamp())
    if not period_start_s < end_exclusive_s <= period_end_s:
        raise KachoGammaDataError("requested data boundary is outside frozen period")
    dataset_config = load_kacho_dataset_config(config.dataset_config)
    batch = load_kacho_decision_batch(
        dataset_config,
        config.dataset_root,
        assets=config.assets,
        max_markets=0,
        decision_seconds_before_end=market_config.decision_seconds_before_end,
        transition_horizon_seconds=market_config.transition_horizon_seconds,
        label_policy="external_authoritative_labels",
        execution_latency_seconds=math_ceil_milliseconds(
            market_config.execution_latency_ms
        ),
        period_start_s=period_start_s,
        period_end_exclusive_s=end_exclusive_s,
    )
    root = Path(market_config.data_root)
    universe_path = root / "universe.parquet"
    universe_manifest_path = root / "universe_manifest.json"
    if not universe_path.is_file() or not universe_manifest_path.is_file():
        raise KachoGammaDataError("build the Stage 4c Gamma universe first")
    universe_manifest = json.loads(
        universe_manifest_path.read_text(encoding="utf-8")
    )
    if universe_manifest.get("data_contract_sha256") != market_config.data_contract_sha256:
        raise KachoGammaDataError("Gamma universe data contract differs")
    if universe_manifest.get("universe_sha256") != _sha256(universe_path):
        raise KachoGammaDataError("Gamma universe hash mismatch")
    gamma = _load_gamma_rows(
        universe_path,
        assets=config.assets,
        period_start_s=period_start_s,
        end_exclusive_s=end_exclusive_s,
    )
    missing = [value for value in batch.condition_ids if value not in gamma]
    if missing:
        raise KachoGammaDataError(
            f"{len(missing)} Kacho conditions are absent from Gamma universe"
        )
    if len(gamma) != len(batch):
        raise KachoGammaDataError(
            "Kacho/Gamma selected-period market counts differ: "
            f"{len(batch)} != {len(gamma)}"
        )
    for condition_id, asset in zip(batch.condition_ids, batch.assets, strict=True):
        if str(gamma[condition_id]["asset"]) != asset:
            raise KachoGammaDataError(f"asset mismatch for {condition_id}")
    outcomes = torch.tensor(
        [float(gamma[value]["outcome_up"]) for value in batch.condition_ids],
        dtype=torch.float64,
    )
    fees = torch.tensor(
        [float(gamma[value]["fee_rate"]) for value in batch.condition_ids],
        dtype=torch.float64,
    )
    if not torch.isfinite(outcomes).all() or not torch.isfinite(fees).all():
        raise KachoGammaDataError("Gamma outcome or fee is non-finite")
    batch = replace(
        batch,
        outcome_up=outcomes,
        label_source="gamma_authoritative_on_kacho_prices",
    )
    valid = int(batch.snapshot_valid.sum().item())
    if valid / len(batch) < 0.99:
        raise KachoGammaDataError("Kacho exact-second coverage is below 99%")
    dataset_dir = dataset_config.dataset_dir(config.dataset_root)
    dataset_manifest_path = dataset_dir / "manifest.json"
    if not dataset_manifest_path.is_file():
        raise KachoGammaDataError("Kacho dataset manifest is missing")
    provenance = KachoGammaProvenance(
        dataset_manifest=str(dataset_manifest_path),
        dataset_manifest_sha256=_sha256(dataset_manifest_path),
        dataset_config_sha256=dataset_config.config_sha256,
        dataset_revision=dataset_config.revision,
        gamma_universe_manifest=str(universe_manifest_path),
        gamma_universe_manifest_sha256=_sha256(universe_manifest_path),
        gamma_universe_sha256=_sha256(universe_path),
        gamma_rows_loaded=len(gamma),
        loaded_markets=len(batch),
        valid_markets=valid,
    )
    return ScreeningData(batch=batch, fee_rate=fees), provenance


def math_ceil_milliseconds(value: int) -> int:
    if value < 0:
        raise KachoGammaDataError("execution latency must be nonnegative")
    return (value + 999) // 1_000


def select_strategy_data(
    data: ScreeningData, strategy: StrategyConfig
) -> ScreeningData:
    if strategy.duration != "5m":
        raise KachoGammaDataError("Stage 4c supports 5m strategies only")
    allowed = set(strategy.assets)
    indices = torch.tensor(
        [index for index, asset in enumerate(data.batch.assets) if asset in allowed],
        dtype=torch.int64,
    )
    if indices.numel() == 0:
        raise KachoGammaDataError(f"empty universe for {strategy.strategy_id}")
    return data.index(indices)
