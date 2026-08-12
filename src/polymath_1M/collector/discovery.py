from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from polymath_1M.audit.http import JsonResponse, JsonTransport

from .config import CollectorConfig, SeriesSpec
from .models import DiscoveryResult, MarketRecord


class HttpArchive(Protocol):
    def archive_http(self, source: str, response: JsonResponse) -> None: ...


def _datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} is missing or not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} has no timezone")
    return parsed.astimezone(UTC)


def _array(value: Any, field: str) -> list[Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError(f"{field} is not an array")
    return value


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _reference(series: SeriesSpec, source: str) -> tuple[str, str]:
    lowered = source.lower()
    if "twap-30s" in lowered:
        return "crypto_prices_twap_thirty", f"{series.asset.lower()}/usd"
    if "twap-60s" in lowered:
        return "crypto_prices_twap_sixty", f"{series.asset.lower()}/usd"
    if "binance.com" in lowered:
        return "crypto_prices", f"{series.asset.lower()}usdt"
    return "unsupported", ""


class MarketDiscovery:
    def __init__(
        self,
        config: CollectorConfig,
        transport: JsonTransport,
        archive: HttpArchive,
    ) -> None:
        self.config = config
        self.transport = transport
        self.archive = archive
        self._clob_cache: dict[str, dict[str, Any]] = {}

    def discover(self, now: datetime | None = None) -> DiscoveryResult:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        markets: dict[str, MarketRecord] = {}
        errors: list[str] = []
        for series in self.config.series:
            try:
                response = self.transport.get_json(
                    self.config.gamma_url,
                    "/events",
                    {
                        "series_id": series.id,
                        "active": True,
                        "closed": False,
                        "limit": 20,
                        "end_date_min": (
                            observed_at
                            - timedelta(seconds=self.config.market_lookback_seconds)
                        ).isoformat(),
                        "end_date_max": (
                            observed_at
                            + timedelta(
                                seconds=(
                                    self.config.market_lookahead_seconds
                                    + series.duration_seconds
                                )
                            )
                        ).isoformat(),
                    },
                )
                self.archive.archive_http(f"gamma_series_{series.id}", response)
                if not isinstance(response.data, list):
                    raise ValueError("Gamma events response is not an array")
                for event in response.data:
                    try:
                        record = self._parse_event(series, event, observed_at)
                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ) as exc:
                        errors.append(f"{series.slug}: {exc}")
                        continue
                    if record is not None:
                        markets[record.condition_id] = record
            except Exception as exc:
                errors.append(f"{series.slug}: discovery request failed: {exc}")
        return DiscoveryResult(
            markets=tuple(sorted(markets.values(), key=lambda item: item.condition_id)),
            errors=tuple(errors),
        )

    def _parse_event(
        self, series: SeriesSpec, event: dict[str, Any], observed_at: datetime
    ) -> MarketRecord | None:
        end = _datetime(event.get("endDate"), "event.endDate")
        start_raw = event.get("startTime")
        start = (
            _datetime(start_raw, "event.startTime")
            if start_raw
            else end - timedelta(seconds=series.duration_seconds)
        )
        if end <= start:
            raise ValueError(f"invalid event interval for {event.get('slug')}")
        if end < observed_at - timedelta(seconds=self.config.market_lookback_seconds):
            return None
        if start > observed_at + timedelta(
            seconds=self.config.market_lookahead_seconds
        ):
            return None

        event_markets = event.get("markets")
        if not isinstance(event_markets, list) or len(event_markets) != 1:
            raise ValueError(
                f"expected exactly one market for event {event.get('slug')}, got {event_markets!r}"
            )
        market = event_markets[0]
        if market.get("acceptingOrders") is False:
            return None
        if market.get("enableOrderBook") is False:
            return None
        outcomes_raw = tuple(
            str(value) for value in _array(market["outcomes"], "outcomes")
        )
        token_ids_raw = tuple(
            str(value) for value in _array(market["clobTokenIds"], "clobTokenIds")
        )
        if outcomes_raw != ("Up", "Down") or len(token_ids_raw) != 2:
            raise ValueError(
                f"unexpected outcomes/tokens for market {market.get('slug')}: "
                f"{outcomes_raw}/{len(token_ids_raw)}"
            )
        token_ids = (token_ids_raw[0], token_ids_raw[1])
        condition_id = str(market["conditionId"])
        clob = self._clob_info(condition_id)
        clob_tokens = {
            str(item["t"]): str(item["o"])
            for item in clob.get("t", [])
            if isinstance(item, dict) and "t" in item and "o" in item
        }
        expected_mapping = dict(zip(token_ids, outcomes_raw, strict=True))
        if clob_tokens != expected_mapping:
            raise ValueError(
                f"Gamma/CLOB token mapping mismatch for {condition_id}: "
                f"{expected_mapping} != {clob_tokens}"
            )

        description = str(market.get("description") or event.get("description") or "")
        resolution_source = str(
            market.get("resolutionSource") or event.get("resolutionSource") or ""
        )
        reference_topic, reference_symbol = _reference(series, resolution_source)
        return MarketRecord(
            series_id=series.id,
            series_slug=series.slug,
            asset=series.asset,
            duration=series.duration,
            event_id=str(event["id"]),
            market_id=str(market["id"]),
            slug=str(market.get("slug") or event["slug"]),
            question=str(market.get("question") or event.get("title") or ""),
            condition_id=condition_id,
            token_ids=token_ids,
            outcomes=(outcomes_raw[0], outcomes_raw[1]),
            start_timestamp_ms=int(start.timestamp() * 1_000),
            end_timestamp_ms=int(end.timestamp() * 1_000),
            description=description,
            resolution_source=resolution_source,
            reference_topic=reference_topic,
            reference_symbol=reference_symbol,
            gamma_min_tick_size=_optional_string(market.get("orderPriceMinTickSize")),
            gamma_min_order_size=_optional_string(market.get("orderMinSize")),
            gamma_fees_enabled=(
                bool(market["feesEnabled"])
                if market.get("feesEnabled") is not None
                else None
            ),
            gamma_fee_schedule=(
                dict(market["feeSchedule"])
                if isinstance(market.get("feeSchedule"), dict)
                else None
            ),
            clob_min_tick_size=str(clob["mts"]),
            clob_min_order_size=str(clob["mos"]),
            clob_maker_base_fee_bps=int(clob.get("mbf", 0)),
            clob_taker_base_fee_bps=int(clob.get("tbf", 0)),
            clob_fee_schedule=dict(clob.get("fd") or {}),
            clob_order_delay_enabled=(
                bool(clob["itode"]) if clob.get("itode") is not None else None
            ),
        )

    def _clob_info(self, condition_id: str) -> dict[str, Any]:
        cached = self._clob_cache.get(condition_id)
        if cached is not None:
            return cached
        response = self.transport.get_json(
            self.config.clob_url, f"/clob-markets/{condition_id}", {}
        )
        self.archive.archive_http("clob_market_info", response)
        if not isinstance(response.data, dict):
            raise ValueError(
                f"CLOB market response for {condition_id} is not an object"
            )
        self._clob_cache[condition_id] = response.data
        return response.data

    def fetch_resolution(self, market: MarketRecord) -> dict[str, Any] | None:
        response = self.transport.get_json(
            self.config.gamma_url, "/markets", {"slug": market.slug}
        )
        self.archive.archive_http("gamma_resolution", response)
        if not isinstance(response.data, list) or len(response.data) != 1:
            raise ValueError(
                f"expected one Gamma resolution market for {market.slug}, "
                f"got {type(response.data).__name__}/{len(response.data) if isinstance(response.data, list) else 'n/a'}"
            )
        payload = response.data[0]
        if not isinstance(payload, dict):
            raise ValueError(f"Gamma resolution for {market.slug} is not an object")
        if not payload.get("closed"):
            return None
        outcomes = [str(value) for value in _array(payload["outcomes"], "outcomes")]
        prices = [
            str(value) for value in _array(payload["outcomePrices"], "outcomePrices")
        ]
        if outcomes != list(market.outcomes) or len(prices) != 2:
            raise ValueError(f"resolution outcome mapping mismatch for {market.slug}")
        winning_indices = [
            index for index, value in enumerate(prices) if Decimal(value) == 1
        ]
        losing_indices = [
            index for index, value in enumerate(prices) if Decimal(value) == 0
        ]
        if len(winning_indices) != 1 or len(losing_indices) != 1:
            return None
        winner = winning_indices[0]
        return {
            "source": "gamma_closed_market",
            "condition_id": market.condition_id,
            "market_slug": market.slug,
            "closed": True,
            "winning_outcome": outcomes[winner],
            "winning_token_id": market.token_ids[winner],
            "outcomes": outcomes,
            "outcome_prices": prices,
            "uma_resolution_status": payload.get("umaResolutionStatus"),
            "closed_time": payload.get("closedTime"),
            "retrieved_at": response.retrieved_at,
        }
