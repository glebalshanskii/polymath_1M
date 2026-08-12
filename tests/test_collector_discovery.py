from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from typing import Any

from polymath_1M.audit.http import JsonResponse
from polymath_1M.collector.config import load_collector_config
from polymath_1M.collector.discovery import MarketDiscovery


class FakeArchive:
    def __init__(self) -> None:
        self.sources: list[str] = []

    def archive_http(self, source: str, response: JsonResponse) -> None:
        self.sources.append(source)


class FakeTransport:
    def __init__(self, *, hourly: bool = False) -> None:
        self.hourly = hourly

    def get_json(
        self, base_url: str, path: str, params: dict[str, object]
    ) -> JsonResponse:
        if path == "/events":
            target = "10114" if self.hourly else "10684"
            payload: Any = self._events() if params["series_id"] == target else []
        elif path == "/markets":
            payload = [
                {
                    "closed": True,
                    "outcomes": json.dumps(["Up", "Down"]),
                    "outcomePrices": json.dumps(["1", "0"]),
                    "umaResolutionStatus": "resolved",
                    "closedTime": "2026-08-12T19:06:00Z",
                }
            ]
        else:
            payload = {
                "t": [
                    {"t": "up-token", "o": "Up"},
                    {"t": "down-token", "o": "Down"},
                ],
                "mos": 5,
                "mts": 0.001,
                "mbf": 1000,
                "tbf": 1000,
                "itode": True,
                "fd": {"r": 0.07, "e": 1, "to": True},
            }
        body = json.dumps(payload).encode()
        return JsonResponse(
            data=payload,
            body=body,
            status=200,
            url=f"{base_url}{path}",
            retrieved_at="2026-08-12T19:00:00+00:00",
        )

    def _events(self) -> list[dict[str, Any]]:
        hourly_fields = (
            {
                "slug": "bitcoin-up-or-down-august-12-2026-3pm-et",
                "startTime": None,
                "endDate": "2026-08-12T20:00:00Z",
                "resolutionSource": "https://www.binance.com/en/trade/BTC_USDT",
            }
            if self.hourly
            else {
                "slug": "btc-updown-5m-1786561200",
                "startTime": "2026-08-12T19:00:00Z",
                "endDate": "2026-08-12T19:05:00Z",
                "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-30s-streams",
            }
        )
        event = {
            "id": "event-1",
            "description": "exact rules",
            **hourly_fields,
        }
        event["markets"] = [
            {
                "id": "market-1",
                "slug": event["slug"],
                "question": "Bitcoin Up or Down",
                "conditionId": "condition-1",
                "clobTokenIds": json.dumps(["up-token", "down-token"]),
                "outcomes": json.dumps(["Up", "Down"]),
                "acceptingOrders": True,
                "enableOrderBook": True,
                "orderPriceMinTickSize": 0.01,
                "orderMinSize": 5,
                "feesEnabled": True,
                "feeSchedule": {"rate": 0.07, "exponent": 1},
                "description": "exact rules",
                "resolutionSource": event["resolutionSource"],
            }
        ]
        return [event]


class CollectorDiscoveryTest(unittest.TestCase):
    def test_discovers_chainlink_twap_market_and_clob_contract(self) -> None:
        config = load_collector_config("cfg/collectors/stage2_polymarket.json")
        archive = FakeArchive()
        discovery = MarketDiscovery(config, FakeTransport(), archive)

        result = discovery.discover(datetime(2026, 8, 12, 19, 2, tzinfo=UTC))

        self.assertEqual(result.errors, ())
        self.assertEqual(len(result.markets), 1)
        market = result.markets[0]
        self.assertEqual(market.reference_topic, "crypto_prices_twap_thirty")
        self.assertEqual(market.reference_symbol, "btc/usd")
        self.assertEqual(market.clob_min_tick_size, "0.001")
        self.assertEqual(market.clob_fee_schedule["r"], 0.07)
        self.assertIn("gamma_series_10684", archive.sources)
        self.assertIn("clob_market_info", archive.sources)

        resolution = discovery.fetch_resolution(market)
        self.assertEqual(resolution["winning_outcome"], "Up")
        self.assertEqual(resolution["winning_token_id"], "up-token")
        self.assertIn("gamma_resolution", archive.sources)

    def test_derives_hourly_start_from_end_when_gamma_start_time_is_null(self) -> None:
        config = load_collector_config("cfg/collectors/stage2_polymarket.json")
        discovery = MarketDiscovery(config, FakeTransport(hourly=True), FakeArchive())

        result = discovery.discover(datetime(2026, 8, 12, 19, 30, tzinfo=UTC))

        self.assertEqual(len(result.markets), 1)
        market = result.markets[0]
        self.assertEqual(market.reference_topic, "crypto_prices")
        self.assertEqual(market.reference_symbol, "btcusdt")
        self.assertEqual(market.end_timestamp_ms - market.start_timestamp_ms, 3_600_000)


if __name__ == "__main__":
    unittest.main()
