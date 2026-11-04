"""Online collection of staged commit uppers.

A worker uploads a sandbox's raw upper as a lone blob in ``commits/<hash>``
(``commit_policy.staging_repository``). The builder reads it within that
build's execution deadline, and no manifest ever references it. The quiescent
sweep would reclaim it, but only in a maintenance window, so without this
collector every sandbox build left up to 8 GiB on the registry volume (it
filled the 500 GB volume on 2026-11-04).

Links go through the registry's DELETE API, which keeps its blob cache
coherent. Blob data goes once no repository links it. An upload of the same
bytes racing the data removal leaves a link without data: the registry then
answers 404 for that blob, and the next export uploads it again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import time
from typing import Callable, Iterable

from .registry_sweep import DistributionTree, _HEX

STAGING_ROOT = "commits"


@dataclass(frozen=True)
class StagingRepository:
    name: str
    links: dict[str, float]  # digest -> link mtime
    last_upload: float  # newest file under _uploads; 0 when none


@dataclass
class StagedCollection:
    unlinked: int = 0
    unlink_failures: int = 0
    deleted_blobs: int = 0
    deleted_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"unlinked": self.unlinked, "unlink_failures": self.unlink_failures,
                "deleted_blobs": self.deleted_blobs, "deleted_bytes": self.deleted_bytes,
                "errors": self.errors[:5]}


def expired_links(repositories: Iterable[StagingRepository], *, cutoff: float) -> list[tuple[str, str]]:
    """(repository, digest) for every link of a staging repository untouched since ``cutoff``."""

    return [(repo.name, digest) for repo in repositories
            if repo.links and max(repo.links.values()) < cutoff and repo.last_upload < cutoff
            for digest in sorted(repo.links)]


def orphaned_blobs(
    digests: Iterable[str], live: set[str], data_mtime: Callable[[str], float | None], *, cutoff: float,
) -> list[str]:
    """Digests no repository links whose data was written before ``cutoff``."""

    return [digest for digest in sorted(set(digests))
            if digest not in live and (written := data_mtime(digest)) is not None and written < cutoff]


def _newest(path: Path) -> float:
    newest = 0.0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            try:
                newest = max(newest, os.stat(os.path.join(directory, name)).st_mtime)
            except FileNotFoundError:
                pass
    return newest


def _data_mtime(tree: DistributionTree, digest: str) -> float | None:
    try:
        return (tree.blob_dir(digest) / "data").stat().st_mtime
    except FileNotFoundError:
        return None


def collect_staged_commits(
    root: Path,
    delete_link: Callable[[str, str], None],
    *,
    grace_seconds: float,
    clock: Callable[[], float] = time.time,
) -> StagedCollection:
    """Unlink staged uppers older than ``grace_seconds``, then delete orphaned data.

    ``delete_link(repository, digest)`` is the registry's blob DELETE. It
    removes the link file and leaves its directory, which marks the digest
    for the data pass here or in a later run.
    """

    tree = DistributionTree(root)
    staging = tree.repositories / STAGING_ROOT
    cutoff = clock() - grace_seconds
    result = StagedCollection()
    try:
        names = sorted(entry.name for entry in os.scandir(staging) if entry.is_dir(follow_symlinks=False))
    except FileNotFoundError:
        return result
    repositories = [StagingRepository(f"{STAGING_ROOT}/{name}", tree.links(staging / name / "_layers"),
                                      _newest(staging / name / "_uploads")) for name in names]
    for repository, digest in expired_links(repositories, cutoff=cutoff):
        try:
            delete_link(repository, digest)
            result.unlinked += 1
        except (OSError, ValueError) as exc:
            result.unlink_failures += 1
            result.errors.append(f"{repository} {digest}: {exc}")
    # Directories whose link file is gone: what DELETE left behind.
    unlinked: dict[str, list[Path]] = {}
    for name in names:
        try:
            entries = list(os.scandir(staging / name / "_layers" / "sha256"))
        except FileNotFoundError:
            continue
        for entry in entries:
            if _HEX.match(entry.name) and not os.path.exists(os.path.join(entry.path, "link")):
                unlinked.setdefault("sha256:" + entry.name, []).append(Path(entry.path))
    if not unlinked:
        return result
    live: set[str] = set()
    for _name, path in tree.repository_roots():
        live.update(tree.links(path / "_layers"))
    for digest in orphaned_blobs(unlinked, live, lambda digest: _data_mtime(tree, digest), cutoff=cutoff):
        data = tree.blob_dir(digest) / "data"
        try:
            size = data.stat().st_size
            data.unlink()
        except FileNotFoundError:
            continue
        try:
            data.parent.rmdir()
        except OSError:
            pass
        result.deleted_blobs += 1
        result.deleted_bytes += size
    for directories in unlinked.values():
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:  # a new link was written meanwhile
                pass
    return result
