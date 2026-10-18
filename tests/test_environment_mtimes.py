"""Layout-2 layer components keep file mtimes, so Python's .pyc caches stay valid.

The view tests run everywhere: they record the tree the builder hands mkfs.
The real tests need erofs-utils 1.9+ (mkfs.erofs --mkfs-time --MZ and fsck.erofs
--extract=DIR). They publish through the builder's layer-group path with its
exact mkfs flags, then read the signed image back through a host extraction.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import py_compile
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.test_environment_artifact import MemoryRegistry
from tests.test_environment_layers import Keys
from tests.test_oci_layer_materialize import REPOSITORY, MemoryRegistry as BlobRegistry, directory, layer, member
from sandboxes.environment_artifact import EnvironmentArtifactRegistry
from sandboxes.environment_builder import FreshEnvironmentBuilder, normalize_directory_times
from sandboxes.oci_layer_materialize import materialize_layers

SOURCE_TIME = 1_700_000_000
LINK_TIME = 1_650_000_000
# Equal sizes: the cached bytecode is valid for SOURCE only while the image
# keeps SOURCE's mtime, and importing then returns the cached value.
SOURCE = b"VALUE = 'source'\n"
CACHED = b"VALUE = 'cached'\n"
CACHE = "app/__pycache__/mod." + sys.implementation.cache_tag + ".pyc"
# Header times of every regular file and symlink in the two layers.
FILE_TIMES = {"app/mod.py": SOURCE_TIME, CACHE: SOURCE_TIME + 5, "app/link": LINK_TIME,
              "tool/run.py": SOURCE_TIME + 9}


def timed(entry, mtime):
    entry[0].mtime = mtime
    return entry


def compiled(root):
    """Timestamp bytecode of CACHED, stamped with SOURCE's mtime and size."""
    source = root / "compile" / "mod.py"
    source.parent.mkdir()
    source.write_bytes(CACHED)
    os.utime(source, (SOURCE_TIME, SOURCE_TIME))
    target = root / "compile" / "mod.pyc"
    py_compile.compile(str(source), cfile=str(target), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    return target.read_bytes()


class LayoutTwoFixture:
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.keys = Keys()
        stack = ExitStack()
        self.addCleanup(stack.close)
        # Extraction is privileged in production; tests keep their own owner.
        stack.enter_context(patch("sandboxes.oci_layer_materialize.os.chown"))
        package = layer([timed(directory("app"), 1_600_000_000), timed(member("app/mod.py", SOURCE), SOURCE_TIME),
                         timed(directory("app/__pycache__"), 1_600_000_001),
                         timed(member(CACHE, compiled(self.root)), FILE_TIMES[CACHE]),
                         timed(member("app/link", kind=tarfile.SYMTYPE, linkname="mod.py"), LINK_TIME)])
        tool = layer([timed(directory("tool"), 1_600_000_002),
                      timed(member("tool/run.py", b"print('run')\n"), FILE_TIMES["tool/run.py"])])
        self.layers = [package, tool]
        self.blobs = BlobRegistry(self.layers)

    def layer_format(self, builder):
        return builder.layer_format()

    def publish(self, name, *, preserve, consume=True, count=2, directory_time):
        """Publish one group with a fresh builder and registry; nothing is reused."""
        layers = self.layers[:count]
        registry = EnvironmentArtifactRegistry(MemoryRegistry(), "environments", self.keys.trusted)
        builder = FreshEnvironmentBuilder(None, registry, self.keys.key, self.root / name / "work",
                                          preserve_mtimes=preserve)
        directories = materialize_layers(self.blobs, REPOSITORY, [item[0] for item in layers],
                                         [item[1] for item in layers], self.root / name / "diffs")
        # Another extraction, squash or Docker layer creation makes directories
        # at another time. Only file and symlink times come from the tar headers.
        for top in directories:
            for path in (top, *top.rglob("*")):
                if path.is_dir() and not path.is_symlink():
                    os.utime(path, ns=(directory_time, directory_time))
        digest, reused = builder._publish_layer_group(
            directories, [item[1] for item in layers], lower_dirs=(), parent=None,
            layer_format=self.layer_format(builder), consume_private_diffs=consume)
        self.assertFalse(reused)
        return registry, registry.load(digest)


class NormalizeDirectoryTimesTests(unittest.TestCase):
    def test_directory_and_whiteout_times_go_to_zero_and_file_and_symlink_times_stay(self):
        with TemporaryDirectory() as temporary:
            view = Path(temporary) / "view"
            (view / "app" / "lib").mkdir(parents=True)
            (view / "app" / "mod.py").write_bytes(SOURCE)
            (view / "app" / "link").symlink_to("mod.py")
            # A FIFO stands in for a 0:0 whiteout device, which needs root.
            os.mkfifo(view / "app" / "lib" / "gone")
            for path in (view, *view.rglob("*")):
                os.utime(path, ns=(7, SOURCE_TIME * 10**9), follow_symlinks=False)
            normalize_directory_times(view)
            self.assertEqual({str(path.relative_to(view)): path.lstat().st_mtime_ns for path in (view, *view.rglob("*"))},
                             {".": 0, "app": 0, "app/lib": 0, "app/lib/gone": 0,
                              "app/mod.py": SOURCE_TIME * 10**9, "app/link": SOURCE_TIME * 10**9})


if __name__ == "__main__":
    unittest.main()
