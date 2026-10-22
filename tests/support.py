"""Fixtures that must not depend on the developer's environment or test order."""

from __future__ import annotations

import errno
from functools import lru_cache
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, TypeVar
import unittest
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
# The scripts under test insert this checkout themselves. Inserting it here as
# well makes SDK availability independent of which module was imported first.
_SDK_CHECKOUT = REPO_ROOT / "ucloud-sandboxes-sdk" / "src"
if _SDK_CHECKOUT.is_dir() and str(_SDK_CHECKOUT) not in sys.path:
    sys.path.insert(0, str(_SDK_CHECKOUT))

POSTGRES_DSN = os.environ.get("UCLOUD_TEST_POSTGRES_DSN", "")
POSTGRES_SKIP_REASON = "requires PostgreSQL (UCLOUD_TEST_POSTGRES_DSN)"
# For a test that opens routing inside subTest, which would swallow the fixture's skip.
requires_postgres = unittest.skipUnless(POSTGRES_DSN, POSTGRES_SKIP_REASON)

# A node's state roots are created under systemd's default UMask=0022.
NODE_DIRECTORY_MODE = 0o755
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def make_dirs(path: Path | str, *, mode: int = NODE_DIRECTORY_MODE) -> Path:
    """Create path and every missing parent with exactly mode.

    Product stores reject group- or world-writable directories. Path.mkdir
    applies the process umask (0002 on many developer machines) and
    os.makedirs applies mode only to the leaf, so fixtures use this instead.
    Like Path.mkdir(parents=True), an existing leaf is an error.
    """
    path = Path(path)
    missing = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    if not missing:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(path))
    for directory in reversed(missing):
        directory.mkdir(mode=mode)
        os.chmod(directory, mode)
    return path


def _parse_version(text: str) -> tuple[int, int, int]:
    match = _VERSION.match(text)
    if match is None:
        raise ValueError(f"unparseable SDK version {text!r}")
    return tuple(int(part) for part in match.groups())


@lru_cache(maxsize=None)
def sdk_version() -> tuple[int, int, int] | None:
    try:
        import ucloud_sandboxes_sdk
    except ImportError:
        return None
    return _parse_version(ucloud_sandboxes_sdk.__version__)


def sdk_skip_reason(minimum: str | None = None) -> str | None:
    """Why SDK tests cannot run here, or None when they can."""
    version = sdk_version()
    if version is None:
        return (
            "requires ucloud_sandboxes_sdk (an ucloud-sandboxes-sdk/src checkout "
            "or PYTHONPATH)"
        )
    if minimum is not None and version < _parse_version(minimum):
        found = ".".join(map(str, version))
        return f"requires ucloud_sandboxes_sdk >= {minimum}; found {found}"
    return None


def requires_sdk(minimum: str | None = None):
    reason = sdk_skip_reason(minimum)
    return unittest.skipIf(reason is not None, reason or "")


def skip_module(module: str, reason: str):
    """A load_tests hook that reports a whole module as one skipped test.

    Raising SkipTest at import works under discovery but is an error under
    ``python -m unittest tests.test_x``; load_tests works under both.
    """

    def load_tests(loader, tests, pattern):
        del tests, pattern

        @unittest.skip(reason)
        class ModuleSkipped(unittest.TestCase):
            def test_module(self):
                pass

        ModuleSkipped.__module__ = module
        ModuleSkipped.__qualname__ = "ModuleSkipped"
        return loader.suiteClass([ModuleSkipped("test_module")])

    return load_tests


_T = TypeVar("_T")


def wait_until(condition: Callable[[], _T], timeout: float = 10.0, message: str = "") -> _T:
    """Return condition()'s first truthy value, polling until timeout seconds pass.

    Wait for the end state a test asserts on, not for an intermediate signal:
    background work may still be writing after an event fires. A passing test
    returns as soon as the state holds; only a broken one waits the full timeout.
    """
    deadline = time.monotonic() + timeout
    delay = 0.001
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(message or f"condition not met within {timeout} s")
        time.sleep(delay)
        delay = min(delay * 2, 0.05)


def build_init_binary(directory: Path) -> Path:
    """Build runtime/managed_process (ucloud-sandbox-init) into directory."""
    go = shutil.which("go")
    if go is None:
        raise unittest.SkipTest("Go toolchain is unavailable")
    binary = directory / "ucloud-sandbox-init"
    build_env = {key: value for key, value in os.environ.items() if key not in {"GOARCH", "GOFLAGS", "GOOS"}}
    build_env.update({"CGO_ENABLED": "0", "GOCACHE": str(directory / "go-build-cache"), "GOTOOLCHAIN": "local"})
    build = subprocess.run(
        [go, "build", "-trimpath", "-o", str(binary), "."],
        cwd=REPO_ROOT / "runtime/managed_process", env=build_env, capture_output=True, timeout=300, check=False,
    )
    if build.returncode != 0:
        raise AssertionError(build.stderr.decode(errors="replace"))
    return binary


# -- PostgreSQL routing ---------------------------------------------------------
#
# Routing runs only on PostgreSQL. Each routes file gets its own schema, created
# by the production init (init_routing_authority) and dropped at cleanup.

_ROUTING_INIT_LOCK = threading.Lock()
_DSN_DIRECTORY: tempfile.TemporaryDirectory | None = None
# Routes files initialised here whose schema is not yet dropped.
_ROUTING_FILES: set[Path] = set()


def _dsn_file() -> Path:
    """One 0600 DSN file per test process, outside every test's own directory."""
    global _DSN_DIRECTORY
    if _DSN_DIRECTORY is None:
        _DSN_DIRECTORY = tempfile.TemporaryDirectory(prefix="routing-dsn-")
        path = Path(_DSN_DIRECTORY.name) / "routing.dsn"
        path.write_text(POSTGRES_DSN, encoding="utf-8")
        path.chmod(0o600)
    return Path(_DSN_DIRECTORY.name) / "routing.dsn"


def create_routing_file(path: Path) -> str:
    """Initialise ``path`` as a routing descriptor for a fresh schema; return it."""
    if not POSTGRES_DSN:
        raise unittest.SkipTest(POSTGRES_SKIP_REASON)
    from sandboxes.shared_control.routing_repository import init_routing_authority

    schema = "ucloud_routing_test_" + uuid4().hex
    init_routing_authority(path, dsn_file=_dsn_file(), schema=schema)
    return schema


def drop_postgres_schema(schema: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
        # A leaked transaction must fail the cleanup, not hang it.
        connection.execute("SET lock_timeout = '10s'")
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )


def drop_routing_schema(path: Path, schema: str) -> None:
    """Close this process's store for ``path`` and drop its schema."""
    from sandboxes.routing import close_routing_store

    close_routing_store(path)
    drop_postgres_schema(schema)
    with _ROUTING_INIT_LOCK:
        _ROUTING_FILES.discard(Path(path).resolve())


def postgres_routing_file(test: Any, path: Path) -> Path:
    """The routes file ``path`` as a descriptor for its own PostgreSQL schema.

    Skips without UCLOUD_TEST_POSTGRES_DSN. The first call for a file
    initialises it; later calls return it unchanged. ``test`` is a TestCase
    (or a TestCase class, from setUpClass), which drops the schema at cleanup.
    A file removed before that cleanup (its directory deleted) is an error:
    initialising it again would leave this process's open store on the old
    schema.
    """
    path = Path(path).resolve()
    with _ROUTING_INIT_LOCK:
        if path.exists():
            return path
        if path in _ROUTING_FILES:
            raise AssertionError(
                f"routes file {path} was removed while its schema is in use; "
                "read it before its directory is deleted"
            )
        schema = create_routing_file(path)
        _ROUTING_FILES.add(path)
    cleanup = test.addClassCleanup if isinstance(test, type) else test.addCleanup
    cleanup(drop_routing_schema, path, schema)
    return path


def routing_store_for(test: Any, path: Path):
    """The routing store for routes file ``path``, initialised on first use.

    Within a process this is the same store every later call (and a gateway
    built on ``path``) gets; use reopen_routing_store to read as after a restart.
    """
    from sandboxes.routing import open_routing_store

    return open_routing_store(postgres_routing_file(test, path))


def reopen_routing_store(path: Path):
    """Close this process's store for ``path`` and open a new one, as a restart does.

    Whatever still holds the old store (a gateway, a committer) must not be
    used afterwards.
    """
    from sandboxes.routing import close_routing_store, open_routing_store

    close_routing_store(path)
    return open_routing_store(path)
