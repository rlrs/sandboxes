from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import socket
import sqlite3
import struct
import tempfile
import threading
import time
import unittest

from sandboxes.background_io import BackgroundPacer, Pressure
from sandboxes.durable_batch import DurableSqliteBatch
from sandboxes.storage_native import AgentEnvUblkClient

TEST_TIER = "contract"


class GroupCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "journal"
        with closing(self.connect()) as conn:
            conn.execute("CREATE TABLE values_test (id INTEGER PRIMARY KEY)")

    def connect(self):
        c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        return c

    def read(self):
        with closing(self.connect()) as c:
            return [
                row[0] for row in c.execute("SELECT id FROM values_test ORDER BY id")
            ]

    def wait_for_operations(self, batch, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with batch._condition:
                if batch._batch is not None and batch._batch.operations == count:
                    return
            time.sleep(0.005)
        self.fail("batch did not accept operations")

    def batch_ready(self, batch, count):
        self.wait_for_operations(batch, count)
        with batch._condition:
            self.assertEqual(self.read(), [])
            batch._batch.deadline = 0
            batch._flush_condition.notify()

    def test_concurrent_closed_batches_make_durable_progress(self):
        batch = DurableSqliteBatch(self.connect, lambda: None, max_operations=2)
        barrier = threading.Barrier(64)

        def write(i):
            barrier.wait(10)
            for j in range(4):
                with batch.transaction() as conn:
                    conn.execute("INSERT INTO values_test VALUES (?)", (i * 4 + j,))
                with closing(self.connect()) as reader:
                    self.assertIsNotNone(reader.execute("SELECT id FROM values_test WHERE id=?", (i * 4 + j,)).fetchone())

        with ThreadPoolExecutor(max_workers=64) as pool:
            futures = [pool.submit(write, i) for i in range(64)]
            for future in futures:
                future.result(20)
        self.assertEqual(self.read(), list(range(256)))

    def test_batch_is_invisible_until_durable_and_bad_operation_is_isolated(self):
        batch = DurableSqliteBatch(self.connect, lambda: None, delay_seconds=10)

        def write(i):
            with batch.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO values_test VALUES (?)", (i,))
                if i == 3:
                    raise ValueError("bad operation")
                conn.commit()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(write, i) for i in range(8)]
            self.batch_ready(batch, 8)
            for i, future in enumerate(futures):
                if i == 3:
                    with self.assertRaisesRegex(ValueError, "bad operation"):
                        future.result(3)
                else:
                    future.result(3)
        self.assertEqual(self.read(), [0, 1, 2, 4, 5, 6, 7])
        self.assertEqual(batch.commits, 1)
        self.assertEqual(batch.operations, 8)

    def test_commit_failure_fails_all_waiters_and_next_batch_can_recover(self):
        class FailingConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                raise sqlite3.OperationalError("injected fsync failure")

        batch = DurableSqliteBatch(
            lambda: FailingConnection(self.connect()), lambda: None, delay_seconds=10
        )

        def write(i):
            with batch.transaction() as conn:
                conn.execute("INSERT INTO values_test VALUES (?)", (i,))

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(write, i) for i in range(4)]
            self.batch_ready(batch, 4)
            for future in futures:
                with self.assertRaisesRegex(sqlite3.OperationalError, "fsync"):
                    future.result(3)
        self.assertEqual(self.read(), [])
        batch.connect, batch.delay = self.connect, 0
        write(9)
        self.assertEqual(self.read(), [9])

    def test_sqlite_transaction_abort_fails_other_unacknowledged_writes(self):
        batch = DurableSqliteBatch(self.connect, lambda: None, delay_seconds=10)

        def write(rollback=False):
            with batch.transaction() as conn:
                conn.execute(
                    "INSERT OR ROLLBACK INTO values_test VALUES (1)"
                    if rollback
                    else "INSERT INTO values_test VALUES (1)"
                )

        with ThreadPoolExecutor() as pool:
            first = pool.submit(write)
            self.wait_for_operations(batch, 1)
            with self.assertRaises(sqlite3.Error):
                write(True)
            with self.assertRaises(sqlite3.Error):
                first.result(2)
        self.assertEqual(self.read(), [])
        batch.delay = 0
        write()
        self.assertEqual(self.read(), [1])

    def test_ack_waits_for_commit_and_metrics_do_not(self):
        entered, release = threading.Event(), threading.Event()

        class SlowConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test release")
                self.connection.commit()

        batch = DurableSqliteBatch(lambda: SlowConnection(self.connect()), lambda: None)

        def write():
            with batch.transaction() as conn:
                conn.execute("INSERT INTO values_test VALUES (1)")

        with ThreadPoolExecutor() as pool:
            future = pool.submit(write)
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(future.done())
                self.assertEqual(self.read(), [])
                self.assertEqual(batch.metrics()["journal_batch_commits"], 0)
            finally:
                release.set()
            future.result(3)
        self.assertEqual(self.read(), [1])

    def test_uncommitted_explicit_operation_is_rolled_back(self):
        batch = DurableSqliteBatch(self.connect, lambda: None)
        with batch.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO values_test VALUES (1)")
        self.assertEqual(self.read(), [])


class BackgroundSchedulingTests(unittest.TestCase):
    def test_pacing_tracks_pressure_and_resumes_without_a_rate_ceiling(self):
        now, waits, observed = [0.0], [], [Pressure(io_stall=75)]

        def sleep(value):
            waits.append(value)
            now[0] += value

        pacer = BackgroundPacer(lambda: observed[0], clock=lambda: now[0], sleep=sleep)
        now[0] += 0.01
        pacer.pace()
        self.assertAlmostEqual(waits[0], 0.03)
        observed[0] = Pressure(io_stall=0)
        now[0] += 0.01
        pacer.pace()
        self.assertEqual(len(waits), 1)
        observed[0] = Pressure(io_stall=100)
        now[0] += 5
        pacer.pace()
        self.assertLessEqual(waits[-1], 0.1)

    def test_foreground_work_receives_priority_before_psi_catches_up(self):
        now, waits = [0.0], []
        pacer = BackgroundPacer(
            lambda: Pressure(io_stall=0),
            foreground=lambda: True,
            clock=lambda: now[0],
            sleep=waits.append,
        )
        now[0] = 0.02
        pacer.pace()
        self.assertEqual(waits, [0.02])



    def test_native_control_wait_uses_export_progress_but_stalls_expire(self):
        for progressing in [True, False]:
            with (
                self.subTest(progressing=progressing),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "socket"
                last = [time.monotonic()]
                server = socket.socket(socket.AF_UNIX)
                self.addCleanup(server.close)
                server.bind(str(path))
                server.listen()

                def serve():
                    conn, _ = server.accept()
                    with conn:
                        size = struct.unpack(
                            ">I", AgentEnvUblkClient._recv_exact(conn, 4)
                        )[0]
                        AgentEnvUblkClient._recv_exact(conn, size)
                        for _ in range(10):
                            time.sleep(0.015)
                            if progressing:
                                last[0] = time.monotonic()
                        try:
                            conn.sendall(struct.pack(">I", 15) + b'{"status":"ok"}')
                        except BrokenPipeError:
                            pass

                thread = threading.Thread(target=serve)
                thread.start()
                try:
                    client = AgentEnvUblkClient(path, timeout_seconds=0.05)
                    if progressing:
                        self.assertEqual(
                            client._call({"kind": "test"}, progress=lambda: last[0]),
                            {"status": "ok"},
                        )
                    else:
                        with self.assertRaises(socket.timeout):
                            client._call({"kind": "test"}, progress=lambda: last[0])
                finally:
                    thread.join(2)
