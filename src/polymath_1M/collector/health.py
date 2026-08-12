from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class StreamHealth:
    messages: int = 0
    bytes: int = 0
    connections: int = 0
    reconnects: int = 0
    parse_errors: int = 0
    last_receive_timestamp_ns: int | None = None
    max_receive_gap_ms: float = 0.0
    max_event_lag_ms: float = 0.0
    event_counts: Counter[str] = field(default_factory=Counter)
    connection_counts: Counter[str] = field(default_factory=Counter)
    connection_messages: Counter[str] = field(default_factory=Counter)
    connection_last_receive_ns: dict[str, int] = field(default_factory=dict)
    connection_max_gap_ms: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def connected(self, connection_key: str = "default") -> None:
        self.connections += 1
        self.connection_counts[connection_key] += 1
        if self.connection_counts[connection_key] > 1:
            self.reconnects += 1

    def received(
        self,
        raw_bytes: int,
        receive_timestamp_ns: int,
        *,
        connection_key: str = "default",
        event_type: str | None = None,
        event_timestamp_ms: int | None = None,
    ) -> None:
        if self.last_receive_timestamp_ns is not None:
            gap_ms = (receive_timestamp_ns - self.last_receive_timestamp_ns) / 1_000_000
            self.max_receive_gap_ms = max(self.max_receive_gap_ms, gap_ms)
        self.last_receive_timestamp_ns = receive_timestamp_ns
        self.messages += 1
        self.bytes += raw_bytes
        previous_receive = self.connection_last_receive_ns.get(connection_key)
        if previous_receive is not None:
            connection_gap_ms = (receive_timestamp_ns - previous_receive) / 1_000_000
            self.connection_max_gap_ms[connection_key] = max(
                self.connection_max_gap_ms.get(connection_key, 0.0),
                connection_gap_ms,
            )
        self.connection_last_receive_ns[connection_key] = receive_timestamp_ns
        self.connection_messages[connection_key] += 1
        if event_type:
            self.event_counts[event_type] += 1
        if event_timestamp_ms is not None:
            lag_ms = receive_timestamp_ns / 1_000_000 - event_timestamp_ms
            self.max_event_lag_ms = max(self.max_event_lag_ms, abs(lag_ms))

    def error(self, message: str) -> None:
        self.errors.append(message)

    def observed_event(
        self,
        event_type: str,
        event_timestamp_ms: int | None,
        receive_timestamp_ns: int,
    ) -> None:
        self.event_counts[event_type] += 1
        if event_timestamp_ms is not None:
            lag_ms = receive_timestamp_ns / 1_000_000 - event_timestamp_ms
            self.max_event_lag_ms = max(self.max_event_lag_ms, abs(lag_ms))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_counts"] = dict(sorted(self.event_counts.items()))
        payload["connection_counts"] = dict(sorted(self.connection_counts.items()))
        payload["connection_messages"] = dict(sorted(self.connection_messages.items()))
        payload["connection_last_receive_ns"] = dict(
            sorted(self.connection_last_receive_ns.items())
        )
        payload["connection_max_gap_ms"] = dict(
            sorted(self.connection_max_gap_ms.items())
        )
        return payload


@dataclass
class CollectorHealth:
    clob: StreamHealth = field(default_factory=StreamHealth)
    rtds: StreamHealth = field(default_factory=StreamHealth)
    discovery_runs: int = 0
    discovery_errors: list[str] = field(default_factory=list)
    book_parse_errors: int = 0
    book_orphan_updates: int = 0
    book_crossed_states: int = 0
    max_clob_queue_depth: int = 0

    def to_dict(self, stale_after_seconds: float, now_ns: int) -> dict[str, Any]:
        streams = {"clob": self.clob.to_dict(), "rtds": self.rtds.to_dict()}
        stale = {}
        for name, stream in (("clob", self.clob), ("rtds", self.rtds)):
            last = stream.last_receive_timestamp_ns
            stale[name] = (
                last is None or (now_ns - last) / 1_000_000_000 > stale_after_seconds
            )
        stale_connections = {
            name: {
                connection: (now_ns - last) / 1_000_000_000 > stale_after_seconds
                for connection, last in sorted(
                    stream.connection_last_receive_ns.items()
                )
            }
            for name, stream in (("clob", self.clob), ("rtds", self.rtds))
        }
        return {
            "streams": streams,
            "stale_at_end": stale,
            "stale_connections_at_end": stale_connections,
            "discovery_runs": self.discovery_runs,
            "discovery_errors": self.discovery_errors,
            "book_parse_errors": self.book_parse_errors,
            "book_orphan_updates": self.book_orphan_updates,
            "book_crossed_states": self.book_crossed_states,
            "max_clob_queue_depth": self.max_clob_queue_depth,
        }
