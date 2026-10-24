"""Build immutable components from a fresh allowlisted view of OCI image input.

The only public input is the existing immutable Docker image adapter. No runtime
workspace, memory directory, or checkpoint is accepted as a publication source.
"""
from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import time
from tempfile import TemporaryDirectory

from .direct_warden import DirectWardenError
from .environment_artifact import OCI_IMAGE, canonical_bytes, content_digest, sign_component
from .image_rootfs import DockerOverlay2RootfsStore
from .managed_registry import RegistryRequestError
from .build_deadline import (
    ImageBuildTimeoutError, build_execution_deadline,
    remaining_build_execution_seconds, without_build_execution_deadline,
)

_LOG = logging.getLogger(__name__)
_PUBLICATION_METRICS = ContextVar("environment_publication_metrics", default=None)


@contextmanager
def publication_metrics():
    """Collect per-publication measurements without mixing concurrent builds."""
    current = _PUBLICATION_METRICS.get()
    if current is not None:
        yield current
        return
    values = {}
    token = _PUBLICATION_METRICS.set(values)
    try:
        yield values
    finally:
        _PUBLICATION_METRICS.reset(token)


def _measure(name, value=1):
    values = _PUBLICATION_METRICS.get()
    if values is not None:
        values[name] = values.get(name, 0) + value


@contextmanager
def _phase(name):
    started = time.monotonic()
    try:
        yield
    finally:
        _measure(name + "_ms", round((time.monotonic() - started) * 1000, 3))


def _copy_metadata(source, destination, info):
    # Ownership first: chown(2) clears security.capability and setuid bits,
    # so file capabilities and the mode are applied after it.
    os.chown(destination, info.st_uid, info.st_gid, follow_symlinks=False)
    if not stat.S_ISLNK(info.st_mode):
        os.chmod(destination, stat.S_IMODE(info.st_mode))
    if hasattr(os, "listxattr"):
        for name in os.listxattr(source, follow_symlinks=False):
            os.setxattr(destination, name, os.getxattr(source, name, follow_symlinks=False),
                        follow_symlinks=False)
    os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)


def _copy_entry(source, destination, hardlinks):
    remaining_build_execution_seconds()
    info = source.lstat()
    if stat.S_ISDIR(info.st_mode):
        destination.mkdir(mode=0o700)
        for child in sorted(source.iterdir(), key=lambda path: path.name):
            # This is a mounted immutable filesystem, not an OCI layer tar.
            # A literal .wh.* filename is ordinary user data. Real filesystem
            # whiteout devices and opaque xattrs retain their exact semantics.
            _copy_entry(child, destination / child.name, hardlinks)
    elif stat.S_ISREG(info.st_mode):
        identity = (info.st_dev, info.st_ino)
        previous = hardlinks.get(identity)
        if previous is not None:
            os.link(previous, destination)
        else:
            # Input is an immutable image lease; NOFOLLOW also fences a replaced
            # final path and the post-read stat rejects a changing builder view.
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as reader, destination.open("xb") as writer:
                current = os.fstat(reader.fileno())
                if (current.st_dev, current.st_ino) != identity:
                    raise ValueError("immutable build input changed")
                while chunk := reader.read(1024 * 1024):
                    remaining_build_execution_seconds()
                    writer.write(chunk)
                after = os.fstat(reader.fileno())
                if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    raise ValueError("immutable build input changed while copying")
            hardlinks[identity] = destination
    elif stat.S_ISLNK(info.st_mode):
        destination.symlink_to(os.readlink(source))
    elif stat.S_ISCHR(info.st_mode) and info.st_rdev == os.makedev(0, 0):
        os.mknod(destination, stat.S_IFCHR | 0o600, info.st_rdev)
    else:
        raise ValueError("environment build view contains an unsupported special file")
    _copy_metadata(source, destination, info)


# Mounted by the sandbox runtime; Docker images carry them empty.
WHOLE_IMAGE_EXCLUDED = frozenset({"dev", "proc", "sys", "run"})


def expand_allowlist(source_root: Path, paths) -> list:
    """Expand ``*`` to every top-level image entry except runtime mounts.

    Arbitrary imported and task images keep content in places a fixed list
    cannot know (``/testbed``, ``/app``, ``/opt/conda``), and a listed path
    that an image lacks is an error.
    """
    expanded = []
    for value in paths:
        if value == "*":
            expanded.extend(sorted(
                entry.name for entry in source_root.iterdir()
                if entry.name not in WHOLE_IMAGE_EXCLUDED
            ))
        else:
            expanded.append(value)
    return expanded


def _whole_image(allowlist) -> bool:
    values = list(allowlist)
    return bool(values) and all(value == "*" for value in values)


def allowlisted_build_view(source_root: Path, destination: Path, paths):
    """Copy declared immutable image paths, preserving merged-layer semantics."""
    if source_root.is_symlink() or not source_root.is_dir() or destination.exists():
        raise ValueError("fresh environment view requires a real source and new destination")
    allowed = []
    for value in expand_allowlist(source_root, paths):
        if not isinstance(value, str):
            raise ValueError("environment allowlist paths must be strings")
        path = PurePosixPath(value)
        if (path.is_absolute() or not path.parts
                or any(part in {".", ".."} for part in path.parts) or "\0" in value):
            raise ValueError("environment allowlist paths must stay within the immutable image")
        if path not in allowed:
            allowed.append(path)
    if not allowed:
        raise ValueError("an explicit environment publication allowlist is required")
    allowed = sorted(path for path in allowed if not any(parent in allowed for parent in path.parents))
    destination.mkdir(mode=0o700)
    hardlinks = {}
    for path in allowed:
        source = source_root
        target = destination
        for part in path.parts[:-1]:
            source /= part
            target /= part
            if source.is_symlink() or not source.is_dir():
                raise ValueError("allowlist traverses a non-directory or symlink")
            target.mkdir(mode=0o700, exist_ok=True)
        source /= path.name
        if not source.exists() and not source.is_symlink():
            raise ValueError("allowlisted build path is absent")
        _copy_entry(source, target / path.name, hardlinks)
    # Parents synthesized for narrow allowlist entries also retain source
    # ownership/mode/xattrs; do this last so readonly directories remain writable
    # while the fresh view is being assembled.
    for path in sorted(destination.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        original = source_root / path.relative_to(destination)
        if path.is_dir() and not path.is_symlink() and original.is_dir():
            _copy_metadata(original, path, original.lstat())
    _copy_metadata(source_root, destination, source_root.lstat())


def normalize_directory_times(root: Path):
    """Set the directory and whiteout times of a builder-owned view to 0.

    Layout 2 keeps file and symlink mtimes. Directory and whiteout times record
    when the view was assembled instead, so equal inputs would build different
    bytes; no .pyc check reads them. Never apply this to a Docker merged view,
    which the builder borrows.
    """
    pending = [os.fspath(root)]
    while pending:
        remaining_build_execution_seconds()
        path = pending.pop()
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif not entry.is_symlink() and not entry.is_file(follow_symlinks=False):
                    os.utime(entry.path, ns=(0, 0), follow_symlinks=False)
        os.utime(path, ns=(0, 0), follow_symlinks=False)


@dataclass
class FreshEnvironmentBuilder:
    image_store: DockerOverlay2RootfsStore
    registry: object
    signing_key: object
    work_root: Path
    mkfs_erofs: str = "mkfs.erofs"
    # lz4 made SWE-bench images 38% smaller with a faster mkfs; workers read
    # fewer bytes per chunk and the kernel decompresses (docs/images.md).
    compression: str = "lz4"
    # Commit preparation runs in a fresh keyless interpreter, off the GIL of
    # concurrent build threads. Test adapters retain the local path.
    preparation_subprocess: bool = False
    release_published_tag: bool = False
    # Layout 2 (-T 0 --mkfs-time --MZ, erofs-utils 1.9+). Enable only after
    # every worker and gateway accepts layout-2 layer formats.
    preserve_mtimes: bool = False
    _layer_format: dict | None = field(default=None, init=False, repr=False)

    def build(self, image_ref, *, allowlist, tag):
        if not isinstance(self.image_store, DockerOverlay2RootfsStore):
            raise ValueError("environment publication requires the immutable OCI build adapter")
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        image_id = None
        try:
            with self.image_store.operation_lease(image_ref) as source:
                image_id = source.image_id
                with TemporaryDirectory(dir=self.work_root) as temporary:
                    root = Path(temporary)
                    image = root / "component.erofs"
                    if _whole_image(allowlist):
                        # The merged overlay already is the image; copying every
                        # file into a fresh view only repeated it (13 s for 2.6 GB,
                        # serialized on the GIL across concurrent publications).
                        if source.rootfs.is_symlink() or not source.rootfs.is_dir():
                            raise ValueError("environment publication requires a real source")
                        view, exclude = source.rootfs, True
                    else:
                        view, exclude = root / "view", False
                        allowlisted_build_view(source.rootfs, view, allowlist)
                        if self.preserve_mtimes:
                            normalize_directory_times(view)
                    # A v1 component records no format. Every EROFS reader
                    # mounts both layouts (no feature bit), so it follows the switch.
                    self._mkfs(image, view, exclude_runtime_mounts=exclude, preserve_mtimes=self.preserve_mtimes)
                    component = sign_component(image, source_image=source.image_id, signing_key=self.signing_key)
                    digest = self._publish_component(image, component, tag)
                    return {"image_id": source.image_id, "component_digest": digest,
                            "component": component, "image_config": source.image_config}
        finally:
            if image_id is not None:
                # Builders have no sandbox registry users of this temporary
                # mount. Existing image leases fence concurrent build readers.
                self._collect_image(image_id)

    def _collect_image(self, image_id):
        # Another publisher may still hold a lease on the same image. Cleanup
        # cannot extend an expired build indefinitely; ordinary image GC can
        # reclaim a cache entry whose readers have not drained in this budget.
        try:
            with without_build_execution_deadline(), build_execution_deadline(10):
                self.image_store.collect_image(image_id, is_referenced=lambda _: False)
        except (ImageBuildTimeoutError, subprocess.TimeoutExpired):
            _LOG.warning("temporary builder image cleanup deferred after its deadline")

    def _publish_component(self, image, component, tag):
        """Publish with a signed metadata prefetch hint when the walker qualifies it.

        Unqualified or non-EROFS bytes publish exactly the hint-free manifest.
        """
        from .environment_metadata import sign_metadata_hint
        hint = None
        try:
            with _phase("metadata_hint"):
                hint, metadata = sign_metadata_hint(image, component, self.signing_key,
                                                    check=remaining_build_execution_seconds)
        except (ValueError, OSError) as exc:
            _measure("metadata_hint_unsupported")
            _LOG.info("publishing environment component without a metadata hint: %s", exc)
        else:
            _measure("metadata_hints")
            _measure("metadata_hint_chunks", len(hint.chunks))
            _measure("metadata_hint_bytes", metadata.metadata_bytes)
        if hint is None:
            return self.registry.publish(image, component, tag=tag)
        return self.registry.publish(image, component, tag=tag, metadata=hint)

    def _mkfs(self, image, view, *, exclude_runtime_mounts, preserve_mtimes, tar=False):
        # -T 0 alone (erofs-utils' default --all-time) sets every mtime to 0.
        # Layout 2: --mkfs-time fixes only the build time and keeps per-file
        # mtimes; --MZ packs inodes and directories into one metadata zone, so
        # hints span few chunks. Neither sets a feature bit. mkfs stages the
        # zone in an unlinked $TMPDIR file, so it lives in this build's directory.
        options = ["-T", "0", *(["--mkfs-time", "--MZ"] if preserve_mtimes else []),
                   "-U", "00000000-0000-0000-0000-000000000000"]
        if self.compression:
            options.append("-z" + self.compression)
        if exclude_runtime_mounts:
            options.append("--exclude-regex=^(" + "|".join(sorted(WHOLE_IMAGE_EXCLUDED)) + ")$")
        if tar:
            # A filtered commit layer (erofs-utils 1.8+): --aufs turns its OCI
            # whiteouts into the overlayfs ones every other lower carries.
            options += ["--tar=f", "--aufs"]
        subprocess.run((self.mkfs_erofs, *options, str(image), str(view)), check=True, capture_output=True,
                       env={**os.environ, "TMPDIR": str(image.parent)}, timeout=remaining_build_execution_seconds(600))

    def layer_format(self):
        """Everything besides the layers that decides a layer component's bytes."""
        if self._layer_format is None:
            result = subprocess.run((self.mkfs_erofs, "-V"), check=True, capture_output=True,
                                    text=True, timeout=remaining_build_execution_seconds(60))
            lines = (result.stdout.strip() or result.stderr.strip()).splitlines()
            if not lines:
                raise ValueError("mkfs.erofs reported no version")
            self._layer_format = {"layout": 2 if self.preserve_mtimes else 1, "mkfs": lines[0].strip(),
                                  "compression": self.compression or "", "excludes": sorted(WHOLE_IMAGE_EXCLUDED)}
        return self._layer_format

    def publish_image(self, image_ref, *, allowlist):
        with publication_metrics() as metrics:
            try:
                with _phase("total"):
                    return self._publish_image(image_ref, allowlist=allowlist)
            finally:
                if self.release_published_tag:
                    # The registry owns the durable artifact. Docker was only
                    # a temporary extraction cache; rootfs collection releases
                    # its pin but does not remove the pulled publication tag.
                    # Remove just this tag, without force: other tags, reader
                    # pins and containers continue to protect shared layers.
                    try:
                        with without_build_execution_deadline(), build_execution_deadline(30):
                            self.image_store._checked(self.image_store.docker_binary,
                                "image", "rm", image_ref, timeout=30)
                    except (DirectWardenError, OSError, subprocess.SubprocessError, ImageBuildTimeoutError):
                        _LOG.warning("temporary publication tag cleanup deferred: %s", image_ref)
                _LOG.info("environment publication metrics %s", json.dumps(metrics, sort_keys=True))

    def _publish_image(self, image_ref, *, allowlist):
        """Publish fresh build output and attach it to the existing image tag."""
        import uuid
        from .environment_artifact import attach_environment_to_image, publish_environment
        from .environment_manifest import EnvironmentManifest
        from .managed_registry import registry_repository_tag_from_image_ref
        coordinates = registry_repository_tag_from_image_ref(image_ref)
        if coordinates is None or "@" in image_ref:
            raise ValueError("environment publication requires an owned image tag")
        repository, tag = coordinates
        with _phase("docker_pull"):
            self.image_store._checked(self.image_store.docker_binary, "pull", image_ref, timeout=600)
        result = self.build(image_ref, allowlist=tuple(allowlist), tag="environment-component-" + uuid.uuid4().hex)
        manifest = EnvironmentManifest(result["component_digest"])
        config = result["image_config"]
        environment_digest = publish_environment(self.registry,
            source_image=result["image_id"], environment=manifest,
            image_config={"Entrypoint": list(config.entrypoint), "Cmd": list(config.command), "Env": list(config.env),
                          "WorkingDir": config.working_dir, "User": config.user},
            signing_key=self.signing_key, tag="environment-root-" + uuid.uuid4().hex)
        return attach_environment_to_image(self.registry, image_repository=repository, image_reference=tag,
                                           environment_digest=environment_digest)

    def publish_commit(self, commit, *, image_ref):
        """C3.1: publish one sandbox commit as the topmost component of its parent.

        The keyless child fetches, verifies and filters the staged upper; this
        process hashes the filtered tar itself before mkfs and signing. In
        order: the EROFS component, the gzip OCI layer (the durable truth), the
        signed root and last the annotated manifest under ``image_ref``. Every
        step is content addressed, so a re-run publishes the same image.
        """
        from .commit_policy import COMMIT_ANNOTATION, CommitRefused, canonical
        from .environment_artifact import (
            ENVIRONMENT_ANNOTATION, MAX_COMMIT_DEPTH, CommitEnvironmentComponent, _upload_blob,
            load_image_environment, publish_environment, sign_commit_component,
        )
        from .environment_manifest import EnvironmentManifest
        from .environment_prepare import FILTERED_NAME
        from .managed_registry import manifest_digest_from_image_ref, registry_repository_tag_from_image_ref
        target = registry_repository_tag_from_image_ref(image_ref)
        source = registry_repository_tag_from_image_ref(commit.parent_image)
        parent_digest = manifest_digest_from_image_ref(commit.parent_image)
        if target is None or "@" in image_ref or source is None or not parent_digest:
            raise ValueError("commit publication requires an owned tag and a pinned parent image")
        (repository, tag), parent_repository, client = target, source[0], self.registry.client
        try:
            attachment = load_image_environment(self.registry, parent_repository, parent_digest, required=False)
        except RegistryRequestError as exc:
            if exc.status_code != 404:
                raise
            # Chunk store M2 deletes a released image's OCI (plan §5.4); a commit extends OCI layers.
            raise CommitRefused("commit_parent_released", "the parent image's OCI manifest was released") from exc
        if attachment is None:
            raise CommitRefused("commit_requires_environment_root", "the parent image has no signed root")
        root, parent = attachment
        if root != commit.parent_root:
            raise ValueError("the parent image does not carry the committed parent root")
        if len(parent.components) >= 33 or sum(isinstance(self.registry.load(digest), CommitEnvironmentComponent)
                                               for digest in parent.components) >= MAX_COMMIT_DEPTH:
            raise CommitRefused("commit_chain_too_deep", "the parent carries the deepest commit chain")
        document, _ = client.manifest_document(parent_repository, parent_digest)
        descriptor, media_type = document.get("config"), document.get("mediaType")
        docker = media_type == "application/vnd.docker.distribution.manifest.v2+json"
        if (not docker and media_type != OCI_IMAGE or not isinstance(descriptor, dict)
                or type(descriptor.get("size")) is not int or not isinstance(document.get("layers"), list)):
            raise ValueError("the parent image is not one OCI or Docker image manifest")
        parent_config = client.blob_bytes(parent_repository, descriptor["digest"], max_bytes=descriptor["size"])
        layer_format = self.layer_format()
        key = hashlib.sha256(canonical({"format": layer_format, "parent_root": root, "blob": commit.blob_digest,
                                        "policy": commit.policy.sha256})).hexdigest()
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with TemporaryDirectory(dir=self.work_root) as temporary:
            scratch = Path(temporary)
            (scratch / "prepare").mkdir(mode=0o700)
            with _phase("commit_filter"):
                result = self._prepare_commit(commit, scratch / "prepare")
            filtered, layer, image = scratch / "prepare" / FILTERED_NAME, scratch / "layer.tar.gz", scratch / "c.erofs"
            with _phase("commit_compress"):
                diff_id, layer_digest, layer_size = _gzip_layer(filtered, layer)
            if diff_id != result.diff_id:
                raise ValueError("the filtered commit layer changed after preparation")
            with _phase("mkfs"):
                self._mkfs(image, filtered, exclude_runtime_mounts=False,
                           preserve_mtimes=layer_format["layout"] == 2, tar=True)
            with _phase("sign"):
                component = sign_commit_component(image, diff_id=diff_id, parent_root=root,
                    policy_sha256=commit.policy.sha256, layer_format=layer_format, signing_key=self.signing_key)
            with _phase("publish_component"):
                component_digest = self._publish_component(image, component, "commit-" + key)
            if not client.blob_exists(repository, layer_digest):
                client.upload_blob_file(repository, layer, layer_digest, layer_size)
        if parent_repository != repository:
            for item in document["layers"]:
                if (not client.blob_exists(repository, item["digest"])
                        and not client.mount_blob(repository, parent_repository, item["digest"])):
                    raise ValueError("the registry declined a cross-repository layer mount")
        config = json.loads(parent_config)
        config["rootfs"]["diff_ids"].append(diff_id)
        config["history"] = [*config.get("history", ()), {"created_by": "ucloud-sandboxes commit",
                                                         "comment": commit.image_id}]
        config_bytes = canonical_bytes(config)
        config_digest = content_digest(config_bytes)
        _upload_blob(client, repository, config_bytes, config_digest)
        environment_digest = publish_environment(
            self.registry, source_image=config_digest, environment=EnvironmentManifest(
                parent.environment.base, parent.environment.workspace,
                (*parent.environment.toolkits, component_digest)),
            image_config=parent.image_config, signing_key=self.signing_key, tag="commit-root-" + key,
            source_diff_ids=config["rootfs"]["diff_ids"], parent_root=root, parent_config=parent_config)
        provenance = {"schema": "ucloud-commit-provenance-v1", "parent_root": root, "diff_id": diff_id,
                      "policy_sha256": commit.policy.sha256, **{name: getattr(commit, name) for name in (
                          "sandbox_id", "generation", "operation_id", "image_id", "parent_image")}}
        manifest = canonical_bytes({
            "schemaVersion": 2, "mediaType": media_type,
            "config": {"mediaType": descriptor["mediaType"], "digest": config_digest, "size": len(config_bytes)},
            "layers": [*document["layers"], {"digest": layer_digest, "size": layer_size, "mediaType": (
                "application/vnd.docker.image.rootfs.diff.tar.gzip" if docker
                else "application/vnd.oci.image.layer.v1.tar+gzip")}],
            "annotations": {ENVIRONMENT_ANNOTATION: environment_digest,
                            COMMIT_ANNOTATION: canonical(provenance).decode("ascii")}})
        client.put_manifest(repository, tag, manifest, media_type=media_type)
        return {"manifest_digest": content_digest(manifest), "environment_root": environment_digest,
                "component_digest": component_digest, "diff_id": diff_id, "layer_digest": layer_digest,
                "members": result.members, "drops": result.drops,
                "bytes": {"exported": commit.blob_size, "filtered": result.size,
                          "erofs": component.image_size, "oci": layer_size}}

    def _prepare_commit(self, commit, root):
        from .environment_prepare import commit_result, commit_upper, prepare_commit_in_subprocess
        if self.preparation_subprocess:
            return prepare_commit_in_subprocess(self.registry.client, commit, root,
                                                timeout_seconds=remaining_build_execution_seconds(600))
        blob = {"digest": commit.blob_digest, "size": commit.blob_size}
        return commit_result(commit_upper(self.registry.client, commit.repository, blob, commit.policy,
                                          commit.secret_digests, root), root)


def _gzip_layer(source: Path, destination: Path):
    """(diff ID, gzip digest, gzip size); a fixed header keeps equal tars equal."""
    from .commit_policy import DigestWriter
    diff = hashlib.sha256()
    with source.open("rb") as reader, destination.open("xb") as stream:
        compressed = DigestWriter(stream)
        with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, compresslevel=6, mtime=0) as writer:
            while chunk := reader.read(1 << 20):
                remaining_build_execution_seconds()
                diff.update(chunk)
                writer.write(chunk)
    return "sha256:" + diff.hexdigest(), compressed.digest, compressed.size


def main(argv=None):
    """Publish one canary through the same builder implementation as image builds."""
    import argparse
    from .environment_config import add_environment_registry_args
    parser = argparse.ArgumentParser(description="Publish a fresh allowlisted immutable image artifact")
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--docker-binary", default="docker")
    parser.add_argument("--environment-signing-key", type=Path, required=True)
    parser.add_argument("--environment-allow-path", action="append", default=[])
    parser.add_argument("--environment-preserve-mtimes", action="store_true")
    add_environment_registry_args(parser)
    args = parser.parse_args(argv)
    return publish_from_args(args)


def publish_from_args(args):
    """``publish-environment``: one allowlisted EROFS component and its signed root.

    Toolkits are published this way; builders convert builds into the chunk
    store instead (environment_config.environment_publisher_from_args).
    """
    from .environment_artifact import load_image_environment
    from .environment_config import environment_registry_from_args, load_signing_key
    from .managed_registry import registry_repository_tag_from_image_ref
    registry = environment_registry_from_args(args)
    allowlist = tuple(args.environment_allow_path or ())
    if registry is None or not allowlist:
        raise ValueError("publish-environment requires registry trust and an explicit immutable path allowlist")
    root = args.state_root.absolute() / "environment-build"
    builder = FreshEnvironmentBuilder(DockerOverlay2RootfsStore(root / "images", docker_binary=args.docker_binary),
                                      registry, load_signing_key(args.environment_signing_key), root / "scratch",
                                      preparation_subprocess=True, release_published_tag=True,
                                      preserve_mtimes=args.environment_preserve_mtimes)
    digest = builder.publish_image(args.image_ref, allowlist=allowlist)
    repository, _ = registry_repository_tag_from_image_ref(args.image_ref)
    root, environment = load_image_environment(registry, repository, digest)
    print(json.dumps({"image_ref": args.image_ref + "@" + digest, "manifest_digest": digest,
                      "environment_root": root, "components": environment.components}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
