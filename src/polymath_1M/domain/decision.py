from __future__ import annotations

from dataclasses import dataclass, fields

import torch


@dataclass(frozen=True)
class DecisionBatch:
    """Causal market snapshot shared by historical and live adapters.

    Every tensor has a market dimension first. Book tensors use outcome side
    order ``(Up, Down)`` and ask depth is ordered from best to worst price.
    """

    condition_ids: tuple[str, ...]
    assets: tuple[str, ...]
    market_start_s: torch.Tensor
    market_end_s: torch.Tensor
    decision_s: torch.Tensor
    previous_s: torch.Tensor
    outcome_up: torch.Tensor
    n_ticks: torch.Tensor
    previous_mid_up: torch.Tensor
    current_mid_up: torch.Tensor
    current_mid: torch.Tensor
    bids: torch.Tensor
    asks: torch.Tensor
    ask_depth_prices: torch.Tensor
    ask_depth_sizes: torch.Tensor
    snapshot_valid: torch.Tensor
    label_source: str

    def __post_init__(self) -> None:
        count = len(self.condition_ids)
        if count == 0 or len(self.assets) != count:
            raise ValueError("DecisionBatch IDs/assets must have equal nonzero length")
        vector_fields = (
            "market_start_s",
            "market_end_s",
            "decision_s",
            "previous_s",
            "outcome_up",
            "n_ticks",
            "previous_mid_up",
            "current_mid_up",
            "snapshot_valid",
        )
        for name in vector_fields:
            value = getattr(self, name)
            if not isinstance(value, torch.Tensor) or value.shape != (count,):
                raise ValueError(f"DecisionBatch {name} must have shape [{count}]")
        for name in ("current_mid", "bids", "asks"):
            if getattr(self, name).shape != (count, 2):
                raise ValueError(f"DecisionBatch {name} must have shape [{count}, 2]")
        if (
            self.ask_depth_prices.ndim != 3
            or self.ask_depth_prices.shape[:2] != (count, 2)
            or self.ask_depth_sizes.shape != self.ask_depth_prices.shape
        ):
            raise ValueError("DecisionBatch ask depth must share shape [N, 2, L]")
        if self.snapshot_valid.dtype != torch.bool:
            raise TypeError("DecisionBatch snapshot_valid must be boolean")
        for name in (
            "market_start_s",
            "market_end_s",
            "decision_s",
            "previous_s",
            "n_ticks",
        ):
            if getattr(self, name).dtype != torch.int64:
                raise TypeError(f"DecisionBatch {name} must be int64")
        floating = (
            "outcome_up",
            "previous_mid_up",
            "current_mid_up",
            "current_mid",
            "bids",
            "asks",
            "ask_depth_prices",
            "ask_depth_sizes",
        )
        if any(not getattr(self, name).is_floating_point() for name in floating):
            raise TypeError("DecisionBatch market values must be floating tensors")

    def __len__(self) -> int:
        return len(self.condition_ids)

    def index(self, indices: torch.Tensor) -> DecisionBatch:
        if indices.dtype != torch.int64 or indices.ndim != 1:
            raise TypeError(
                "DecisionBatch indices must be a one-dimensional int64 tensor"
            )
        cpu_indices = indices.detach().cpu().tolist()
        values: dict[str, object] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, torch.Tensor):
                values[item.name] = value.index_select(0, indices.to(value.device))
            elif item.name in {"condition_ids", "assets"}:
                values[item.name] = tuple(value[index] for index in cpu_indices)
            else:
                values[item.name] = value
        return DecisionBatch(**values)

    def to(self, device: torch.device, dtype: torch.dtype) -> DecisionBatch:
        values: dict[str, object] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, torch.Tensor):
                if value.is_floating_point():
                    values[item.name] = value.to(device=device, dtype=dtype)
                else:
                    values[item.name] = value.to(device=device)
            else:
                values[item.name] = value
        return DecisionBatch(**values)
