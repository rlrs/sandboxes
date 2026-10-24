"""EROFS symlink targets against real mkfs.erofs images."""
import os
from pathlib import Path
import random
import shutil
import socket
import stat
import struct
import subprocess
from tempfile import TemporaryDirectory
import unittest

from sandboxes.environment_builder import FreshEnvironmentBuilder, WHOLE_IMAGE_EXCLUDED
from sandboxes.erofs_metadata import UnsupportedErofs, symlink_targets

MKFS = shutil.which("mkfs.erofs")


def build_tree(root: Path, *, small_files=60, big_entries=160):
    """Deep trees, many small files, hardlinks, symlinks, xattrs, whiteouts."""
    rng = random.Random(7)
    root.mkdir()
    deep = root
    for depth in range(36):
        deep = deep / f"d{depth:02d}"
        deep.mkdir()
        (deep / "leaf").write_text(f"depth {depth}\n")
    small = root / "small"
    small.mkdir()
    for index in range(small_files):
        (small / f"file-{index:05d}-{'x' * (index % 60)}").write_bytes(rng.randbytes(index % 300))
    big = root / "bigdir"  # Several directory blocks.
    big.mkdir()
    for index in range(big_entries):
        (big / ("n" * (index % 200 + 1) + str(index))).touch()
    links = root / "links"
    links.mkdir()
    (links / "target").write_bytes(b"hard link target\n" * 100)
    for index in range(4):
        os.link(links / "target", links / f"hard-{index}")
    os.link(small / "file-00010-xxxxxxxxxx", links / "cross-dir-hard")
    os.symlink("target", links / "short")
    os.symlink("t" * 1500, links / "medium")
    os.symlink("/" + "long/" * 600 + "end", links / "long")
    xattrs = root / "xattrs"
    xattrs.mkdir()
    for index in range(30):
        path = xattrs / f"inline-{index}"
        path.write_text("x")
        os.setxattr(path, "user.unique", f"value-{index}".encode() * (index + 1))
    # Values on three or more inodes become shared; enough of them push the
    # shared xattr area beyond block 0.
    for index in range(60):
        for copy in range(3):
            path = xattrs / f"shared-{index}-{copy}"
            path.write_text("s")
            os.setxattr(path, f"user.pair{index % 7}", f"shared value {index} ".encode() * 6)
    big_value = xattrs / "big-value"
    big_value.write_text("y")
    os.setxattr(big_value, "user.big", rng.randbytes(3000))
    layer = root / "layer"
    layer.mkdir()
    opaque = layer / "opaque"
    opaque.mkdir()
    # trusted.overlay.opaque needs root; the user namespace has the same layout.
    name = "trusted.overlay.opaque" if os.geteuid() == 0 else "user.overlay.opaque"
    os.setxattr(opaque, name, b"y")
    (opaque / "kept").write_text("kept\n")
    for index in range(12):
        os.mknod(layer / f"whiteout-{index}", 0o600 | stat.S_IFCHR, os.makedev(0, 0))
    os.mkfifo(layer / "fifo")
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(layer / "socket"))
    data = root / "data"
    data.mkdir()
    # Compressible files of many logical-cluster counts, some with xattrs so
    # their compact indexes start at different 32-byte offsets.
    line = b"".join(b"line %d of compressible text\n" % index for index in range(70000))
    for index, size in enumerate((5000, 9000, 13000, 30000, 34000, 36000, 64000, 66000, 70000, 140000,
                                  400000, len(line))):
        path = data / f"text-{size}"
        path.write_bytes(line[:size])
        if index % 3:
            os.setxattr(path, "user.pad", b"p" * (4 * index + 1))
    (data / "random").write_bytes(rng.randbytes(300_000))
    (data / "random-tail").write_bytes(rng.randbytes(4096 * 3 + 1234))
    (data / "exact-block").write_bytes(rng.randbytes(8192))
    (data / "empty").touch()
    (root / "emptydir").mkdir()
    for excluded in WHOLE_IMAGE_EXCLUDED:
        (root / excluded).mkdir()
        (root / excluded / "excluded").write_text("must be excluded\n")


def builder_image(view: Path, image: Path, *, compression="lz4", preserve_mtimes=False):
    """mkfs.erofs exactly as FreshEnvironmentBuilder runs it (layout 1 by default)."""
    FreshEnvironmentBuilder(None, None, None, image.parent, compression=compression)._mkfs(
        image, view, exclude_runtime_mounts=True, preserve_mtimes=preserve_mtimes)
    return image


@unittest.skipUnless(MKFS, "mkfs.erofs (erofs-utils) is not installed")
class ErofsMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.view = cls.root / "view"
        try:
            build_tree(cls.view)
        except PermissionError as exc:  # Unprivileged whiteout mknod needs Linux >= 5.8.
            cls.directory.cleanup()
            raise unittest.SkipTest(f"fixture needs whiteout devices: {exc}")
        cls.image = builder_image(cls.view, cls.root / "builder.erofs")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_symlink_targets_are_byte_exact_by_path(self):
        # Rollback's source of truth: nydus-image unpack's tar drops '.' components.
        tree = self.root / "links"
        if not tree.exists():
            (tree / "share/terminfo/31").mkdir(parents=True)
            for index in range(300):  # A directory over one block.
                (tree / "share/terminfo/31" / f"entry-{index:04d}-{'x' * 20}").symlink_to(f".././61/adm{index}")
            (tree / "dot").symlink_to("./a/./b/../c")
            (tree / "long").symlink_to("y" * 4000)
            os.link(tree / "dot", tree / "dot-again", follow_symlinks=False)
        for view, image in ((tree, self.root / "links.erofs"), (self.view, self.image)):
            if not image.exists():
                subprocess.run([MKFS, "-T", "0", str(image), str(view)], check=True, capture_output=True)
            expected = {os.path.relpath(os.path.join(directory, name), view): os.readlink(os.path.join(directory, name))
                        for directory, names, files in os.walk(view) for name in names + files
                        if os.path.islink(os.path.join(directory, name))}
            with self.subTest(image=image.name):
                self.assertEqual(symlink_targets(image.read_bytes()), expected)

    def test_unqualified_features_and_layouts_are_refused_not_guessed(self):
        original = self.image.read_bytes()
        superblock = 1024

        def changed(offset, layout, value):
            data = bytearray(original)
            struct.pack_into(layout, data, offset, value)
            return bytes(data)

        incompat = struct.unpack_from("<I", original, superblock + 80)[0]
        # A RAFS v6 bootstrap's device table and blob devices are allowed.
        refused = {
            "not erofs": changed(superblock, "<I", 0x12345678),
            "ztailpacking": changed(superblock + 80, "<I", incompat | 0x10),
            "fragments": changed(superblock + 80, "<I", incompat | 0x20),
            "xattr prefixes": changed(superblock + 80, "<I", incompat | 0x40),
            "48bit": changed(superblock + 80, "<I", incompat | 0x80),
            "metabox": changed(superblock + 80, "<I", incompat | 0x100),
            "unknown compat": changed(superblock + 8, "<I", 0x80),
            "block size": changed(superblock + 12, "<B", 13),
            "packed inode": changed(superblock + 96, "<Q", 5),
        }
        root_nid = struct.unpack_from("<H", original, superblock + 14)[0]
        root = 32 * root_nid
        i_format = struct.unpack_from("<H", original, root)[0]
        refused["unknown inode format bit"] = changed(root, "<H", i_format | 0x20)
        chunked = bytearray(changed(root, "<H", (i_format & 1) | 4 << 1))
        struct.pack_into("<I", chunked, superblock + 80, incompat | 0x4)
        refused["chunk-based directory"] = bytes(chunked)
        refused["compressed directory"] = changed(root, "<H", (i_format & 1) | 3 << 1)
        for name, data in refused.items():
            with self.subTest(name=name), self.assertRaises(UnsupportedErofs):
                symlink_targets(data)
        for name, data in {"truncated": original[:8192], "empty": b"\0" * 4096}.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                symlink_targets(data)


if __name__ == "__main__":
    unittest.main()
