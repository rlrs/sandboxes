"""Real isolated OCI preparation against a local immutable-blob HTTP fixture."""
import hashlib
import os
import stat
import unittest


TEST_TIER = "contract"


REPOSITORY = "owned/isolation-fixture"


def snapshot(root):
    rows, links = {}, {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        name = str(path.relative_to(root))
        item = {"mode": info.st_mode, "uid": info.st_uid, "gid": info.st_gid,
                "mtime_ns": info.st_mtime_ns,
                "xattrs": {key: os.getxattr(path, key, follow_symlinks=False)
                           for key in sorted(os.listxattr(path, follow_symlinks=False))}}
        if stat.S_ISREG(info.st_mode):
            item["bytes"] = hashlib.sha256(path.read_bytes()).hexdigest()
            links.setdefault((info.st_dev, info.st_ino), []).append(name)
        elif stat.S_ISLNK(info.st_mode):
            item["link"] = os.readlink(path)
        rows[name] = item
    return rows, sorted(sorted(paths) for paths in links.values() if len(paths) > 1)


if __name__ == "__main__":
    unittest.main()
