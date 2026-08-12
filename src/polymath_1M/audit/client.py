from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Lock
from typing import Any

from .http import JsonResponse, JsonTransport

ArchiveCallback = Callable[[str, str, JsonResponse], None]


class PaginationOverflow(RuntimeError):
    """A one-second window exceeded an endpoint's offset budget."""


class PolymarketClient:
    GAMMA_URL = "https://gamma-api.polymarket.com"
    DATA_URL = "https://data-api.polymarket.com"

    def __init__(
        self,
        transport: JsonTransport,
        archive: ArchiveCallback,
        progress: Callable[[str], None] | None = None,
        workers: int = 8,
    ) -> None:
        if workers <= 0:
            raise ValueError("workers must be positive")
        self.transport = transport
        self.archive = archive
        self.progress = progress or (lambda message: None)
        self.workers = workers
        self.request_counts: Counter[tuple[str, str]] = Counter()
        self._count_lock = Lock()

    def _get(
        self,
        account_id: str,
        endpoint: str,
        base_url: str,
        path: str,
        params: dict[str, object],
    ) -> Any:
        response = self.transport.get_json(base_url, path, params)
        self.archive(account_id, endpoint, response)
        key = (account_id, endpoint)
        with self._count_lock:
            self.request_counts[key] += 1
            count = self.request_counts[key]
        if count == 1 or count % 25 == 0:
            self.progress(f"{account_id}: {endpoint} requests={count}")
        return response.data

    def profile(self, account_id: str, address: str) -> dict[str, Any]:
        result = self._get(
            account_id,
            "profile",
            self.GAMMA_URL,
            "/public-profile",
            {"address": address},
        )
        if not isinstance(result, dict):
            raise TypeError("profile endpoint returned a non-object")
        return result

    def activity_parts(
        self, account_id: str, address: str, start: int, end: int
    ) -> Iterable[list[dict[str, Any]]]:
        return self._windowed_parts(
            account_id=account_id,
            endpoint="activity",
            path="/activity",
            params={
                "user": address,
                "sortBy": "TIMESTAMP",
                "sortDirection": "ASC",
                "excludeDepositsWithdrawals": True,
            },
            start=start,
            end=end,
            limit=500,
            max_offset=5000,
        )

    def trade_parts(
        self, account_id: str, address: str, start: int, end: int
    ) -> Iterable[list[dict[str, Any]]]:
        return self._windowed_parts(
            account_id=account_id,
            endpoint="trades",
            path="/trades",
            params={"user": address, "takerOnly": False},
            start=start,
            end=end,
            limit=5_000,
            max_offset=5_000,
        )

    def closed_position_parts(
        self, account_id: str, address: str, start: int, end: int
    ) -> Iterable[list[dict[str, Any]]]:
        limit = 50
        exhausted = True
        for offset in range(0, 100_001, limit):
            payload = self._get(
                account_id,
                "closed_positions",
                self.DATA_URL,
                "/closed-positions",
                {
                    "user": address,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "ASC",
                    "limit": limit,
                    "offset": offset,
                },
            )
            page_rows = _object_rows(payload, "closed-positions")
            filtered = [
                row
                for row in page_rows
                if start <= _integer(row.get("timestamp")) <= end
            ]
            if filtered:
                yield deduplicate_rows(filtered)
            timestamps = [_integer(row.get("timestamp")) for row in page_rows]
            if len(page_rows) < limit or (timestamps and min(timestamps) > end):
                exhausted = False
                break
        if exhausted:
            raise PaginationOverflow(
                f"closed-positions offset budget exceeded for {account_id}"
            )

    def _windowed_parts(
        self,
        *,
        account_id: str,
        endpoint: str,
        path: str,
        params: dict[str, object],
        start: int,
        end: int,
        limit: int,
        max_offset: int,
    ) -> Iterable[list[dict[str, Any]]]:
        day_windows = iter(_day_windows(start, end))
        with ThreadPoolExecutor(max_workers=self.workers) as executor:

            def submit(window: tuple[int, int]):
                return executor.submit(
                    self._bounded_window_rows,
                    account_id=account_id,
                    endpoint=endpoint,
                    path=path,
                    params=params,
                    start=window[0],
                    end=window[1],
                    limit=limit,
                    max_offset=max_offset,
                )

            pending = {submit(window) for window in _take(day_windows, self.workers)}
            while pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    yield deduplicate_rows(future.result())
                    if window := next(day_windows, None):
                        pending.add(submit(window))

    def _bounded_window_rows(
        self,
        *,
        account_id: str,
        endpoint: str,
        path: str,
        params: dict[str, object],
        start: int,
        end: int,
        limit: int,
        max_offset: int,
    ) -> list[dict[str, Any]]:
        pending = [(start, end)]
        completed: list[dict[str, Any]] = []
        while pending:
            window_start, window_end = pending.pop()
            window_rows: list[dict[str, Any]] = []
            saturated = False
            for offset in range(0, max_offset + 1, limit):
                query = dict(params)
                query.update(
                    {
                        "start": window_start,
                        "end": window_end,
                        "limit": limit,
                        "offset": offset,
                    }
                )
                payload = self._get(account_id, endpoint, self.DATA_URL, path, query)
                page_rows = _object_rows(payload, endpoint)
                window_rows.extend(page_rows)
                if len(page_rows) < limit:
                    break
                if offset + limit > max_offset:
                    saturated = True
            if not saturated:
                completed.extend(window_rows)
                continue
            if window_start >= window_end:
                raise PaginationOverflow(
                    f"{endpoint} has more rows than the offset budget at second {window_start}"
                )
            middle = (window_start + window_end) // 2
            pending.append((middle + 1, window_end))
            pending.append((window_start, middle))
        return completed


def deduplicate_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    import hashlib
    import json

    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        canonical = json.dumps(
            row, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        unique[hashlib.sha256(canonical).hexdigest()] = row
    return sorted(
        unique.values(),
        key=lambda row: (
            _integer(row.get("timestamp")),
            str(row.get("conditionId", "")),
        ),
    )


def _object_rows(payload: Any, endpoint: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or any(
        not isinstance(row, dict) for row in payload
    ):
        raise TypeError(f"{endpoint} returned a non-list response")
    return payload


def _integer(value: Any) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return 0


def _day_windows(start: int, end: int) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    cursor = start
    while cursor <= end:
        next_boundary = ((cursor // 86_400) + 1) * 86_400
        window_end = min(end, next_boundary - 1)
        windows.append((cursor, window_end))
        cursor = window_end + 1
    return windows


def _take(values: Iterable[tuple[int, int]], count: int) -> list[tuple[int, int]]:
    iterator = iter(values)
    result: list[tuple[int, int]] = []
    for _ in range(count):
        value = next(iterator, None)
        if value is None:
            break
        result.append(value)
    return result
