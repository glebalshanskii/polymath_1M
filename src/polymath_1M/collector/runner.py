from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from polymath_1M.audit.http import UrllibJsonTransport

from .book import BookDataError, OrderBookStore
from .config import CollectorConfig, load_collector_config
from .discovery import MarketDiscovery
from .health import CollectorHealth
from .models import MarketRecord
from .reference import ReferenceTracker
from .storage import CollectorStorage, RawStreamWriter, wall_and_monotonic_ns


@dataclass(frozen=True)
class SubscriptionUpdate:
    added: tuple[str, ...]
    removed: tuple[str, ...]


class SubscriptionRegistry:
    def __init__(self) -> None:
        self._desired: set[str] = set()
        self._updates: asyncio.Queue[SubscriptionUpdate] = asyncio.Queue()
        self._changed = asyncio.Event()

    def replace(self, token_ids: set[str]) -> SubscriptionUpdate:
        added = tuple(sorted(token_ids - self._desired))
        removed = tuple(sorted(self._desired - token_ids))
        self._desired = set(token_ids)
        update = SubscriptionUpdate(added, removed)
        if added or removed:
            self._updates.put_nowait(update)
            self._changed.set()
        return update

    def snapshot_and_drain(self) -> tuple[str, ...]:
        while not self._updates.empty():
            self._updates.get_nowait()
        return tuple(sorted(self._desired))

    async def next_update(self) -> SubscriptionUpdate:
        return await self._updates.get()

    async def wait_for_tokens(self, stop: asyncio.Event) -> tuple[str, ...]:
        while not self._desired and not stop.is_set():
            changed_task = asyncio.create_task(self._changed.wait())
            stop_task = asyncio.create_task(stop.wait())
            done, pending = await asyncio.wait(
                {changed_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if changed_task in done:
                self._changed.clear()
        return tuple(sorted(self._desired))


class CollectorRuntime:
    def __init__(self, config: CollectorConfig, storage: CollectorStorage) -> None:
        self.config = config
        self.storage = storage
        self.health = CollectorHealth()
        self.books = OrderBookStore()
        self.references = ReferenceTracker()
        self.registries = {
            asset: SubscriptionRegistry() for asset in ("BTC", "ETH", "SOL", "XRP")
        }
        self.markets: dict[str, MarketRecord] = {}
        self.gamma_resolutions: dict[str, dict[str, Any]] = {}
        self.clob_queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self.clob_producers_done = asyncio.Event()
        self.errors: list[str] = []
        self.stop = asyncio.Event()
        self.transport = UrllibJsonTransport(
            timeout_seconds=config.http_timeout_seconds,
            retries=config.http_retries,
        )
        self.discovery = MarketDiscovery(config, self.transport, storage)

    async def run(self) -> Path:
        status = "completed"
        tasks: list[asyncio.Task[Any]] = []
        try:
            await self._discover_once()
            if not self.markets:
                raise RuntimeError("discovery found no eligible Up/Down markets")
            tasks = [
                asyncio.create_task(self._discovery_loop(), name="market-discovery"),
                asyncio.create_task(
                    self._clob_processor_loop(), name="clob-book-processor"
                ),
                asyncio.create_task(self._clob_supervisor(), name="clob-supervisor"),
                asyncio.create_task(self._rtds_loop(), name="rtds-reference"),
                asyncio.create_task(self._timer(), name="collector-timer"),
            ]
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            status = "interrupted"
            self.stop.set()
            raise
        except Exception as exc:
            status = "failed"
            self.errors.append(f"fatal: {type(exc).__name__}: {exc}")
            self.stop.set()
        finally:
            self.stop.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            now_ns = time.time_ns()
            self.health.book_crossed_states = self.books.crossed_states
            manifest = self.storage.finalize(
                status=status,
                health=self.health.to_dict(self.config.stale_after_seconds, now_ns),
                book_summary=self.books.summary(),
                markets=[
                    market.to_dict()
                    for market in sorted(
                        self.markets.values(), key=lambda item: item.condition_id
                    )
                ],
                reference_boundaries=[
                    item.to_dict() for item in self.references.boundaries()
                ],
                resolutions={
                    "gamma": self.gamma_resolutions,
                    "websocket": self.books.resolutions,
                },
                errors=self.errors,
            )
        return manifest.parent

    async def _timer(self) -> None:
        try:
            await asyncio.wait_for(
                self.stop.wait(), timeout=self.config.duration_seconds
            )
        except TimeoutError:
            self.stop.set()

    async def _discover_once(self) -> None:
        result = await asyncio.to_thread(self.discovery.discover)
        self.health.discovery_runs += 1
        for error in result.errors:
            if error not in self.health.discovery_errors:
                self.health.discovery_errors.append(error)
        for market in result.markets:
            self.markets[market.condition_id] = market
            self.references.register(market)
        await asyncio.to_thread(self._reconcile_resolutions)
        self._refresh_subscriptions()

    def _reconcile_resolutions(self) -> None:
        cutoff_ms = (
            time.time_ns() // 1_000_000 - self.config.resolution_delay_seconds * 1_000
        )
        for market in sorted(
            self.markets.values(), key=lambda item: item.end_timestamp_ms
        ):
            if market.end_timestamp_ms > cutoff_ms:
                continue
            if market.condition_id in self.gamma_resolutions:
                continue
            try:
                resolution = self.discovery.fetch_resolution(market)
            except Exception as exc:
                message = f"resolution {market.slug}: {type(exc).__name__}: {exc}"
                if message not in self.health.discovery_errors:
                    self.health.discovery_errors.append(message)
                continue
            if resolution is not None:
                self.gamma_resolutions[market.condition_id] = resolution

    async def _discovery_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(
                    self.stop.wait(),
                    timeout=self.config.discovery_interval_seconds,
                )
                break
            except TimeoutError:
                await self._discover_once()

    def _refresh_subscriptions(self) -> None:
        now_ms = time.time_ns() // 1_000_000
        lower = now_ms - self.config.market_lookback_seconds * 1_000
        upper = now_ms + self.config.market_lookahead_seconds * 1_000
        for asset, registry in self.registries.items():
            desired = {
                token_id
                for market in self.markets.values()
                if market.asset == asset
                and market.end_timestamp_ms >= lower
                and market.start_timestamp_ms <= upper
                for token_id in market.token_ids
            }
            registry.replace(desired)

    async def _clob_loop(self, shard: str, registry: SubscriptionRegistry) -> None:
        connection_number = 0
        attempt = 0
        while not self.stop.is_set():
            token_ids = await registry.wait_for_tokens(self.stop)
            if not token_ids:
                return
            connection_number += 1
            connection_id = f"clob-{shard.lower()}-{connection_number:04d}"
            try:
                async with connect(
                    self.config.clob_ws_url,
                    open_timeout=self.config.http_timeout_seconds,
                    ping_interval=None,
                    max_size=None,
                    max_queue=4096,
                ) as websocket:
                    self.health.clob.connected(shard)
                    attempt = 0
                    token_ids = registry.snapshot_and_drain()
                    await self._send_clob(
                        websocket,
                        {
                            "assets_ids": list(token_ids),
                            "type": "market",
                            "custom_feature_enabled": False,
                        },
                        connection_id,
                    )
                    await self._clob_session(websocket, connection_id, registry, shard)
            except Exception as exc:
                if self.stop.is_set():
                    return
                message = f"{connection_id}: {type(exc).__name__}: {exc}"
                self.health.clob.error(message)
                delay = min(2**attempt, self.config.reconnect_max_seconds)
                attempt += 1
                await self._wait_or_stop(delay)

    async def _clob_supervisor(self) -> None:
        tasks = [
            asyncio.create_task(
                self._clob_loop(asset, self.registries[asset]),
                name=f"clob-market-{asset.lower()}",
            )
            for asset in self.registries
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.clob_producers_done.set()

    async def _clob_session(
        self,
        websocket: Any,
        connection_id: str,
        registry: SubscriptionRegistry,
        shard: str,
    ) -> None:
        receiver = asyncio.create_task(
            self._clob_receive_loop(websocket, connection_id, shard)
        )
        updater = asyncio.create_task(
            self._clob_update_loop(websocket, connection_id, registry)
        )
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(
                websocket,
                self.storage.clob_writer,
                connection_id,
                self.config.clob_heartbeat_seconds,
            )
        )
        stopper = asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait(
                {receiver, updater, heartbeat, stopper},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stopper not in done:
                for task in done:
                    task.result()
                raise ConnectionError("CLOB session task ended before collector stop")
        finally:
            for task in (receiver, updater, heartbeat, stopper):
                task.cancel()
            await asyncio.gather(
                receiver, updater, heartbeat, stopper, return_exceptions=True
            )

    async def _clob_receive_loop(
        self, websocket: Any, connection_id: str, shard: str
    ) -> None:
        received = 0
        async for raw in websocket:
            await self._handle_clob(raw, connection_id, shard)
            received += 1
            if received % 64 == 0:
                await asyncio.sleep(0)

    async def _clob_update_loop(
        self,
        websocket: Any,
        connection_id: str,
        registry: SubscriptionRegistry,
    ) -> None:
        while not self.stop.is_set():
            update = await registry.next_update()
            if update.added:
                await self._send_clob(
                    websocket,
                    {"assets_ids": list(update.added), "operation": "subscribe"},
                    connection_id,
                )
            if update.removed:
                await self._send_clob(
                    websocket,
                    {"assets_ids": list(update.removed), "operation": "unsubscribe"},
                    connection_id,
                )

    async def _handle_clob(
        self, raw_value: str | bytes, connection_id: str, shard: str
    ) -> None:
        raw = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else raw_value
        wall_ns, monotonic_ns = wall_and_monotonic_ns()
        self.storage.clob_writer.write(
            raw,
            receive_timestamp_ns=wall_ns,
            receive_monotonic_ns=monotonic_ns,
            connection_id=connection_id,
        )
        self.health.clob.received(len(raw.encode()), wall_ns, connection_key=shard)
        if raw.strip() in {"PONG", ""}:
            self.health.clob.observed_event("heartbeat", None, wall_ns)
            return
        self.clob_queue.put_nowait((raw, wall_ns))
        self.health.max_clob_queue_depth = max(
            self.health.max_clob_queue_depth, self.clob_queue.qsize()
        )

    async def _clob_processor_loop(self) -> None:
        while not self.clob_producers_done.is_set() or not self.clob_queue.empty():
            try:
                first = await asyncio.wait_for(self.clob_queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            batch = [first]
            while len(batch) < 512:
                try:
                    batch.append(self.clob_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await asyncio.to_thread(self._process_clob_batch, batch)
            for _ in batch:
                self.clob_queue.task_done()

    def _process_clob_batch(self, batch: list[tuple[str, int]]) -> None:
        for raw, wall_ns in batch:
            self._process_clob(raw, wall_ns)

    def _process_clob(self, raw: str, wall_ns: int) -> None:
        try:
            observed = self.books.apply_raw(raw)
        except (
            BookDataError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            self.health.clob.parse_errors += 1
            self.health.book_parse_errors += 1
            if "before initial book snapshot" in str(exc):
                self.health.book_orphan_updates += 1
            self.health.clob.error(f"parse: {type(exc).__name__}: {exc}")
            return
        for event_type, timestamp_ms in observed:
            self.health.clob.observed_event(event_type, timestamp_ms, wall_ns)

    async def _send_clob(
        self, websocket: Any, payload: dict[str, Any], connection_id: str
    ) -> None:
        await self._send_text(
            websocket,
            json.dumps(payload, separators=(",", ":")),
            self.storage.clob_writer,
            connection_id,
        )

    async def _rtds_loop(self) -> None:
        connection_number = 0
        attempt = 0
        subscriptions = [
            {"topic": "crypto_prices_twap_thirty", "type": "update"},
            {"topic": "crypto_prices_twap_sixty", "type": "update"},
        ]
        subscriptions.extend(
            {
                "topic": "crypto_prices",
                "type": "*",
                "filters": json.dumps(
                    {"symbol": f"{asset.lower()}usdt"}, separators=(",", ":")
                ),
            }
            for asset in ("BTC", "ETH", "SOL", "XRP")
        )
        subscription = {
            "action": "subscribe",
            "subscriptions": subscriptions,
        }
        while not self.stop.is_set():
            connection_number += 1
            connection_id = f"rtds-{connection_number:04d}"
            try:
                async with connect(
                    self.config.rtds_ws_url,
                    open_timeout=self.config.http_timeout_seconds,
                    ping_interval=None,
                    max_size=None,
                ) as websocket:
                    self.health.rtds.connected("rtds")
                    attempt = 0
                    await self._send_text(
                        websocket,
                        json.dumps(subscription, separators=(",", ":")),
                        self.storage.rtds_writer,
                        connection_id,
                    )
                    await self._rtds_session(websocket, connection_id)
            except Exception as exc:
                if self.stop.is_set():
                    return
                message = f"{connection_id}: {type(exc).__name__}: {exc}"
                self.health.rtds.error(message)
                delay = min(2**attempt, self.config.reconnect_max_seconds)
                attempt += 1
                await self._wait_or_stop(delay)

    async def _rtds_session(self, websocket: Any, connection_id: str) -> None:
        receiver = asyncio.create_task(websocket.recv())
        stopper = asyncio.create_task(self.stop.wait())
        next_ping = time.monotonic() + self.config.rtds_heartbeat_seconds
        try:
            while not self.stop.is_set():
                timeout = max(0.0, next_ping - time.monotonic())
                done, _ = await asyncio.wait(
                    {receiver, stopper},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stopper in done:
                    return
                if receiver in done:
                    raw = receiver.result()
                    self._handle_rtds(raw, connection_id)
                    receiver = asyncio.create_task(websocket.recv())
                if time.monotonic() >= next_ping:
                    await self._send_text(
                        websocket,
                        "PING",
                        self.storage.rtds_writer,
                        connection_id,
                    )
                    next_ping = time.monotonic() + self.config.rtds_heartbeat_seconds
        finally:
            receiver.cancel()
            stopper.cancel()
            await asyncio.gather(receiver, stopper, return_exceptions=True)

    def _handle_rtds(self, raw: str | bytes, connection_id: str) -> None:
        raw_text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        wall_ns, monotonic_ns = wall_and_monotonic_ns()
        self.storage.rtds_writer.write(
            raw_text,
            receive_timestamp_ns=wall_ns,
            receive_monotonic_ns=monotonic_ns,
            connection_id=connection_id,
        )
        self.health.rtds.received(
            len(raw_text.encode()), wall_ns, connection_key="rtds"
        )
        if raw_text.strip() in {"PONG", ""}:
            self.health.rtds.observed_event("heartbeat", None, wall_ns)
            return
        try:
            observed = self.references.apply_raw(raw_text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self.health.rtds.parse_errors += 1
            self.health.rtds.error(f"parse: {type(exc).__name__}: {exc}")
            return
        for event_type, timestamp_ms in observed:
            self.health.rtds.observed_event(event_type, timestamp_ms, wall_ns)

    async def _send_text(
        self,
        websocket: Any,
        text: str,
        writer: RawStreamWriter,
        connection_id: str,
    ) -> None:
        await websocket.send(text)
        wall_ns, monotonic_ns = wall_and_monotonic_ns()
        writer.write(
            text,
            receive_timestamp_ns=wall_ns,
            receive_monotonic_ns=monotonic_ns,
            connection_id=connection_id,
            direction="outbound",
        )

    async def _heartbeat_loop(
        self,
        websocket: Any,
        writer: RawStreamWriter,
        connection_id: str,
        interval_seconds: float,
    ) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                await self._send_text(websocket, "PING", writer, connection_id)

    async def _wait_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass


async def _run_async(
    config_path: str | Path,
    output_root: str | Path,
    duration_seconds: int | None,
) -> Path:
    config = load_collector_config(config_path).with_duration(duration_seconds)
    storage = CollectorStorage(output_root, config)
    return await CollectorRuntime(config, storage).run()


def run_collector(
    config_path: str | Path,
    output_root: str | Path,
    *,
    duration_seconds: int | None = None,
) -> Path:
    return asyncio.run(_run_async(config_path, output_root, duration_seconds))
