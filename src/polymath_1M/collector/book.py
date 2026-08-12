from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import torch

PRICE_SCALE = 10_000
PRICE_LEVELS = PRICE_SCALE + 1


class BookDataError(ValueError):
    """A market message cannot be applied to a valid binary-outcome book."""


def _field(value: dict[str, Any], snake: str, camel: str | None = None) -> Any:
    if snake in value:
        return value[snake]
    return value.get(camel) if camel else None


def normalize_market_message(raw: str) -> list[tuple[str, dict[str, Any]]]:
    decoded = json.loads(raw)
    items = decoded if isinstance(decoded, list) else [decoded]
    normalized: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            raise BookDataError("market message item is not an object")
        if item.get("topic") == "market" and isinstance(item.get("payload"), dict):
            event_type = str(item.get("type") or "unknown")
            normalized.append((event_type, dict(item["payload"])))
            continue
        payload = item
        event_type = item.get("event_type")
        if not event_type:
            if "bids" in item and "asks" in item:
                event_type = "book"
            elif "price_changes" in item or "priceChanges" in item:
                event_type = "price_change"
            elif "new_tick_size" in item or "newTickSize" in item:
                event_type = "tick_size_change"
            elif "winning_asset_id" in item or "winningTokenId" in item:
                event_type = "market_resolved"
            elif "best_bid" in item and "best_ask" in item and "spread" in item:
                event_type = "best_bid_ask"
            elif "price" in item and "side" in item:
                event_type = "last_trade_price"
            else:
                event_type = "unknown"
        normalized.append((str(event_type), payload))
    return normalized


def event_timestamp_ms(payload: dict[str, Any]) -> int | None:
    value = payload.get("timestamp")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _price_index(value: Any) -> int:
    try:
        scaled = Decimal(str(value)) * PRICE_SCALE
    except (InvalidOperation, ValueError) as exc:
        raise BookDataError(f"invalid price: {value!r}") from exc
    if scaled != scaled.to_integral_value():
        raise BookDataError(f"price has more than four decimals: {value!r}")
    index = int(scaled)
    if index < 0 or index > PRICE_SCALE:
        raise BookDataError(f"price outside [0, 1]: {value!r}")
    return index


def _size(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BookDataError(f"invalid size: {value!r}") from exc
    if not math.isfinite(result) or result < 0:
        raise BookDataError(f"size must be finite and non-negative: {value!r}")
    return result


@dataclass
class AssetBook:
    levels: torch.Tensor
    initialized: bool = False
    last_hash: str | None = None
    snapshots: int = 0
    changes: int = 0
    best_bid_index: int | None = None
    best_ask_index: int | None = None

    @classmethod
    def empty(cls) -> AssetBook:
        return cls(torch.zeros((2, PRICE_LEVELS), dtype=torch.float64, device="cpu"))

    def apply_snapshot(
        self,
        bids: Iterable[dict[str, Any]],
        asks: Iterable[dict[str, Any]],
        book_hash: Any,
    ) -> None:
        replacement = torch.zeros_like(self.levels)
        for side_index, side in enumerate((bids, asks)):
            rows = list(side)
            if rows:
                indices = torch.tensor(
                    [_price_index(row["price"]) for row in rows], dtype=torch.int64
                )
                sizes = torch.tensor(
                    [_size(row["size"]) for row in rows], dtype=torch.float64
                )
                replacement[side_index].index_copy_(0, indices, sizes)
        self.levels.copy_(replacement)
        self.initialized = True
        self.last_hash = str(book_hash) if book_hash is not None else None
        self.snapshots += 1
        self._refresh_best(0)
        self._refresh_best(1)

    def apply_changes(
        self, side: str, prices: list[Any], sizes: list[Any], book_hash: Any
    ) -> None:
        if not self.initialized:
            raise BookDataError("price_change received before initial book snapshot")
        side_index = {"BUY": 0, "SELL": 1}.get(side.upper())
        if side_index is None:
            raise BookDataError(f"unknown order side: {side!r}")
        collapsed = {
            _price_index(price): _size(size)
            for price, size in zip(prices, sizes, strict=True)
        }
        ordered = sorted(collapsed.items())
        indices = torch.tensor([item[0] for item in ordered], dtype=torch.int64)
        values = torch.tensor([item[1] for item in ordered], dtype=torch.float64)
        self.levels[side_index].index_copy_(0, indices, values)
        current_best = self.best_bid_index if side_index == 0 else self.best_ask_index
        positive_indices = indices[values > 0]
        if positive_indices.numel():
            candidate = int(
                (
                    torch.max(positive_indices)
                    if side_index == 0
                    else torch.min(positive_indices)
                ).item()
            )
            if (
                current_best is None
                or (side_index == 0 and candidate > current_best)
                or (side_index == 1 and candidate < current_best)
            ):
                current_best = candidate
        removed_current = current_best is not None and bool(
            torch.any((indices == current_best) & (values == 0)).item()
        )
        if removed_current:
            self._refresh_best(side_index)
        elif side_index == 0:
            self.best_bid_index = current_best
        else:
            self.best_ask_index = current_best
        self.last_hash = str(book_hash) if book_hash is not None else self.last_hash
        self.changes += len(ordered)

    def best_prices(self) -> tuple[int | None, int | None]:
        return self.best_bid_index, self.best_ask_index

    def align_best(self, best_bid: Any, best_ask: Any) -> None:
        if best_bid is not None:
            bid_index = _price_index(best_bid)
            if bid_index < PRICE_SCALE:
                self.levels[0, bid_index + 1 :] = 0
            self.best_bid_index = bid_index if self.levels[0, bid_index] > 0 else None
        if best_ask is not None:
            ask_index = _price_index(best_ask)
            if ask_index > 0:
                self.levels[1, :ask_index] = 0
            self.best_ask_index = ask_index if self.levels[1, ask_index] > 0 else None

    def _refresh_best(self, side_index: int) -> None:
        indices = torch.nonzero(self.levels[side_index] > 0, as_tuple=False).flatten()
        value = None
        if indices.numel():
            value = int(
                (torch.max(indices) if side_index == 0 else torch.min(indices)).item()
            )
        if side_index == 0:
            self.best_bid_index = value
        else:
            self.best_ask_index = value

    def digest(self) -> str:
        digest = hashlib.sha256()
        for side_index, side_name in ((0, "B"), (1, "A")):
            indices = torch.nonzero(
                self.levels[side_index] > 0, as_tuple=False
            ).flatten()
            sizes = self.levels[side_index].index_select(0, indices)
            for index, size in zip(indices.tolist(), sizes.tolist(), strict=True):
                digest.update(f"{side_name}:{index}:{format(size, '.17g')}\n".encode())
        return digest.hexdigest()


class OrderBookStore:
    def __init__(self) -> None:
        self.books: dict[str, AssetBook] = {}
        self.unknown_events = 0
        self.crossed_states = 0
        self.resolutions: dict[str, dict[str, Any]] = {}

    def apply_raw(self, raw: str) -> list[tuple[str, int | None]]:
        observed: list[tuple[str, int | None]] = []
        for event_type, payload in normalize_market_message(raw):
            observed.append((event_type, event_timestamp_ms(payload)))
            if event_type == "book":
                self._snapshot(payload)
            elif event_type == "price_change":
                self._changes(payload)
            elif event_type == "market_resolved":
                condition_id = str(
                    payload.get("market") or payload.get("condition_id") or ""
                )
                if condition_id:
                    self.resolutions[condition_id] = payload
            elif event_type not in {
                "last_trade_price",
                "tick_size_change",
                "best_bid_ask",
                "new_market",
            }:
                self.unknown_events += 1
        return observed

    def _snapshot(self, payload: dict[str, Any]) -> None:
        asset_id = str(_field(payload, "asset_id", "tokenId") or "")
        if not asset_id:
            raise BookDataError("book snapshot has no asset/token id")
        bids = payload.get("bids")
        asks = payload.get("asks")
        if not isinstance(bids, list) or not isinstance(asks, list):
            raise BookDataError("book snapshot bids/asks are not arrays")
        book = self.books.setdefault(asset_id, AssetBook.empty())
        book.apply_snapshot(bids, asks, payload.get("hash"))
        self._check_crossed(book)

    def _changes(self, payload: dict[str, Any]) -> None:
        changes = _field(payload, "price_changes", "priceChanges")
        if not isinstance(changes, list):
            raise BookDataError("price_change payload has no changes array")
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for change in changes:
            if not isinstance(change, dict):
                raise BookDataError("price change item is not an object")
            asset_id = str(_field(change, "asset_id", "tokenId") or "")
            side = str(change.get("side") or "")
            if not asset_id:
                raise BookDataError("price change has no asset/token id")
            grouped.setdefault((asset_id, side), []).append(change)
        affected: set[str] = set()
        for (asset_id, side), rows in grouped.items():
            book = self.books.setdefault(asset_id, AssetBook.empty())
            book.apply_changes(
                side,
                [row["price"] for row in rows],
                [row["size"] for row in rows],
                rows[-1].get("hash"),
            )
            affected.add(asset_id)
        for asset_id in affected:
            asset_rows = [
                row
                for (changed_asset, _), changed_rows in grouped.items()
                if changed_asset == asset_id
                for row in changed_rows
            ]
            best_bid = next(
                (
                    _field(row, "best_bid", "bestBid")
                    for row in reversed(asset_rows)
                    if _field(row, "best_bid", "bestBid") is not None
                ),
                None,
            )
            best_ask = next(
                (
                    _field(row, "best_ask", "bestAsk")
                    for row in reversed(asset_rows)
                    if _field(row, "best_ask", "bestAsk") is not None
                ),
                None,
            )
            book = self.books[asset_id]
            book.align_best(best_bid, best_ask)
            self._check_crossed(book)

    def _check_crossed(self, book: AssetBook) -> None:
        best_bid, best_ask = book.best_prices()
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            self.crossed_states += 1

    def summary(self) -> dict[str, Any]:
        assets = {}
        global_digest = hashlib.sha256()
        for asset_id, book in sorted(self.books.items()):
            best_bid, best_ask = book.best_prices()
            digest = book.digest()
            global_digest.update(f"{asset_id}:{digest}\n".encode())
            assets[asset_id] = {
                "initialized": book.initialized,
                "snapshots": book.snapshots,
                "changes": book.changes,
                "last_hash": book.last_hash,
                "best_bid": best_bid / PRICE_SCALE if best_bid is not None else None,
                "best_ask": best_ask / PRICE_SCALE if best_ask is not None else None,
                "digest": digest,
            }
        return {
            "asset_count": len(assets),
            "initialized_asset_count": sum(
                int(item["initialized"]) for item in assets.values()
            ),
            "unknown_events": self.unknown_events,
            "crossed_states": self.crossed_states,
            "global_digest": global_digest.hexdigest(),
            "assets": assets,
        }
