"""Signed, immutable environment images over a local HTTP registry.

``FakeOciRegistry`` speaks the Distribution API the production clients use:
manifests by tag or digest, tag lists, deletes, and blob uploads and reads
(with ranges). ``EnvironmentPublisher`` publishes a catalog image the way the
trusted builder does: each layer becomes a signed component, the components
a signed environment root, and the source OCI manifest is annotated with that
root. ``FakeEnvironmentBackend`` stands in for the root artifact-I/O backend
behind the production ``EnvironmentBackendServer``: ``ensure`` loads and
authenticates a component from the registry, checks its image digest and
unpacks it into a component directory, where the backend would attach an
EROFS block device. A component image is a deterministic tar, not EROFS.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import shutil
import tarfile
import threading
from typing import Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from sandboxes.environment_artifact import (
    OCI_IMAGE, EnvironmentArtifactRegistry, attach_environment_to_image, canonical_bytes, content_digest,
    publish_environment, sign_component,
)
from sandboxes.environment_backend import EnvironmentBackendServer
from sandboxes.environment_manifest import EnvironmentManifest
from sandboxes.image_rootfs import DockerImageConfig
from sandboxes.managed_registry import (
    RegistryClient, manifest_digest_from_image_ref, registry_host_from_image_ref,
    registry_repository_tag_from_image_ref,
)
from sandboxes.models import ENVIRONMENT_IO_METRICS

from .fake_mount import has_dependents

D9 = "sha256:" + "9" * 64
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"


class FakeOciRegistry(ThreadingHTTPServer):
    """Distribution's manifest and blob API: manifests by tag or digest, tag
    lists, protection-tag PUTs, DELETE of a digest with every tag on it, and
    blob uploads (monolithic or chunked) and reads (whole or by range).
    Blobs are shared by every repository."""

    daemon_threads = True

    def __init__(self):
        self.manifests, self.tags = {}, {}  # (repository, digest) -> bytes; (repository, tag) -> digest
        self.blobs, self.uploads = {}, {}  # digest -> bytes; upload id -> bytes so far
        self.guard = threading.Lock()
        super().__init__(("127.0.0.1", 0), FakeOciHandler)
        self.url = "http://%s:%d" % self.server_address
        self.host = urlparse(self.url).netloc
        # A short poll, so shutdown does not wait out the default 0.5 s.
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def push(self, repository, tag, config_digest, layers=((D9, 100),)):
        body = canonical_bytes({"schemaVersion": 2, "mediaType": OCI_IMAGE, "config": {"digest": config_digest,
                                "size": 2}, "layers": [{"digest": d, "size": s} for d, s in layers]})
        self.manifests[(repository, content_digest(body))] = body
        self.tags[(repository, tag)] = content_digest(body)
        return content_digest(body)

    def delete(self, repository, digest):
        self.manifests.pop((repository, digest))
        for key in [key for key, value in self.tags.items() if key[0] == repository and value == digest]:
            del self.tags[key]

    def close(self):
        self.shutdown()
        self.server_close()


class FakeOciHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body=b"", headers=()):
        self.send_response(status)
        for name, value in (("Content-Length", str(len(body))), *headers):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def manifest(self):
        match = re.fullmatch(r"/v2/(.+)/manifests/([^?]+)", self.path)
        repository, reference = unquote(match[1]), unquote(match[2])
        digest = reference if reference.startswith("sha256:") else self.server.tags.get((repository, reference))
        return repository, reference, digest, self.server.manifests.get((repository, digest))

    def upload(self):
        """(repository, upload id or None, query) of a blob upload path, or None."""
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/v2/(.+)/blobs/uploads/([0-9a-f]*)", parsed.path)
        if match is None:
            return None
        return unquote(match[1]), match[2] or None, parse_qs(parsed.query)

    def get_blob(self, digest):
        data = self.server.blobs.get(digest)
        if data is None:
            return self.reply(404, b'{"errors":[{"code":"BLOB_UNKNOWN"}]}')
        ranged = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range") or "")
        if ranged is None:
            return self.reply(200, data, (("Docker-Content-Digest", digest),))
        start, end = int(ranged[1]), min(int(ranged[2]), len(data) - 1)
        self.reply(206, data[start:end + 1], (("Content-Range", f"bytes {start}-{end}/{len(data)}"),))

    def do_GET(self):
        listing = re.fullmatch(r"/v2/(.+)/tags/list(\?.*)?", self.path)
        if listing:
            name = unquote(listing[1])
            tags = sorted(tag for repository, tag in self.server.tags if repository == name)
            return self.reply(200, json.dumps({"name": name, "tags": tags}).encode())
        blob = re.fullmatch(r"/v2/.+/blobs/(sha256:[0-9a-f]{64})", self.path)
        if blob:
            return self.get_blob(unquote(blob[1]))
        _repository, _reference, digest, body = self.manifest()
        if body is None:
            return self.reply(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
        self.reply(200, body, (("Docker-Content-Digest", digest), ("Content-Type", OCI_IMAGE)))

    do_HEAD = do_GET

    def do_POST(self):
        upload = self.upload()
        if upload is None or upload[1] is not None:
            return self.reply(404)
        self.body()
        upload_id = uuid.uuid4().hex
        with self.server.guard:
            self.server.uploads[upload_id] = b""
        self.reply(202, headers=(("Location", f"/v2/{upload[0]}/blobs/uploads/{upload_id}"),))

    def do_PATCH(self):
        repository, upload_id, _query = self.upload() or (None, None, None)
        chunk = self.body()
        with self.server.guard:
            if upload_id not in self.server.uploads:
                return self.reply(404, b'{"errors":[{"code":"BLOB_UPLOAD_UNKNOWN"}]}')
            self.server.uploads[upload_id] += chunk
        self.reply(202, headers=(("Location", f"/v2/{repository}/blobs/uploads/{upload_id}"),))

    def do_PUT(self):
        upload = self.upload()
        if upload is not None:
            return self.finish_upload(*upload)
        repository, tag, _digest, _body = self.manifest()
        body = self.body()
        self.server.manifests[(repository, content_digest(body))] = body
        self.server.tags[(repository, tag)] = content_digest(body)
        self.reply(201, headers=(("Docker-Content-Digest", content_digest(body)),))

    def finish_upload(self, _repository, upload_id, query):
        chunk = self.body()
        digest = (query.get("digest") or [""])[0]
        with self.server.guard:
            data = self.server.uploads.pop(upload_id, None)
            if data is None:
                return self.reply(404, b'{"errors":[{"code":"BLOB_UPLOAD_UNKNOWN"}]}')
            data += chunk
            if content_digest(data) != digest:
                return self.reply(400, b'{"errors":[{"code":"DIGEST_INVALID"}]}')
            self.server.blobs[digest] = data
        self.reply(201, headers=(("Docker-Content-Digest", digest),))

    def do_DELETE(self):
        upload = self.upload()
        if upload is not None:
            with self.server.guard:
                found = self.server.uploads.pop(upload[1], None) is not None
            return self.reply(204 if found else 404)
        repository, _reference, digest, body = self.manifest()
        if body is None:
            return self.reply(404)
        self.server.delete(repository, digest)
        self.reply(202)


def layer_archive(files: Mapping[str, bytes | str]) -> bytes:
    """A deterministic tar of ``files`` (paths relative to /; a trailing ``/``
    names an empty directory), with every parent directory."""
    entries: dict[str, bytes | None] = {}
    for path, data in files.items():
        name = path.strip("/")
        parts = name.split("/")
        for depth in range(1, len(parts)):
            entries.setdefault("/".join(parts[:depth]), None)
        entries[name] = None if path.endswith("/") else (data.encode("utf-8") if isinstance(data, str) else data)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            if data is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(info)
            else:
                info.mode, info.size = 0o644, len(data)
                archive.addfile(info, io.BytesIO(data))
    # A component image is a block device: whole 4 KiB blocks. Zero blocks
    # past the end of a tar are padding.
    return stream.getvalue().ljust(-(-len(stream.getvalue()) // 4096) * 4096, b"\0")


def unpack_layer(data: bytes, target: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive.getmembers():
            path = target / member.name
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise ValueError(f"component entry escapes its root: {member.name}")
            if member.isdir():
                path.mkdir(mode=member.mode, parents=True, exist_ok=True)
            elif member.isfile():
                path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(member).read())
                path.chmod(member.mode)
            else:
                raise ValueError(f"component entry type is not modeled: {member.name}")


class EnvironmentPublisher:
    """Publishes images as signed environments, as the trusted builder does."""

    repository = "harness/environments"

    def __init__(self, registry: FakeOciRegistry, root: Path) -> None:
        self.registry = registry
        self.root = root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.trusted_keys = {content_digest(public): public}
        self.client = RegistryClient(registry.url)
        self.environments = EnvironmentArtifactRegistry(self.client, self.repository, self.trusted_keys)
        self._guard = threading.Lock()

    def ref(self, name: str) -> str:
        """``name`` on this registry, unless it names a registry already."""
        return name if registry_host_from_image_ref(name) else f"{self.registry.host}/{name}"

    def _upload(self, repository: str, payload: bytes) -> str:
        digest = content_digest(payload)
        if not self.client.blob_exists(repository, digest):
            location = self.client.start_blob_upload(repository)
            self.client.finish_blob_upload(self.client.upload_blob_chunk(location, payload), digest)
        return digest

    def _component(self, archive: bytes, *, source_image: str) -> str:
        image = self.root / f"{content_digest(archive)[7:]}.tar"
        image.write_bytes(archive)
        component = sign_component(image, source_image=source_image, signing_key=self.key)
        tag = "c-" + content_digest(canonical_bytes(component.to_dict()))[7:]
        return self.environments.publish(image, component, tag=tag)

    def publish(self, ref: str, layers: Sequence[Mapping[str, bytes | str]], config: DockerImageConfig) -> str:
        """Publish ``layers`` (the base first, then toolkits stacked above it)
        as the signed environment of ``ref``; return its root digest."""
        if registry_host_from_image_ref(ref) != self.registry.host or manifest_digest_from_image_ref(ref):
            raise ValueError(f"environment images are tags on {self.registry.host}: {ref}")
        repository, tag = registry_repository_tag_from_image_ref(ref)
        archives = [layer_archive(files) for files in layers]
        inspection = {"Cmd": list(config.command), "Env": list(config.env), "WorkingDir": config.working_dir,
                      "User": config.user, **({"Entrypoint": list(config.entrypoint)} if config.entrypoint else {})}
        oci_config = canonical_bytes({"architecture": "amd64", "os": "linux", "config": inspection, "rootfs": {
            "type": "layers", "diff_ids": [content_digest(archive) for archive in archives]}})
        with self._guard:
            source_image = self._upload(repository, oci_config)
            # Toolkits carry their own source identity: their layer's digest.
            components = [self._component(archive, source_image=source_image if index == 0
                                          else content_digest(archive))
                          for index, archive in enumerate(archives)]
            root = publish_environment(
                self.environments, source_image=source_image,
                environment=EnvironmentManifest(base=components[0], toolkits=tuple(components[1:])),
                image_config=inspection, signing_key=self.key, tag="e-" + source_image[7:])
            self.client.put_manifest(repository, tag, canonical_bytes({
                "schemaVersion": 2, "mediaType": OCI_IMAGE, "layers": [],
                "config": {"mediaType": OCI_CONFIG, "digest": source_image, "size": len(oci_config)}}))
            attach_environment_to_image(self.environments, image_repository=repository, image_reference=tag,
                                        environment_digest=root)
        return root


class FakeEnvironmentBackend:
    """``EnvironmentBackend``'s RPC surface over component directories.

    A component directory stays until ``drop``, which refuses while an
    overlay in the node's fake mount table names it as a lower, as the
    backend refuses a component a mounted composition still reads.
    """

    def __init__(self, root: Path, registry: EnvironmentArtifactRegistry, mount_table: Path) -> None:
        root.mkdir(mode=0o700, exist_ok=True)
        self.mounts = root / "components"
        self.mounts.mkdir(mode=0o700, exist_ok=True)
        self.registry, self.mount_table = registry, mount_table
        self._guard = threading.Lock()
        self.attached: list[str] = []  # Component digests, in attach order.

    def ensure(self, digest: str) -> str:
        target = self.mounts / digest.removeprefix("sha256:")
        with self._guard:
            if not target.is_dir():
                component = self.registry.load(digest)  # Authenticated against the trusted keys.
                data = self.registry.client.blob_bytes(
                    self.registry.repository, component.image_digest, max_bytes=component.image_size)
                if content_digest(data) != component.image_digest:
                    raise ValueError("environment component image identity mismatch")
                staging = self.mounts / f".{target.name}.staging"
                shutil.rmtree(staging, ignore_errors=True)
                staging.mkdir(mode=0o755)
                unpack_layer(data, staging)
                staging.rename(target)
                self.attached.append(digest)
        return str(target)

    def drop(self, digest: str) -> bool:
        target = self.mounts / digest.removeprefix("sha256:")
        with self._guard:
            if not target.is_dir():
                return True
            if has_dependents(self.mount_table, target):
                return False
            shutil.rmtree(target)
            return True

    def _active(self) -> int:
        return sum(1 for path in self.mounts.iterdir() if not path.name.startswith("."))

    def metrics(self) -> dict:
        zero = {name: False if name == "prefetch_enabled" else 0.0 if name.endswith("_seconds") else 0
                for name in ENVIRONMENT_IO_METRICS}
        return zero | {"active_components": self._active()}

    def pressure(self) -> dict:
        return {"active_components": self._active(), "over_cache_budget": False}


class EnvironmentBackendService:
    """One node's backend, served on its unix socket; outlives agent restarts."""

    def __init__(self, socket_path: Path, backend: FakeEnvironmentBackend) -> None:
        self.socket_path, self.backend = socket_path, backend
        self.server = EnvironmentBackendServer(socket_path, backend)
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                                        daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self._thread.join(timeout=5)
        self.server.server_close()
