"""``ucloud-noded`` as a node's front door, launched as vm_init launches it.

``vm_init.py`` starts noded with ``--listen H:P --upstream-unix <agent.sock>
--rust-create --rust-exec --rust-pause --node-control-token-file F``; noded
reads everything else from the agent's ``GET /internal/v1/creates/config``.
``FrontDoor`` runs that command line over a node's agent socket:

- ``PATH`` starts with the node's ``bin``, so a command noded runs by name
  finds the fake (the agent reports absolute ones; see README).
- ``PR_SET_PDEATHSIG`` kills noded with the test process, so an aborted run
  leaves no daemon serving. Runtime invocations noded starts clear it and
  outlive a crashed noded, as on a worker.
- The port is the node's for its lifetime: the node URL is part of route
  identity, and a restarted noded binds it again.

noded is ready once it loaded the agent's configuration: its pause runtime
then writes ``<state_root>/noded/status.json`` under its new session.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import unittest

from tests.support import wait_until

REPO = Path(__file__).resolve().parents[2]
NODED = REPO / "runtime" / "noded"
_BUILD_GUARD = threading.Lock()
_BINARY: Path | None = None
_PR_SET_PDEATHSIG = 1


def noded_binary() -> Path:
    """``ucloud-noded`` built from this worktree (``UCLOUD_NODED_BINARY`` overrides)."""
    global _BINARY
    configured = os.environ.get("UCLOUD_NODED_BINARY")
    if configured:
        return Path(configured)
    with _BUILD_GUARD:
        if _BINARY is not None:
            return _BINARY
        cargo = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo")
        if not Path(cargo).exists():
            raise unittest.SkipTest("tests with ucloud-noded build runtime/noded and need cargo, which is not installed")
        built = subprocess.run([cargo, "build", "--offline", "--locked", "--bin", "ucloud-noded"], cwd=NODED,
                               capture_output=True, text=True, check=False)
        if built.returncode != 0:
            raise AssertionError(f"cargo build of ucloud-noded failed:\n{built.stderr[-4000:]}")
        target = Path(os.environ.get("CARGO_TARGET_DIR") or NODED / "target")
        _BINARY = target / "debug" / "ucloud-noded"
        return _BINARY


# Resolved before any fork: the child may only make the system call.
_prctl = ctypes.CDLL(None, use_errno=True).prctl


def _die_with_parent() -> None:
    if _prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0) != 0:
        os._exit(127)


def free_port() -> int:
    """A loopback port no listener holds now; noded binds it with SO_REUSEADDR."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FrontDoor:
    """One node's noded: ``--rust-create --rust-exec --rust-pause`` over the agent socket."""

    def __init__(self, *, port: int, agent_socket: Path, token_file: Path, state_root: Path,
                 bin_dir: Path, log: Path) -> None:
        self.port = port
        self.agent_socket = agent_socket
        self.token_file = token_file
        self.status_path = state_root / "noded" / "status.json"
        self.bin = bin_dir
        self.log = log
        self.process: subprocess.Popen | None = None
        # The daemon session of the last ready noded.
        self.session: str | None = None

    @property
    def argv(self) -> list[str]:
        return [str(noded_binary()), "--listen", f"127.0.0.1:{self.port}", "--upstream-unix", str(self.agent_socket),
                "--rust-create", "--rust-exec", "--rust-pause", "--node-control-token-file", str(self.token_file)]

    def _status_session(self) -> str | None:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8")).get("session")
        except (OSError, ValueError):
            return None

    def start(self) -> None:
        """Start noded; return once it serves the agent's configuration."""
        if self.process is not None:
            raise RuntimeError("noded is already running")
        previous = self._status_session()
        environment = dict(os.environ, PATH=os.pathsep.join((str(self.bin), os.environ.get("PATH", ""))))
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(self.argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                            env=environment, preexec_fn=_die_with_parent)

        def ready() -> str | None:
            if self.process.poll() is not None:
                raise AssertionError(f"noded exited at start:\n{self.log.read_text(errors='replace')[-4000:]}")
            session = self._status_session()
            return session if session and session != previous else None

        self.session = wait_until(ready, timeout=30, message="noded did not load the agent's configuration")

    def _end(self, sig: int) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        process.send_signal(sig)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def stop(self) -> None:
        """SIGTERM: noded finishes in-flight requests and stops its loops."""
        self._end(signal.SIGTERM)

    def crash(self) -> None:
        """SIGKILL: nothing of noded's own runs afterwards."""
        if self.process is None:
            raise RuntimeError("noded is not running")
        self._end(signal.SIGKILL)

    def restart(self) -> None:
        self.stop()
        self.start()
