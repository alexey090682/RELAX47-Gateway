"""Interruption tests against real SQLite, never a production database."""
import asyncio
import threading
import unittest

import test_runtime_cutover as fixture
from ha_sql_store import Store
from runtime_migration import STORES


class CancellationTests(unittest.TestCase):
    setUp = fixture.CutoverTests.setUp
    migrate = fixture.CutoverTests.migrate
    args = fixture.CutoverTests.args
    hass = fixture.CutoverTests.hass

    def test_cancelled_committed_save_keeps_revision_and_next_write(self):
        self.migrate()

        async def exercise():
            store = Store(self.hass(), 1, STORES[0])
            await store.async_load()
            entered, release = threading.Event(), threading.Event()
            original = store._save

            def delayed(payload):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('Test gate timed out')
                original(payload)

            store._save = delayed
            first = asyncio.create_task(store.async_save({'value': 'first'}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()  # Repeated cancellation must not release the lock.
                await asyncio.sleep(0)
                self.assertTrue(store.lock.locked())
                self.assertFalse(first.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(store.revision, 2)
            store._save = original
            await store.async_save({'value': 'second'})
            self.assertEqual(store.revision, 3)
            restarted = Store(self.hass(), 1, STORES[0])
            self.assertEqual(await restarted.async_load(), {'value': 'second'})

        asyncio.run(exercise())

    def test_executor_failure_does_not_advance_revision(self):
        self.migrate()

        async def exercise():
            store = Store(self.hass(), 1, STORES[0])
            original_data = await store.async_load()
            original = store._save

            def failed(_):
                raise OSError('Simulated storage failure')

            store._save = failed
            with self.assertRaises(OSError):
                await store.async_save({'new': True})
            self.assertEqual(store.revision, 1)
            reopened = Store(self.hass(), 1, STORES[0])
            self.assertEqual(await reopened.async_load(), original_data)
            store._save = original
            await store.async_save({'recovered': True})
            self.assertEqual(store.revision, 2)

        asyncio.run(exercise())

    def test_invalid_numbers_leave_existing_data_intact(self):
        self.migrate()

        async def exercise():
            store = Store(self.hass(), 1, STORES[0])
            original_data = await store.async_load()
            for invalid in (float('nan'), float('inf'), float('-inf')):
                with self.assertRaises(ValueError):
                    await store.async_save({'amount': invalid})
            self.assertEqual(store.revision, 1)
            self.assertEqual(await Store(self.hass(), 1, STORES[0]).async_load(), original_data)

        asyncio.run(exercise())
