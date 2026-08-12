from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from typing import Any

from polymath_1M.audit.client import PolymarketClient
from polymath_1M.audit.http import JsonResponse


class RowTransport:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.requests: list[dict[str, object]] = []

    def get_json(
        self, base_url: str, path: str, params: dict[str, object]
    ) -> JsonResponse:
        self.requests.append(dict(params))
        start = int(params["start"])
        end = int(params["end"])
        offset = int(params["offset"])
        limit = int(params["limit"])
        selected = [row for row in self.rows if start <= int(row["timestamp"]) <= end]
        page = selected[offset : offset + limit]
        body = json.dumps(page).encode()
        return JsonResponse(
            data=page,
            body=body,
            status=200,
            url=f"{base_url}{path}?offset={offset}",
            retrieved_at=datetime.now(UTC).isoformat(),
        )


class AuditClientTest(unittest.TestCase):
    def test_recursively_splits_saturated_windows_without_duplicates(self) -> None:
        rows = [
            {"timestamp": timestamp, "conditionId": f"c{index}"}
            for index, timestamp in enumerate([1, 1, 2, 2, 3])
        ]
        transport = RowTransport(rows)
        archived: list[str] = []
        client = PolymarketClient(
            transport,
            lambda account, endpoint, response: archived.append(response.url),
        )

        result = [
            row
            for part in client._windowed_parts(
                account_id="a",
                endpoint="test",
                path="/test",
                params={},
                start=1,
                end=3,
                limit=2,
                max_offset=2,
            )
            for row in part
        ]

        self.assertEqual(
            [row["conditionId"] for row in result], ["c0", "c1", "c2", "c3", "c4"]
        )
        self.assertGreater(len(archived), 2)


if __name__ == "__main__":
    unittest.main()
