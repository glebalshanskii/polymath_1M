from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    """A Polymarket endpoint did not return a usable JSON response."""


@dataclass(frozen=True)
class JsonResponse:
    data: Any
    body: bytes
    status: int
    url: str
    retrieved_at: str


class JsonTransport(Protocol):
    def get_json(
        self, base_url: str, path: str, params: dict[str, object]
    ) -> JsonResponse: ...


class UrllibJsonTransport:
    def __init__(self, timeout_seconds: float = 30.0, retries: int = 4) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if retries < 0:
            raise ValueError("retries must be non-negative")
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def get_json(
        self, base_url: str, path: str, params: dict[str, object]
    ) -> JsonResponse:
        query = urlencode(
            [
                (key, str(value).lower() if isinstance(value, bool) else str(value))
                for key, value in params.items()
            ]
        )
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        request = Request(
            url, headers={"Accept": "application/json", "User-Agent": "polymath-1M/0.1"}
        )

        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise ApiError(f"invalid JSON from {url}") from exc
                    return JsonResponse(
                        data=payload,
                        body=body,
                        status=response.status,
                        url=url,
                        retrieved_at=datetime.now(UTC).isoformat(),
                    )
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == self.retries:
                    detail = exc.read().decode("utf-8", errors="replace")[:500]
                    raise ApiError(f"HTTP {exc.code} for {url}: {detail}") from exc
            except URLError as exc:
                if attempt == self.retries:
                    raise ApiError(f"network error for {url}: {exc.reason}") from exc
            time.sleep(min(2**attempt, 8))

        raise AssertionError("retry loop exited unexpectedly")
