from __future__ import annotations

import unittest

from polymath_1M.collector.runner import SubscriptionRegistry


class SubscriptionRegistryTest(unittest.IsolatedAsyncioTestCase):
    async def test_replaces_subscriptions_and_drains_stale_updates(self) -> None:
        registry = SubscriptionRegistry()
        first = registry.replace({"b", "a"})
        self.assertEqual(first.added, ("a", "b"))
        self.assertEqual(registry.snapshot_and_drain(), ("a", "b"))

        update = registry.replace({"b", "c"})
        self.assertEqual(update.added, ("c",))
        self.assertEqual(update.removed, ("a",))
        self.assertEqual(await registry.next_update(), update)


if __name__ == "__main__":
    unittest.main()
