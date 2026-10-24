"""Layout-2 components keep file mtimes, so Python's .pyc caches stay valid."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sandboxes.environment_builder import normalize_directory_times

SOURCE_TIME = 1_700_000_000
SOURCE = b"VALUE = 'source'\n"


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
