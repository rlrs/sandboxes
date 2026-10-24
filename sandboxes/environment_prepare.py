"""Bounded exec-isolated preparation of one sandbox commit's filtered upper.

No signing key is sent through the protocol, and the child never publishes
registry data. Its output is a fixed file under a private parent-owned root.
This is performance isolation, not a security sandbox: the fresh interpreter
runs with the parent's UID and environment and can access the same filesystem.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit

from .commit_policy import (
    REFUSALS, CommitPolicy, CommitRefused, FilterResult, filter_upper, require_secret_digests,
)
from .environment_artifact import require_digest
from .managed_registry import RegistryClient

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 4096
RESULT_NAME = "preparation-result.json"
# Schema 1 was the retired layer-group request; the child refuses it.
COMMIT_REQUEST_SCHEMA = 2
FILTERED_NAME = "filtered.tar"


class PreparationError(RuntimeError):
    """Preparation failed without permitting publication or Docker fallback."""


def _private_root(value, *, empty):
    if not isinstance(value, str) or len(value) > 4096 or "\0" in value:
        raise ValueError("invalid preparation root")
    root = Path(value)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError("preparation root must be canonical and absolute")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("preparation root must be a private owned directory")
    if empty and any(root.iterdir()):
        raise ValueError("preparation root must be empty")
    return root


_REGISTRY_FIELDS = {"schema", "root", "registry_url", "registry_timeout_seconds", "repository"}


def _validate_commit_request(value):
    """``commit-upper`` (C3.1): fetch one staged upper, verify it, and filter it."""
    if (not isinstance(value, dict) or set(value) != _REGISTRY_FIELDS | {"blob", "policy", "secret_digests"}
            or value["schema"] != COMMIT_REQUEST_SCHEMA or not isinstance(value["blob"], dict)
            or set(value["blob"]) != {"digest", "size"} or type(value["blob"]["size"]) is not int
            or not 0 < value["blob"]["size"] <= 1 << 40 or not isinstance(value["secret_digests"], list)):
        raise ValueError("invalid commit preparation request")
    _validate_registry(value)
    require_digest(value["blob"]["digest"])
    CommitPolicy.from_dict(value["policy"])
    require_secret_digests(value["secret_digests"])
    return _private_root(value["root"], empty=True)


def _validate_registry(value):
    url = value["registry_url"]
    if not isinstance(url, str) or len(url) > 2048 or any(c in url for c in "\0\r\n"):
        raise ValueError("invalid preparation registry")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("invalid preparation registry")
    timeout = value["registry_timeout_seconds"]
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("invalid preparation registry timeout")
    repository = value["repository"]
    if (not isinstance(repository, str) or len(repository) > 1024
            or re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", repository) is None):
        raise ValueError("invalid preparation repository")


def _result_value(root):
    descriptor = os.open(root / RESULT_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("invalid preparation result file")
        raw = stream.read(MAX_RESULT_BYTES + 1)
    if len(raw) > MAX_RESULT_BYTES:
        raise ValueError("preparation result exceeds its bound")
    return json.loads(raw)


def _spawn(data, timeout_seconds):
    # stdout/stderr cannot leak payloads or accumulate unbounded data. The
    # bounded result file is the only response channel; stdin carries no key.
    with subprocess.Popen((sys.executable, "-m", __name__), stdin=subprocess.PIPE,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                          cwd=Path(__file__).resolve().parent.parent) as process:
        try:
            process.communicate(data, timeout=timeout_seconds)
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        if process.returncode != 0:
            raise PreparationError("preparation child failed")


def _private_scratch(root):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("preparation root cannot be a symlink")
    # A configured work-root ancestor may legitimately be a storage symlink;
    # canonicalize it while retaining the real, private scratch-root check.
    return root.resolve(strict=True)


def commit_upper(client, repository, blob, policy, secret_digests, root):
    """Fetch and verify the staged upper, then filter it into ``root/filtered.tar``."""
    upper, digest, size = root / "upper.tar", hashlib.sha256(), 0
    response = client.open_blob(repository, blob["digest"])
    try:
        with upper.open("xb") as stream:
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > blob["size"]:
                    raise ValueError("staged commit upper exceeds its recorded size")
                digest.update(chunk)
                stream.write(chunk)
    finally:
        response.close()
    try:
        if size != blob["size"] or "sha256:" + digest.hexdigest() != blob["digest"]:
            raise ValueError("staged commit upper does not match its digest")
        with upper.open("rb") as source, (root / FILTERED_NAME).open("xb") as destination:
            result = filter_upper(source, destination, policy, secret_digests)
    except CommitRefused as refused:
        (root / FILTERED_NAME).unlink(missing_ok=True)
        return {"status": "refused", "error_code": refused.code}
    finally:
        upper.unlink(missing_ok=True)
    return {"status": "ok", "commit": result.to_dict()}


def commit_result(value, root) -> FilterResult:
    """A refusal raises CommitRefused; ``root/filtered.tar`` holds the accepted layer."""
    if (isinstance(value, dict) and set(value) == {"status", "error_code"} and value["status"] == "refused"
            and value["error_code"] in REFUSALS):
        raise CommitRefused(value["error_code"], "the commit policy refused the exported upper")
    if not isinstance(value, dict) or set(value) != {"status", "commit"} or value["status"] != "ok":
        raise ValueError("commit preparation child failed")
    result = FilterResult.from_dict(value["commit"])
    info = (root / FILTERED_NAME).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != result.size:
        raise ValueError("invalid filtered commit layer")
    return result


def prepare_commit_in_subprocess(client, commit, root, *, timeout_seconds=600) -> FilterResult:
    """Fetch, verify and filter one commit upper in a fresh keyless interpreter."""
    value = {"schema": COMMIT_REQUEST_SCHEMA, "root": str(_private_scratch(root)), "registry_url": client.base_url,
             "registry_timeout_seconds": client.timeout_seconds, "repository": commit.repository,
             "blob": {"digest": commit.blob_digest, "size": commit.blob_size},
             "policy": commit.policy.to_dict(), "secret_digests": list(commit.secret_digests)}
    root = _validate_commit_request(value)
    try:
        _spawn(json.dumps(value, separators=(",", ":"), allow_nan=False).encode(), timeout_seconds)
        return commit_result(_result_value(root), root)
    except CommitRefused:
        raise
    except subprocess.TimeoutExpired:
        raise PreparationError("commit preparation child timed out") from None
    except (OSError, ValueError):
        raise PreparationError("invalid commit preparation result") from None


def main():
    root = None
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            return 2
        value = json.loads(raw)
        root = _validate_commit_request(value)
        result = commit_upper(
            RegistryClient(value["registry_url"], timeout_seconds=value["registry_timeout_seconds"]),
            value["repository"], value["blob"], CommitPolicy.from_dict(value["policy"]),
            value["secret_digests"], root)
    except Exception:
        # Avoid serializing exception text: registry failures may contain URLs
        # or response payloads. Unexpected failures are terminal for the parent.
        result = {"status": "error"}
    if root is None:
        return 2
    try:
        descriptor = os.open(root / RESULT_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(result, stream, separators=(",", ":"), allow_nan=False)
    except Exception:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
