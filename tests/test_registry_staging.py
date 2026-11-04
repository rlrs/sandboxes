import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sandboxes.registry_staging import (
    StagingRepository,
    collect_staged_commits,
    expired_links,
    orphaned_blobs,
)
from sandboxes.registry_sweep import DistributionTree


HOUR = 3600.0
NOW = 1_800_000_000.0


class ExpiredLinkTests(unittest.TestCase):
    def test_only_repositories_untouched_since_the_cutoff_expire(self) -> None:
        cutoff = NOW - 2 * HOUR
        old = StagingRepository("commits/old", {"sha256:a": NOW - 5 * HOUR, "sha256:b": NOW - 4 * HOUR}, 0.0)
        fresh_link = StagingRepository("commits/fresh", {"sha256:c": NOW - 5 * HOUR, "sha256:d": NOW - HOUR}, 0.0)
        uploading = StagingRepository("commits/uploading", {"sha256:e": NOW - 5 * HOUR}, NOW - 60)
        empty = StagingRepository("commits/empty", {}, 0.0)

        self.assertEqual(expired_links([old, fresh_link, uploading, empty], cutoff=cutoff),
                         [("commits/old", "sha256:a"), ("commits/old", "sha256:b")])

    def test_orphans_are_unlinked_everywhere_and_written_before_the_cutoff(self) -> None:
        written = {"sha256:old": NOW - 5 * HOUR, "sha256:live": NOW - 5 * HOUR, "sha256:new": NOW - 60}

        self.assertEqual(orphaned_blobs(["sha256:old", "sha256:live", "sha256:new", "sha256:gone"],
                                        {"sha256:live"}, written.get, cutoff=NOW - 2 * HOUR),
                         ["sha256:old"])


class StagingRegistry:
    """A Distribution filesystem tree and its blob DELETE."""

    def __init__(self, root: Path) -> None:
        self.tree = DistributionTree(root)

    def _age(self, path: Path, age: float) -> None:
        os.utime(path, (NOW - age, NOW - age))

    def stage(self, repository: str, content: bytes, *, age: float) -> str:
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        data = self.tree.blob_dir(digest) / "data"
        data.parent.mkdir(parents=True, exist_ok=True)
        data.write_bytes(content)
        self._age(data, age)
        link = self.tree.repositories / repository / "_layers" / "sha256" / digest[7:] / "link"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text(digest)
        self._age(link, age)
        return digest

    def delete_link(self, repository: str, digest: str) -> None:
        # Distribution removes the link file and keeps its directory.
        (self.tree.repositories / repository / "_layers" / "sha256" / digest[7:] / "link").unlink()

    def exists(self, digest: str) -> bool:
        return (self.tree.blob_dir(digest) / "data").exists()

    def linked(self, repository: str, digest: str) -> bool:
        return (self.tree.repositories / repository / "_layers" / "sha256" / digest[7:] / "link").exists()


class CollectStagedCommitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.registry = StagingRegistry(self.root)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def collect(self):
        return collect_staged_commits(self.root, self.registry.delete_link, grace_seconds=2 * HOUR,
                                      clock=lambda: NOW)

    def test_finished_builds_lose_their_upper_and_running_builds_keep_it(self) -> None:
        finished = self.registry.stage("commits/finished", b"finished upper", age=5 * HOUR)
        running = self.registry.stage("commits/running", b"running upper", age=10 * 60)

        result = self.collect()

        self.assertFalse(self.registry.exists(finished))
        self.assertFalse(self.registry.linked("commits/finished", finished))
        self.assertTrue(self.registry.exists(running))
        self.assertTrue(self.registry.linked("commits/running", running))
        self.assertEqual((result.unlinked, result.deleted_blobs, result.deleted_bytes),
                         (1, 1, len(b"finished upper")))
        self.assertEqual(list((self.root / "docker/registry/v2/repositories/commits/finished/_layers/sha256")
                              .iterdir()), [])

    def test_data_another_repository_links_stays(self) -> None:
        upper = self.registry.stage("commits/finished", b"shared bytes", age=5 * HOUR)
        self.registry.stage("environments", b"shared bytes", age=5 * HOUR)

        self.collect()

        self.assertFalse(self.registry.linked("commits/finished", upper))
        self.assertTrue(self.registry.linked("environments", upper))
        self.assertTrue(self.registry.exists(upper))

    def test_a_failed_unlink_keeps_the_blob_and_is_reported(self) -> None:
        upper = self.registry.stage("commits/finished", b"finished upper", age=5 * HOUR)

        def refuse(repository, digest):
            raise OSError("registry unavailable")

        result = collect_staged_commits(self.root, refuse, grace_seconds=2 * HOUR, clock=lambda: NOW)

        self.assertTrue(self.registry.exists(upper))
        self.assertEqual((result.unlinked, result.unlink_failures, result.deleted_blobs), (0, 1, 0))

    def test_no_staging_repositories_is_a_no_op(self) -> None:
        self.assertEqual(self.collect().to_dict()["deleted_blobs"], 0)


if __name__ == "__main__":
    unittest.main()
