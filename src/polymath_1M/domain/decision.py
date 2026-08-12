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
