from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .models import MarketRecord, ReferenceBoundary


def _exact_value(
    topic: str, payload: dict[str, Any], *, snapshot: bool
) -> tuple[str, bool]:
    full = payload.get("full_accuracy_value")
    if full is not None:
        if topic in {"crypto_prices_twap_thirty", "crypto_prices_twap_sixty"}:
            decimal_value = Decimal(str(full)) / Decimal(10**18)
            return format(decimal_value, "f"), True
        return str(full), True
    value = payload.get("value")
    if value is None:
        raise ValueError("reference price payload has no value")
    return str(value), False


class ReferenceTracker:
    def __init__(self) -> None:
        self._markets: dict[str, MarketRecord] = {}
        self._index: dict[tuple[str, str, int], set[str]] = {}
        self._matches: dict[str, ReferenceBoundary] = {}

    def register(self, market: MarketRecord) -> None:
        if market.condition_id in self._markets:
            return
        self._markets[market.condition_id] = market
        if market.reference_topic != "unsupported":
            key = (
                market.reference_topic,
                market.reference_symbol,
                market.start_timestamp_ms,
            )
            self._index.setdefault(key, set()).add(market.condition_id)

    def apply_raw(self, raw: str) -> list[tuple[str, int | None]]:
        if not raw.strip():
            return [("empty", None)]
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("RTDS message is not an object")
        topic = str(decoded.get("topic") or "unknown")
        message_type = str(decoded.get("type") or "unknown")
        payload = decoded.get("payload")
        if not isinstance(payload, dict):
            return [(f"{topic}:{message_type}", None)]
        outer_timestamp = decoded.get("timestamp")
        outer_ms = int(outer_timestamp) if outer_timestamp is not None else None
        if message_type == "subscribe" and isinstance(payload.get("data"), list):
            symbol = str(payload.get("symbol") or "").lower()
            for row in payload["data"]:
                if isinstance(row, dict):
                    self._apply_point(topic, symbol, row, snapshot=True)
        elif message_type == "update":
            symbol = str(payload.get("symbol") or "").lower()
            self._apply_point(topic, symbol, payload, snapshot=False)
        return [(f"{topic}:{message_type}", outer_ms)]

    def _apply_point(
        self, topic: str, symbol: str, payload: dict[str, Any], *, snapshot: bool
    ) -> None:
        timestamp = payload.get("timestamp")
        if timestamp is None:
            return
        observed_ms = int(timestamp)
        condition_ids = self._index.get((topic, symbol, observed_ms), ())
        for condition_id in condition_ids:
            value, exact_source_value = _exact_value(topic, payload, snapshot=snapshot)
            candidate = ReferenceBoundary(
                condition_id=condition_id,
                market_slug=self._markets[condition_id].slug,
                reference_topic=topic,
                reference_symbol=symbol,
                boundary_timestamp_ms=observed_ms,
                observed_timestamp_ms=observed_ms,
                value=value,
                exact=exact_source_value,
            )
            previous = self._matches.get(condition_id)
            if previous is None or (candidate.exact and not previous.exact):
                self._matches[condition_id] = candidate

    def boundaries(self) -> list[ReferenceBoundary]:
        result = []
        for condition_id, market in sorted(self._markets.items()):
            match = self._matches.get(condition_id)
            if match is not None:
                result.append(match)
            else:
                result.append(
                    ReferenceBoundary(
                        condition_id=condition_id,
                        market_slug=market.slug,
                        reference_topic=market.reference_topic,
                        reference_symbol=market.reference_symbol,
                        boundary_timestamp_ms=market.start_timestamp_ms,
                        observed_timestamp_ms=None,
                        value=None,
                        exact=False,
                    )
                )
        return result
