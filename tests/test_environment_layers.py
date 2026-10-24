"""Per-layer EROFS roots older builders published: schema, binding and mounting.

Builders no longer publish layer groups; workers still read the roots they did.
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.test_environment_artifact import MemoryRegistry
from sandboxes.direct_registry import DirectRegistryCapacityUnavailable
from sandboxes.environment_artifact import (
    COMPONENT_SCHEMA, COMPONENT_SCHEMA_V2, EMPTY_LAYER_DIFF_ID, OCI_IMAGE, EnvironmentArtifactRegistry,
    EnvironmentComponent, LayerEnvironmentComponent, attach_environment_to_image, bind_source_layers,
    canonical_bytes, content_digest, layer_chain_id, load_image_environment, publish_environment,
    require_layer_format, sign_component, sign_layer_component,
)
from sandboxes.environment_backend import NO_BLOCK_DEVICE, mount_has_dependents
from sandboxes.environment_builder import FreshEnvironmentBuilder
from sandboxes.environment_manifest import EnvironmentManifest, HOST_EROFS_ABI
from sandboxes.environment_rootfs import EnvironmentDeviceCapacityError, EnvironmentRootfsStore
from sandboxes.image_rootfs import DockerImageConfig, DockerOverlay2RootfsStore

TEST_TIER = "contract"


FORMAT = {"layout": 1, "mkfs": "mkfs.erofs (erofs-utils) 1.8.1", "compression": "lz4",
          "excludes": ["dev", "proc", "run", "sys"]}


def digest(character):
    return "sha256:" + character * 64


def diff_id(name):
    return content_digest(name.encode())


def layer_tag(component: LayerEnvironmentComponent) -> str:
    return "layer-" + hashlib.sha256(repr(component.unsigned()).encode()).hexdigest()


class Keys:
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.trusted = {content_digest(public): public}


def image_file(path, fill):
    path.write_bytes(fill * 4096 * 3)
    return path


class LayerComponentSchemaTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.keys = Keys()
        self.client = MemoryRegistry()
        self.registry = EnvironmentArtifactRegistry(self.client, "environments", self.keys.trusted)

    def layer(self, layers, parent, fill=b"l"):
        image = image_file(self.root / (hashlib.sha256(repr((layers, fill)).encode()).hexdigest() + ".erofs"), fill)
        component = sign_layer_component(image, source_layers=layers, parent=parent,
                                         layer_format=FORMAT, signing_key=self.keys.key)
        return component, self.registry.publish(image, component, tag=layer_tag(component))

    def test_v1_and_v2_round_trip_with_schema_bound_signatures(self):
        whole = sign_component(image_file(self.root / "whole.erofs", b"w"), source_image=digest("1"),
                               signing_key=self.keys.key)
        whole_digest = self.registry.publish(self.root / "whole.erofs", whole, tag="whole")
        layer, layer_digest = self.layer((diff_id("a"), diff_id("b")), None)
        self.assertEqual(whole.schema, COMPONENT_SCHEMA)
        self.assertEqual(layer.schema, COMPONENT_SCHEMA_V2)
        self.assertEqual(self.registry.load(whole_digest), whole)
        self.assertEqual(self.registry.load(layer_digest), layer)
        self.assertIsInstance(EnvironmentComponent.from_dict(layer.to_dict()), LayerEnvironmentComponent)
        self.assertEqual(EnvironmentComponent.from_dict(whole.to_dict()), whole)
        for changed in (replace(layer, source_layers=(diff_id("a"),)),
                        replace(layer, parent=diff_id("z")),
                        replace(layer, format=FORMAT | {"compression": ""})):
            with self.subTest(changed=changed.unsigned()), self.assertRaisesRegex(ValueError, "signature"):
                changed.authenticate(self.keys.trusted)
        # A v2 index relabelled as v1 (or the reverse) is not a valid document.
        with self.assertRaises(ValueError):
            EnvironmentComponent.from_dict(layer.to_dict() | {"schema": COMPONENT_SCHEMA})
        with self.assertRaises(ValueError):
            EnvironmentComponent.from_dict(whole.to_dict() | {"schema": COMPONENT_SCHEMA_V2})
        for invalid in ({"source_layers": [EMPTY_LAYER_DIFF_ID]}, {"source_layers": []},
                        {"format": FORMAT | {"layout": 3}}, {"format": FORMAT | {"excludes": ["sys", "dev"]}},
                        {"parent": "not-a-digest"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                LayerEnvironmentComponent.from_dict(layer.to_dict() | invalid)

    def test_readers_accept_layouts_one_and_two_and_nothing_else(self):
        layers = (diff_id("a"),)
        for layout in (1, 2):
            with self.subTest(layout=layout):
                self.assertEqual(require_layer_format(FORMAT | {"layout": layout})["layout"], layout)
                image = image_file(self.root / f"layout-{layout}.erofs", b"m")
                signed = sign_layer_component(image, source_layers=layers, parent=None,
                                              layer_format=FORMAT | {"layout": layout}, signing_key=self.keys.key)
                published = self.registry.publish(image, signed, tag=layer_tag(signed))
                self.assertEqual(self.registry.load(published).format["layout"], layout)
                self.assertEqual(EnvironmentComponent.from_dict(signed.to_dict()).authenticate(self.keys.trusted),
                                 signed)
        for invalid in (FORMAT | {"layout": 0}, FORMAT | {"layout": 3}, FORMAT | {"layout": True},
                        FORMAT | {"layout": "2"}, FORMAT | {"layout": 2.0}, FORMAT | {"layout": 2, "mtimes": "kept"},
                        {key: value for key, value in FORMAT.items() if key != "layout"}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "layer format"):
                require_layer_format(invalid)

    def test_chain_id_names_the_ordered_layers_below_a_group(self):
        layers = [diff_id("a"), diff_id("b")]
        self.assertEqual(layer_chain_id([]), None)
        self.assertEqual(layer_chain_id([diff_id("a")]), diff_id("a"))
        self.assertEqual(layer_chain_id(layers), content_digest((diff_id("a") + " " + diff_id("b")).encode()))

    def test_root_binding_requires_exactly_the_image_layers(self):
        a, b, c = diff_id("a"), diff_id("b"), diff_id("c")
        lower, lower_digest = self.layer((a, b), None)
        upper, upper_digest = self.layer((c,), layer_chain_id([a, b]))
        foreign, foreign_digest = self.layer((c,), None, fill=b"f")
        toolkit = sign_component(image_file(self.root / "toolkit.erofs", b"t"), source_image=digest("9"),
                                 signing_key=self.keys.key)
        toolkit_digest = self.registry.publish(self.root / "toolkit.erofs", toolkit, tag="toolkit")

        def publish(components, diff_ids):
            return publish_environment(self.registry, source_image=digest("1"),
                environment=EnvironmentManifest(components[0], toolkits=tuple(components[1:])),
                image_config={"Cmd": ["/bin/sh"]}, signing_key=self.keys.key, tag="root",
                source_diff_ids=diff_ids)

        # Empty-tar layers change no file and are ignored; toolkits may follow.
        publish([lower_digest, upper_digest, toolkit_digest], [a, EMPTY_LAYER_DIFF_ID, b, c])
        for components, diff_ids, message in (
            ([lower_digest], [a, b, c], "differ"),
            ([lower_digest, upper_digest], [a, b], "differ"),
            ([lower_digest, upper_digest], [a, c, b], "differ"),
            ([lower_digest, foreign_digest], [a, b, c], "parent chain"),
            ([lower_digest, upper_digest], None, "diff_ids"),
        ):
            with self.subTest(diff_ids=diff_ids), self.assertRaisesRegex(ValueError, message):
                publish(components, diff_ids)
        with self.assertRaisesRegex(ValueError, "differ"):
            bind_source_layers([lower, upper], [])

    def test_rootfs_fingerprint_is_stable_over_ordered_components(self):
        manifest = EnvironmentManifest(digest("1"), toolkits=(digest("2"), digest("3")))
        again = EnvironmentManifest.from_dict(manifest.to_dict())
        self.assertEqual(manifest.rootfs_fingerprint(HOST_EROFS_ABI), again.rootfs_fingerprint(HOST_EROFS_ABI))
        # Pinned: checkpoints on every worker bind this exact value.
        self.assertEqual(manifest.rootfs_fingerprint(HOST_EROFS_ABI),
                         "99502df2ac8406c222ecc8ff1e4dc3e19a658fe7904482335200ddb800889631")
        self.assertEqual(EnvironmentManifest(digest("1")).rootfs_fingerprint(HOST_EROFS_ABI),
                         "52a222069c1bd02192e15d0d003c133ec5e2b7ea95ae5c1dc917c1ef49ee5fcf")
        self.assertNotEqual(
            manifest.rootfs_fingerprint(HOST_EROFS_ABI),
            EnvironmentManifest(digest("1"), toolkits=(digest("3"), digest("2"))).rootfs_fingerprint(HOST_EROFS_ABI))


class FakeDockerStore(DockerOverlay2RootfsStore):
    def __init__(self, root):
        self.docker_binary = "docker"
        self.root = root
        self.images = {}

    def add(self, name, image_id):
        rootfs = self.root / name / "rootfs"
        rootfs.mkdir(parents=True)
        (rootfs / "merged").write_text(name)
        self.images[name] = (image_id, rootfs)

    @contextmanager
    def operation_lease(self, image_ref, environment_root=None):
        image_id, rootfs = self.images[image_ref.rsplit(":", 1)[1]]
        yield SimpleNamespace(image_id=image_id, rootfs=rootfs,
                              image_config=DockerImageConfig.from_inspection({"Cmd": ["/bin/sh"]}))

    def collect_image(self, image_id, *, is_referenced):
        return True


class WholeImageBuildTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.keys = Keys()
        self.registry = EnvironmentArtifactRegistry(MemoryRegistry(), "environments", self.keys.trusted)
        self.store = FakeDockerStore(self.root / "docker")
        self.store.add("base", digest("a"))

    def mkfs(self, argv, **_kwargs):
        if argv[1] == "-V":
            return subprocess.CompletedProcess(argv, 0, stdout=FORMAT["mkfs"] + "\n", stderr="")
        view = Path(argv[-1])
        listing = sorted(str(path.relative_to(view)) for path in view.rglob("*"))
        Path(argv[-2]).write_bytes(hashlib.sha256(repr(listing).encode()).digest() * 128)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def capture_mkfs(self, commands, times):
        def run(command, **kwargs):
            if command[1] != "-V":
                commands.append(command)
                view = Path(command[-1])
                times.append({str(path.relative_to(view)): path.lstat().st_mtime_ns
                              for path in (view, *view.rglob("*"))})
            return self.mkfs(command, **kwargs)
        return patch("sandboxes.environment_builder.subprocess.run", side_effect=run)

    def test_whole_image_components_follow_the_switch_without_touching_the_docker_rootfs(self):
        rootfs = self.store.images["base"][1]
        os.utime(rootfs / "merged", ns=(0, 6 * 10**9))
        os.utime(rootfs, ns=(0, 5 * 10**9))
        for preserve in (False, True):
            builder = FreshEnvironmentBuilder(self.store, self.registry, self.keys.key, self.root / "scratch",
                                              preserve_mtimes=preserve)
            commands, times = [], []
            with self.capture_mkfs(commands, times):
                for allowlist in (("merged",), ("*",)):
                    builder.build("registry.example/ucloud-managed/base:base", allowlist=allowlist,
                                  tag="whole-" + "-".join(allowlist).replace("*", "all"))
            self.assertEqual([("--mkfs-time" in command, "--MZ" in command) for command in commands],
                             [(preserve, preserve)] * 2)
            # The private allowlisted view is normalized; the merged rootfs is borrowed.
            self.assertEqual(times, [{".": 0 if preserve else 5 * 10**9, "merged": 6 * 10**9},
                                     {".": 5 * 10**9, "merged": 6 * 10**9}])
        self.assertEqual(rootfs.lstat().st_mtime_ns, 5 * 10**9)


class ComposedImageTests(unittest.TestCase):
    """A root of three layer-group components, as older builders published them."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.keys = Keys()
        self.client = MemoryRegistry()
        self.client.base_url = "http://registry.example"
        self.registry = EnvironmentArtifactRegistry(self.client, "environments", self.keys.trusted)

    def publish(self, tag):
        components, below = [], []
        for index, group in enumerate([(diff_id("b0"), diff_id("b1")), (diff_id("b2"),), (diff_id("b3"),)]):
            image = image_file(self.root / f"group-{index}.erofs", bytes([ord("a") + index]))
            component = sign_layer_component(image, source_layers=group, parent=layer_chain_id(below),
                                             layer_format=FORMAT, signing_key=self.keys.key)
            components.append(self.registry.publish(image, component, tag=layer_tag(component)))
            below.extend(group)
        self.client.manifests[tag] = canonical_bytes({
            "schemaVersion": 2, "mediaType": OCI_IMAGE, "config": {"digest": digest("a")}, "layers": []})
        root = publish_environment(self.registry, source_image=digest("a"),
            environment=EnvironmentManifest(components[0], toolkits=tuple(components[1:])),
            image_config={"Cmd": ["/bin/sh"]}, signing_key=self.keys.key, tag="root", source_diff_ids=below)
        annotated = attach_environment_to_image(self.registry, image_repository="ucloud-managed/" + tag,
                                                image_reference=tag, environment_digest=root)
        _root, environment = load_image_environment(self.registry, "ucloud-managed/" + tag, annotated)
        return annotated, environment

    def test_composed_image_mounts_relative_lowers_and_counts_shared_devices(self):
        annotated, environment = self.publish("base")
        components = self.root / "backend" / "components"
        mounts, commands = set(), []

        class Runner:
            def run(self, command, **_kwargs):
                commands.append(command)
                if command[0] in {"mount", "env"}:
                    mounts.add(Path(command[-1]))
                code = int(Path(command[-1]) not in mounts) if command[0] == "mountpoint" else 0
                return SimpleNamespace(returncode=code, stdout="", stderr="")

        ensured = []

        def ensure(component):
            ensured.append(component)
            return components / component[7:]

        backend = SimpleNamespace(ensure=ensure, drop=lambda _digest: True)
        store = EnvironmentRootfsStore(self.root / "store", self.registry, backend, runner=Runner(),
                                       referenced=lambda _: False, block_devices=8)
        with store.operation_lease("registry.example/ucloud-managed/base@" + annotated) as image:
            self.assertEqual(image.backend_abi, HOST_EROFS_ABI)
            self.assertEqual(len(image.environment.toolkits), 2)
        mount = next(command for command in commands if command[0] == "env")
        self.assertEqual(mount[:5], ("env", "-C", str(components), "LIBMOUNT_FORCE_MOUNT2=always", "mount"))
        lowers = mount[mount.index("-o") + 1].removeprefix("ro,lowerdir=").split(":")
        self.assertEqual(lowers, [component[7:] for component in reversed(environment.components)])
        self.assertEqual(sorted(ensured[:3]), sorted(environment.components))  # Attached concurrently.
        self.assertEqual(store.operation_snapshot()["environment_devices_in_use"], 3)
        self.assertEqual(store.operation_snapshot()["environment_devices_free"], 5)
        self.assertTrue(store.collect_image(image.image_id, is_referenced=lambda _: False))
        self.assertEqual(store.operation_snapshot()["environment_devices_in_use"], 0)

        def exhausted(_component):
            raise RuntimeError(NO_BLOCK_DEVICE)

        store.backend = SimpleNamespace(ensure=exhausted, drop=lambda _digest: True)
        with self.assertRaises(EnvironmentDeviceCapacityError) as raised:
            with store.operation_lease("registry.example/ucloud-managed/base@" + annotated):
                pass
        self.assertIsInstance(raised.exception, DirectRegistryCapacityUnavailable)

    def test_partial_device_exhaustion_releases_unused_exports(self):
        _annotated, environment = self.publish("base")
        active = set()

        def ensure(component):
            if component not in active and len(active) == 2:
                raise RuntimeError(NO_BLOCK_DEVICE)
            active.add(component)
            return self.root / "components" / component[7:]

        def drop(component):
            active.discard(component)
            return True

        store = EnvironmentRootfsStore(self.root / "store", self.registry,
            SimpleNamespace(ensure=ensure, drop=drop), block_devices=2,
            runner=SimpleNamespace(run=lambda *args, **kwargs: SimpleNamespace(returncode=1)))
        (store.images / ("a" * 64)).mkdir()
        with self.assertRaises(EnvironmentDeviceCapacityError):
            store._mount(digest("a"), environment)
        self.assertEqual(active, set())


class RelativeLowerDependencyTests(unittest.TestCase):
    def test_relative_lowers_resolve_against_the_components_directory(self):
        with TemporaryDirectory() as raw:
            components = Path(raw) / "components"
            first, second, third = (components / (character * 64) for character in "123")
            for path in (first, second, third):
                path.mkdir(parents=True)
            overlay = (f"101 1 0:123 / /image ro - overlay overlay ro,lowerdir={'2' * 64}:{'1' * 64}\n"
                       "102 1 0:124 / /docker ro - overlay overlay ro,lowerdir=l/ABC:l/DEF\n")
            with patch("sandboxes.environment_backend.Path.read_text", return_value=overlay):
                self.assertTrue(mount_has_dependents(first, include_bind_mounts=False))
                self.assertTrue(mount_has_dependents(second, include_bind_mounts=False))
                self.assertFalse(mount_has_dependents(third, include_bind_mounts=False))
            plus = f"103 1 0:125 / /image ro - overlay overlay ro,lowerdir+={first},lowerdir+={'2' * 64}\n"
            with patch("sandboxes.environment_backend.Path.read_text", return_value=plus):
                self.assertTrue(mount_has_dependents(first, include_bind_mounts=False))
                self.assertTrue(mount_has_dependents(second, include_bind_mounts=False))


if __name__ == "__main__":
    unittest.main()
