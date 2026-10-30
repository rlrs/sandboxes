"""The gateway half of sandbox builds (docs/images.md).

``job(payload)`` decides, for one recipe build, whether a sandbox builds it:
the prepared catalog must split it into a base (a foundation or prepared
source) and a remainder, the base must dispatch a whole-image chunk-store
root, and the remainder must plan (``sandbox_build.plan_build``). Anything
else goes to the builders unchanged. A job is spooled for
``serve-sandbox-builds``; ``adopt`` records a finished image as born in the
chunk store: an ``image_roots`` row ``released`` from the start (wave
``sandbox-build``) and a gateway image record naming its born identity.
``cancel`` stops a queued or running build through the spool (``Spool.cancel``).
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import io
import logging
import tarfile
import uuid

from ..images import IMAGE_ID_RE
from ..sandbox_build import ContextTree, Spool, Unsupported, context_tar, plan_build

_LOG = logging.getLogger(__name__)
WAVE = "sandbox-build"
BUILD_PREFIX = "sandbox:"


class SandboxBuilds:
    def __init__(self, spool, *, catalog, contexts, roots, environments, tag_for, apt_proxy=None):
        self.spool, self.catalog, self.contexts, self.roots = spool, catalog, contexts, roots
        self.environments, self.tag_for, self.apt_proxy = environments, tag_for, apt_proxy

    def job(self, payload):
        """The spool job building ``payload`` (a recipe's /v1/images/build
        payload) in a sandbox, or None with the reason the builders take it."""
        from ..environment_artifact import RafsEnvironmentComponent, load_environment
        from ..managed_registry import manifest_digest_from_image_ref, registry_repository_tag_from_image_ref
        from ..prepared_images import MAX_TEXT_BYTES, read_context
        if payload.get("build_args") or payload.get("dockerfile", "Dockerfile") != "Dockerfile":
            return None, "build arguments or another Dockerfile"
        digest = payload["context_archive_digest"]
        try:
            members, files = read_context(self.contexts, digest)
        except (ValueError, OSError, EOFError) as exc:
            return None, f"context: {exc}"
        if files.get(".dockerignore") or len(files.get("Dockerfile", b"")) > MAX_TEXT_BYTES:
            return None, "a .dockerignore or a large Dockerfile"
        try:
            decision = self.catalog.choose(files.get("Dockerfile", b"").decode(), files)
        except (ValueError, UnicodeError) as exc:
            return None, f"no prepared base: {exc}"
        if not decision or decision.get("kind") not in ("foundation", "source"):
            return None, "no prepared base"
        reference = decision["reference"]
        coordinates, base_digest = registry_repository_tag_from_image_ref(reference), \
            manifest_digest_from_image_ref(reference)
        root = self.roots.dispatch_root(coordinates[0], base_digest) if coordinates and base_digest else None
        if root is None:
            return None, "the base has no chunk-store root"
        environment = load_environment(self.environments, root)
        base = self.environments.load(environment.environment.base)
        if (environment.environment.workspace is not None or environment.environment.toolkits
                or not isinstance(base, RafsEnvironmentComponent) or base.format["layout"] != "image"):
            return None, "the base is not one whole-image chunk-store root"
        with self.contexts.open(digest) as stream:
            plain = context_tar(stream.read(), {name: value.encode() for name, value in decision["files"].items()})
        try:
            plan_build(decision["dockerfile"], ContextTree.of(tarfile.open(fileobj=io.BytesIO(plain)).getmembers()),
                       environment.image_config, apt_proxy=self.apt_proxy)
        except Unsupported as exc:
            return None, str(exc)
        tag = self.tag_for(payload["id"])
        return {"image_id": payload["id"], "build_id": f"{BUILD_PREFIX}{payload['id']}:{uuid.uuid4().hex[:12]}",
                "repository": registry_repository_tag_from_image_ref(tag)[0], "tag": tag,
                "dockerfile": decision["dockerfile"], "context_tar": base64.b64encode(plain).decode(),
                "base_config": environment.image_config, "base_reference": reference, "parent_root": root,
                "base_kind": decision["kind"], "submitted": datetime.now(timezone.utc).isoformat()}, ""

    def submit(self, payload):
        """A build id once spooled, else None (the builders build it)."""
        job, reason = self.job(payload)
        if job is None:
            _LOG.info("image %s builds on a builder: %s", payload.get("id"), reason)
            return None
        self.spool.submit(job)
        return job["build_id"]

    def status(self, build_id):
        """(status, build) as RecipeEnsurer's build_status answers: None when lost."""
        image_id = _image_id(build_id)
        status, result = self.spool.status(image_id, build_id) if image_id else (None, None)
        if status == "running":
            return "running", None
        if status is None:
            return None, None
        return status, {**result, "sandbox_build": True}

    def record(self, build_id):
        """The build as GET /v1/images/builds/<id> shows it, or None if unknown."""
        status, build = self.status(build_id)
        if status == "running":
            return _running(build_id)
        return build

    def cancel(self, build_id):
        """(outcome, build): ``cancelled`` (now failed), ``finished`` (already
        terminal, including an earlier cancellation), ``stopping`` (asked to
        stop, not stopped yet) or (None, None) if unknown. A cancelled build
        reads as failed, so a later ensure may submit its recipe again."""
        image_id = _image_id(build_id)
        outcome, result = self.spool.cancel(image_id, build_id) if image_id else (None, None)
        if outcome is None:
            return None, None
        if outcome == "stopping":
            return outcome, _running(build_id)
        return outcome, {**result, "sandbox_build": True}

    def adopt(self, result):
        """A finished image as born in the chunk store; returns its image record."""
        repository, digest = result["repository"], result["manifest_digest"]
        if self.roots.get(repository, digest) is None:
            self.roots.record_converted(repository, digest, config_digest=result["config"], old_root=result["root"],
                                        new_root=result["root"], wave=WAVE, build_input=False,
                                        detail="built in a sandbox: born in the chunk store")
        row = self.roots.get(repository, digest)
        for state in {"converted": ("switched", "released"), "switched": ("released",)}.get(row["state"], ()):
            self.roots.transition(repository, digest, state, detail="sandbox build")
        self.roots.record_tags(repository, digest, [result["tag"].rpartition(":")[2]])
        if (repository, digest) not in self.roots.oci_released():
            self.roots.mark_oci_released(repository, digest, layer_bytes=0, detail="never had an OCI manifest")
        now = datetime.now(timezone.utc).isoformat()
        return {"id": result["image_id"], "tag": result["tag"], "source": "build:sandbox", "state": "available",
                "pushed": True, "manifest_digest": digest, "created_at": now, "updated_at": now,
                "labels": {"ucloud-sandboxes.sandbox-build": result["build_id"]}}


def _image_id(build_id):
    """The image id in a ``sandbox:<image_id>:<hex>`` build id, or "" (it names spool files)."""
    if not build_id.startswith(BUILD_PREFIX):
        return ""
    image_id = build_id[len(BUILD_PREFIX):].rpartition(":")[0]
    return image_id if IMAGE_ID_RE.fullmatch(image_id) else ""


def _running(build_id):
    return {"build_id": build_id, "image_id": _image_id(build_id), "status": "running", "sandbox_build": True}


def from_deployment(config, *, catalog, contexts, roots, environments, tag_for):
    """``immutable_environments.sandbox_builds`` on, else None."""
    selected = config.immutable_environments
    settings = selected.sandbox_builds if selected is not None else None
    if settings is None or not settings.enabled:
        return None
    return SandboxBuilds(Spool(spool_root(config)), catalog=catalog, contexts=contexts, roots=roots,
                         environments=environments, tag_for=tag_for, apt_proxy=apt_proxy(config))


def spool_root(config):
    return config.control_state_file().parent / "sandbox-builds"


def git_proxy(config):
    """The store node's package cache URL when it serves GitHub git fetches, or None."""
    chunk_store = config.immutable_environments.chunk_store if config.immutable_environments else None
    node = chunk_store.store_node if chunk_store is not None else None
    cache = getattr(node, "package_cache", None)
    return cache.url if cache is not None and cache.github_token_file else None


def apt_proxy(config):
    """(url, hosts) of the store node's package cache, or None."""
    chunk_store = config.immutable_environments.chunk_store if config.immutable_environments else None
    node = chunk_store.store_node if chunk_store is not None else None
    cache = getattr(node, "package_cache", None)
    return (cache.url, tuple(sorted(cache.upstreams))) if cache is not None else None
