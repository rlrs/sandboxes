from __future__ import annotations

from importlib import metadata
from functools import lru_cache
from pathlib import Path
import re
import tarfile


PACKAGE_NAME = "sandboxes"
DEFAULT_INIT_VERSION = "3"

NODE_LABEL = "ucloud-sandboxes/node"
GATEWAY_LABEL = "ucloud-sandboxes/gateway"
BUILDER_LABEL = "ucloud-sandboxes/builder"
RECONCILE_LABEL = "ucloud-sandboxes/reconcile"
RECONCILE_CYCLE_LABEL = "ucloud-sandboxes/reconcile-cycle"
CREATE_INDEX_LABEL = "ucloud-sandboxes/create-index"
DEPLOYMENT_LABEL = "ucloud-sandboxes/deployment"
AGENT_VERSION_LABEL = "ucloud-sandboxes/agent-version"
INIT_VERSION_LABEL = "ucloud-sandboxes/init-version"


@lru_cache(maxsize=1)
def package_version() -> str:
    # A running service belongs to one immutable release. Placement checks
    # every candidate's agent version; read wheel metadata once, not per candidate.
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        from . import __version__

        return __version__


_AGENT_RUNTIME_MEMBER = "runtime/agent/node-agent-runtime.tar"
_AGENT_DIST_INFO = re.compile(r"site-packages/sandboxes-([^/]+)\.dist-info/")
_worker_agent_version: str | None = None


def bundle_agent_version(bundle: Path) -> str:
    """The sandboxes release inside a node package bundle: what its workers run.

    Read from the agent runtime's dist-info, streaming the archive; the agent
    runtime is its second member, so this reads a small prefix of the bundle.
    """

    with tarfile.open(bundle, "r|gz") as outer:
        for member in outer:
            if member.name != _AGENT_RUNTIME_MEMBER:
                continue
            runtime = outer.extractfile(member)
            if runtime is None:
                break
            with tarfile.open(fileobj=runtime, mode="r|") as inner:
                for item in inner:
                    if match := _AGENT_DIST_INFO.search(item.name):
                        return match.group(1)
            break
    raise ValueError(f"{bundle} has no sandboxes agent runtime")


def use_worker_bundle(bundle: Path) -> str:
    """Expect workers to run ``bundle``'s release from now on in this process.

    The gateway host can then run a newer release than its workers: workers
    stay compatible until the configured bundle changes. Without a bundle
    (development, tests) workers are expected to run this release.
    """

    global _worker_agent_version
    _worker_agent_version = bundle_agent_version(bundle) if bundle.is_file() else None
    return worker_agent_version()


def worker_agent_version() -> str:
    """The release workers must run to take new work."""

    return _worker_agent_version or package_version()


def agent_version_is_schedulable(
    agent_version: str,
    *,
    expected: str | None = None,
) -> bool:
    """Return whether an agent may accept newly placed sandbox work."""

    return agent_version.strip() == (expected or worker_agent_version()).strip()


def service_health(service: str) -> dict[str, object]:
    return {
        "ok": True,
        "service": service,
        "version": package_version(),
    }
