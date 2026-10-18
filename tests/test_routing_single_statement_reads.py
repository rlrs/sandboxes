"""Exact-owner reads avoid transaction setup without weakening placement snapshots."""

import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from tests.test_routing import sandbox_route

TEST_TIER = "contract"

DSN = os.environ.get("UCLOUD_TEST_POSTGRES_DSN")


@unittest.skipUnless(DSN, "requires isolated PostgreSQL")
class SingleStatementRoutingTests(unittest.TestCase):
    def setUp(self):
        from sandboxes.shared_control.routing_repository import PostgresRoutingStore

        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "routes"
        self.schema = "ucloud_routing_single_" + uuid4().hex
        self.store = PostgresRoutingStore(self.path, dsn=DSN, schema=self.schema)
        self.store.migrate()
        self.route = self.store.upsert_sandbox(
            sandbox_route(
                sandbox_id="single",
                node_id="n",
                job_id="j",
                node_url="http://node",
                state="running",
            )
        )

    def tearDown(self):
        import psycopg
        from psycopg import sql

        self.store.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema))
            )
        self.temp.cleanup()

    def test_replaced_authority_descriptor_fails_before_read(self):
        self.path.write_text("descriptor")
        self.store.bind_authority()
        replacement = self.path.with_suffix(".replacement")
        replacement.write_text("changed")
        replacement.replace(self.path)
        # Replacement is detected by the next once-a-second recheck.
        self.store.get_sandbox("single")
        self.store._authority_checked_at = float("-inf")
        with self.assertRaisesRegex(sqlite3.DatabaseError, "descriptor was replaced"):
            self.store.get_sandbox("single")

    def test_query_failure_is_mapped_and_does_not_poison_pool(self):
        with self.assertRaisesRegex(sqlite3.DatabaseError, "routing read failed"):
            self.store._fetchone_readonly("SELECT * FROM nonexistent_read_table")
        self.assertEqual(self.store.get_sandbox("single"), self.route)
