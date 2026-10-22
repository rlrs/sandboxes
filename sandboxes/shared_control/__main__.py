"""Explicit relay (and, with --routing-file, routing) schema migration and status."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


async def run(args):
    from .database import PostgresDatabase
    from .credentials import read_private_dsn

    store = PostgresDatabase(read_private_dsn(args.dsn_file), args.deployment_id, schema=args.schema)
    try:
        await store.open()
        if args.command == "migrate":
            await store.migrate()
            result = {"schema": args.schema, "authority": "relay", "version": store.schema_version, "migrated": True}
            if args.routing_file is not None:
                from .routing_repository import init_routing_authority

                # Synchronous psycopg: run it off the event loop.
                result.update(await asyncio.to_thread(
                    init_routing_authority, args.routing_file,
                    dsn_file=args.dsn_file, schema=args.routing_schema,
                ))
            return result
        from .relay import PostgresRelayState
        # Status is read-only: do not start dispatchers, record runtime mode,
        # initialize quota, or otherwise pretend this CLI is a relay process.
        return {"authority": "relay", "schema": args.schema, **await PostgresRelayState(store).stats()}
    finally:
        await store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "status"))
    parser.add_argument("--dsn-file", type=Path, required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--schema", default="ucloud_shared")
    parser.add_argument("--routing-file", type=Path,
                        help="migrate only: also create the PostgreSQL routing schema and "
                             "write this routing descriptor (<data_root>/routes.sqlite)")
    parser.add_argument("--routing-schema", default="ucloud_routing")
    args = parser.parse_args(argv)
    if args.routing_file is not None and args.command != "migrate":
        parser.error("--routing-file applies only to migrate")
    try:
        print(json.dumps(asyncio.run(run(args))))
    except ImportError:
        print("Install the server's postgres extra to use shared-control commands.", file=sys.stderr)
        return 1
    except Exception as exc:
        # Database connection errors may carry connection parameters. Never
        # print the DSN or an unfiltered exception to a shell/CI log.
        print(f"Shared-control {args.command} failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
