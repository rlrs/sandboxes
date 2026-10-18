"""Public publication flow with a signed cached base and real OCI tail bytes.

The grouping threshold is reduced for small fixtures. Registry manifests,
compressed/uncompressed identities, component signatures and root bindings are
real; Docker mount/pull and EROFS construction are test adapters. Real filesystem
and EROFS equivalence is a separate qualification.
"""
from contextlib import ExitStack
import io
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.test_environment_layers import FORMAT, FakeDockerStore, Keys, LayerRegistry
from tests.test_oci_layer_materialize import directory, layer, member
from sandboxes import environment_builder
from sandboxes.environment_artifact import (
    EnvironmentArtifactRegistry, canonical_bytes, content_digest, load_image_environment,
)
from sandboxes.environment_builder import FreshEnvironmentBuilder


class StreamingRegistry(LayerRegistry):
    def __init__(self):
        super().__init__()
        self.opened = []
        self.streams = []

    def open_blob(self, repository, digest):
        self.opened.append((repository, digest))
        result = io.BytesIO(self.blobs[digest])
        self.streams.append(result)
        return result


class SelectiveEnvironmentPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.keys = Keys()
        self.client = StreamingRegistry()
        self.registry = EnvironmentArtifactRegistry(self.client, "environments", self.keys.trusted)
        self.store = FakeDockerStore(self.root / "docker")
        self.builder = FreshEnvironmentBuilder(self.store, self.registry, self.keys.key, self.root / "work")
        self.builder._layer_format = FORMAT
        self.mkfs_views = []
        self.stack.enter_context(patch.object(self.builder, "_mkfs", side_effect=self.mkfs))
        # Production uses a 64 MiB threshold. Keep the same planner with a small
        # threshold so a real uncompressed base closes its own fixture group.
        planner = environment_builder.plan_layer_groups
        self.stack.enter_context(patch.object(environment_builder, "plan_layer_groups",
            side_effect=lambda sizes, *, max_groups: planner(sizes, threshold=8192, max_groups=max_groups)))
        # Extraction is privileged in production. Test paths retain the caller's
        # ownership; signature/content/binding checks are not mocked.
        self.stack.enter_context(patch("sandboxes.oci_layer_materialize.os.chown"))
        self.base = layer([directory("base"), member("base/data", b"base" * 2048)], compressed=False)
        self.add_image("base", [self.base])
        _, base_environment = self.publish("base")
        self.base_component = base_environment.components[0]
        self.mkfs_views.clear()
        self.client.opened.clear()
        self.client.puts.clear()

    def add_image(self, tag, layers):
        config = {"Cmd": ["/app/run"], "Env": ["FIXTURE=selective"], "WorkingDir": "/app", "User": "123:456"}
        raw = canonical_bytes({"rootfs": {"type": "layers", "diff_ids": [value[1] for value in layers]},
                               "config": config})
        image_id = content_digest(raw)
        self.client.blobs[image_id] = raw
        directories = []
        for index, (descriptor, diff_id, blob) in enumerate(layers):
            self.client.blobs[descriptor["digest"]] = blob
            # This is the already-materialized Docker adapter used only when a
            # test explicitly expects fallback. OCI semantics are tested below.
            diff = self.root / "docker-diffs" / tag / str(index)
            diff.mkdir(parents=True)
            (diff / "docker-materialized").write_bytes(blob)
            directories.append((diff_id, diff))
        self.store.add(tag, image_id, directories)
        self.store.configs[tag] = config
        self.client.layer_sizes[tag] = [value[0]["size"] for value in layers]
        self.client.manifests[tag] = canonical_bytes({
            "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": image_id, "size": len(raw)},
            "layers": [value[0] for value in layers],
        })
        return image_id

    def mkfs(self, image, view, *, exclude_runtime_mounts, preserve_mtimes):
        self.assertTrue(exclude_runtime_mounts)
        self.assertEqual(preserve_mtimes, self.builder.layer_format()["layout"] == 2)
        snapshot = {}
        for path in sorted(view.rglob("*")):
            info = path.lstat()
            snapshot[str(path.relative_to(view))] = {
                "mode": stat.S_IMODE(info.st_mode),
                "content": path.read_bytes().hex() if path.is_file() else None,
            }
        self.mkfs_views.append(snapshot)
        # The registry signs these bytes exactly; real mkfs byte equivalence is
        # intentionally not claimed by this integration fixture.
        image.write_bytes(bytes.fromhex(content_digest(canonical_bytes(snapshot)).removeprefix("sha256:")) * 128)

    def publish(self, tag):
        repository = "ucloud-managed/" + tag
        annotated = self.builder.publish_image("registry.example/" + repository + ":" + tag, allowlist=("*",))
        _, environment = load_image_environment(self.registry, repository, annotated)
        return annotated, environment

    def tail(self, content=b"#!/bin/sh\necho selective\n"):
        return layer([directory("app"), member("app/run", content, mode=0o751)])

    def test_isolated_fallback_uses_docker_but_unexpected_failure_does_not_publish(self):
        from sandboxes.environment_prepare import PreparationError, PreparationResult
        self.builder.preparation_subprocess = True
        self.add_image("isolated-fallback", [self.base, self.tail()])
        with patch("sandboxes.environment_prepare.prepare_in_subprocess",
                   return_value=PreparationResult((), {"selective_materialization_ms": 2.0,
                       "squash_ms": 0.0, "selective_subprocess_ms": 30.0}, fallback=True)), \
             patch.object(self.store, "_checked", wraps=self.store._checked) as pull:
            self.publish("isolated-fallback")
        pull.assert_called_once()
        self.assertIn("docker-materialized", self.mkfs_views[0])
        self.add_image("isolated-failed", [self.base, self.tail(b"distinct missing tail")])
        scratch = []
        def fail(*args, timeout_seconds):
            self.assertEqual(timeout_seconds, 600)
            scratch.append(args[-1])
            (args[-1] / "partial").write_bytes(b"discarded")
            raise PreparationError("child failed")
        self.client.puts.clear()
        with patch("sandboxes.environment_prepare.prepare_in_subprocess", side_effect=fail), \
             patch.object(self.store, "_checked", side_effect=AssertionError("must not pull")), \
             patch.object(environment_builder, "sign_layer_component", side_effect=AssertionError("must not sign")), \
             self.assertRaises(PreparationError):
            self.publish("isolated-failed")
        self.assertEqual(self.client.puts, [])
        self.assertTrue(all(not path.exists() for path in scratch))


if __name__ == "__main__":
    unittest.main()
