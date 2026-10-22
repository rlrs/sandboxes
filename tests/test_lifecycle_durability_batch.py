from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import threading
import time
import unittest

from sandboxes.durable_batch import DurableSqliteBatch

TEST_TIER = "contract"


class DurableBatchTests(unittest.TestCase):
    def test_queued_writers_enter_in_order_across_commits(self):
        with TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=9) as pool:
            path = Path(tmp) / 'fifo.sqlite'
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('CREATE TABLE entries (value INTEGER)')
                conn.commit()
            committing, release = threading.Event(), threading.Event()
            class BlockedCommit(sqlite3.Connection):
                def commit(self):
                    committing.set()
                    if not release.wait(10):
                        raise TimeoutError('test did not release commit')
                    return super().commit()
            batch = DurableSqliteBatch(
                lambda: sqlite3.connect(path, check_same_thread=False, factory=BlockedCommit),
                lambda: None, delay_seconds=0, max_operations=1,
            )
            def write(value):
                with batch.transaction() as conn:
                    conn.execute('INSERT INTO entries VALUES (?)', (value,))
            first = pool.submit(write, -1)
            futures = []
            try:
                self.assertTrue(committing.wait(3))
                for i in range(8):
                    futures.append(pool.submit(write, i))
                    deadline = time.monotonic() + 3
                    while True:
                        with batch._writers_lock:
                            queued = len(batch._writers)
                        if queued == i + 1:
                            break
                        self.assertLess(time.monotonic(), deadline, 'writer did not queue')
                        time.sleep(0.001)
                self.assertFalse(any(f.done() for f in [first, *futures]))
            finally:
                release.set()
            for future in [first, *futures]:
                future.result(timeout=3)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(
                    [r[0] for r in conn.execute('SELECT value FROM entries ORDER BY rowid')],
                    [-1, *range(8)],
                )
