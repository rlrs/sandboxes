from __future__ import annotations

from threading import Barrier, Event, Lock, Thread
import unittest

from sandboxes.image_inventory_cache import (
    ImageInventoryCache,
    ImageInventorySnapshot,
)


class ImageInventoryCacheTests(unittest.TestCase):
    def test_invalidation_does_not_wait_and_fences_stale_publish(self) -> None:
        load_started = Event()
        release_load = Event()
        invalidated = Event()
        calls = 0
        cache = ImageInventoryCache(ttl_seconds=5.0, clock=lambda: 0.0)

        def loader() -> ImageInventorySnapshot:
            nonlocal calls
            calls += 1
            if calls == 1:
                load_started.set()
                release_load.wait(timeout=2.0)
                return ImageInventorySnapshot.from_records(
                    [{"id": "stale"}], complete=False
                )
            return ImageInventorySnapshot.from_records(
                [{"id": "fresh"}], complete=True
            )

        first_result: list[ImageInventorySnapshot] = []
        first = Thread(target=lambda: first_result.append(cache.get_or_load(loader)))
        first.start()
        self.assertTrue(load_started.wait(timeout=1.0))

        def invalidate() -> None:
            cache.invalidate()
            invalidated.set()

        invalidator = Thread(target=invalidate)
        invalidator.start()
        completed_while_loading = invalidated.wait(timeout=1.0)
        release_load.set()
        invalidator.join(timeout=2.0)
        first.join(timeout=2.0)

        self.assertTrue(completed_while_loading)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_result[0].records, ({"id": "stale"},))
        self.assertFalse(first_result[0].complete)
        self.assertEqual(cache.get_or_load(loader).records, ({"id": "fresh"},))
        self.assertEqual(cache.get_or_load(loader).records, ({"id": "fresh"},))
        self.assertEqual(calls, 2)

    def test_loader_exception_wakes_waiter_and_allows_retry(self) -> None:
        start = Barrier(3)
        first_started = Event()
        release_failure = Event()
        guard = Lock()
        calls = 0
        results: list[ImageInventorySnapshot] = []
        errors: list[BaseException] = []
        cache = ImageInventoryCache(ttl_seconds=5.0, clock=lambda: 0.0)

        def loader() -> ImageInventorySnapshot:
            nonlocal calls
            with guard:
                calls += 1
                attempt = calls
            if attempt == 1:
                first_started.set()
                release_failure.wait(timeout=2.0)
                raise RuntimeError("inventory unavailable")
            return ImageInventorySnapshot.from_records(
                [{"id": "recovered"}], complete=True
            )

        def read() -> None:
            start.wait()
            try:
                snapshot = cache.get_or_load(loader)
            except BaseException as exc:
                with guard:
                    errors.append(exc)
            else:
                with guard:
                    results.append(snapshot)

        threads = [Thread(target=read) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        self.assertTrue(first_started.wait(timeout=1.0))
        release_failure.set()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(calls, 2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(str(errors[0]), "inventory unavailable")
        self.assertEqual(results[0].records, ({"id": "recovered"},))
        self.assertTrue(results[0].complete)
        self.assertEqual(cache.get_or_load(loader).records, ({"id": "recovered"},))
        self.assertEqual(calls, 2)

    def test_invalid_ttl_is_rejected(self) -> None:
        for ttl_seconds in (-1.0, float("nan"), float("inf")):
            with self.subTest(ttl_seconds=ttl_seconds), self.assertRaises(ValueError):
                ImageInventoryCache(ttl_seconds=ttl_seconds)


if __name__ == "__main__":
    unittest.main()
