import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sandboxes.environment_keys import provision
from tests import test_vm_init as vm_fixtures
from sandboxes.config import DeploymentConfig
from sandboxes.environment_config import EnvironmentDeploymentConfig, environment_publisher_from_args
from sandboxes.vm_init import render_vm_init_script

TEST_TIER = "contract"


def chunk_store(**changes):
    from tests.test_chunk_store_formats import ChunkStoreConfigTests
    return {**ChunkStoreConfigTests.RAW, "nydus_image_sha256": "a" * 64, **changes}


# What the gateway hands a builder: builds end in the chunk store.
BUILDER_MATERIAL = {"chunk_store_config_json": json.dumps(chunk_store()), "chunk_store_write_token": "w" * 64,
                    "chunk_store_s3_access_key_id": "AKIDBUILDER1",
                    "chunk_store_s3_secret_access_key": "secret-builder-key"}


class EnvironmentBootstrapTests(unittest.TestCase):
    def test_builders_need_the_chunk_store_and_publish_environment_an_allowlist(self):
        from sandboxes.environment_builder import FreshEnvironmentBuilder, publish_from_args

        self.assertIsNone(environment_publisher_from_args(SimpleNamespace()))
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = provision(root / "key")
            registry = {"environment_registry_url": "http://registry:5000",
                        "environment_registry_repository": "environments",
                        "environment_trusted_keys": Path(key["public_trust_file"]),
                        "environment_signing_key": Path(key["private_key_file"])}
            with self.assertRaisesRegex(ValueError, "--chunk-store-config"):
                environment_publisher_from_args(SimpleNamespace(**registry, image_file=root / "images.json"))
            # publish-environment (toolkits) keeps the allowlisted EROFS builder.
            toolkit = {**registry, "image_ref": "registry:5000/owned/toolkit:1", "state_root": root,
                       "docker_binary": "docker", "environment_preserve_mtimes": True}
            with self.assertRaisesRegex(ValueError, "allowlist"):
                publish_from_args(SimpleNamespace(**toolkit, environment_allow_path=[]))
            seen = []

            def publish_image(builder, image_ref, *, allowlist):
                seen.append((builder.preserve_mtimes, builder.work_root, image_ref, allowlist))
                raise LookupError("stop before the registry")

            with patch.object(FreshEnvironmentBuilder, "publish_image", publish_image), \
                    self.assertRaises(LookupError):
                publish_from_args(SimpleNamespace(**toolkit, environment_allow_path=["opt/ucloud/toolkits/kit"]))
            self.assertEqual(seen, [(True, root / "environment-build/scratch", "registry:5000/owned/toolkit:1",
                                     ("opt/ucloud/toolkits/kit",))])

    def test_node_advertises_actual_runtime_restore_identity_and_selected_adapter(self):
        from tests import test_direct_provisioner as fixtures
        from sandboxes.capabilities import HOST_EROFS_CAPABILITY, RUNTIME_COMPATIBILITY_CAPABILITY_PREFIX
        from sandboxes.direct_service import DirectSandboxService
        from sandboxes.environment_manifest import HOST_EROFS_ABI
        from sandboxes.models import ResourceQuantity
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            provisioner, _, _, _, warden = fixtures.DirectProvisionerTests().make(root)
            provisioner.overlays.image_store.backend_abi = HOST_EROFS_ABI
            provisioner.overlays.image_store.io_metrics = lambda: {"hits": 3}
            service = DirectSandboxService(provisioner, process_runner=fixtures.FakeProcessRunner())
            server = fixtures.build_direct_node_agent_server("127.0.0.1", 0, service=service,
                image_file=root / "images", job_id="job", node_id="node", total_resources=ResourceQuantity())
            try:
                caps = server.RequestHandlerClass.capabilities
                self.assertIn(HOST_EROFS_CAPABILITY, caps)
                self.assertIn(RUNTIME_COMPATIBILITY_CAPABILITY_PREFIX + warden.config.runtime_fingerprint.node_compatibility_sha256, caps)
                metrics = server.RequestHandlerClass.runtime_metrics_provider
                self.assertEqual(metrics().environment_io, {"hits": 3})
                # Read before, and independent of, the storage daemon's metrics.
                with patch.object(service.warden.storage, "get_metrics", side_effect=OSError("storage down")):
                    self.assertEqual(metrics().environment_io, {"hits": 3})
            finally:
                server.server_close()

    def test_config_stays_disabled_until_explicitly_selected_and_roundtrips(self):
        config = DeploymentConfig.default()
        self.assertNotIn("immutable_environments", config.to_dict())
        self.assertIsNone(DeploymentConfig.from_dict(config.to_dict()).immutable_environments)
        raw = config.to_dict()
        raw["immutable_environments"] = {"trusted_keys_file": "/etc/producers.json", "worker_enabled": True}
        parsed = DeploymentConfig.from_dict(raw)
        # Retired: true only, and written true, so 0.10.0 reads the same file.
        self.assertIs(parsed.to_dict()["immutable_environments"]["worker_enabled"], True)
        del raw["immutable_environments"]["worker_enabled"]
        self.assertEqual(DeploymentConfig.from_dict(raw), parsed)
        self.assertFalse(parsed.immutable_environments.builder_enabled)
        self.assertEqual(DeploymentConfig.from_dict(parsed.to_dict()), parsed)
        # Configs written for 0.10.0 name the retired prefetch switches; a
        # config written now leaves them out, which 0.10.0 reads as its defaults.
        for retired in ({"prefetch_enabled": True, "shared_traces": False},
                        {"prefetch_enabled": False, "shared_traces": True}):
            with self.subTest(retired=retired):
                raw["immutable_environments"] = {"trusted_keys_file": "/etc/producers.json", "worker_enabled": True,
                                                 **retired}
                self.assertEqual(DeploymentConfig.from_dict(raw), parsed)
        self.assertFalse({"prefetch_enabled", "shared_traces"} & set(parsed.immutable_environments.to_dict()))
        for change in ({"worker_enabled": 1}, {"worker_enabled": False}, {"builder_enabled": True},
                       {"prefetch_enabled": "false"}, {"prefetch_enabled": 0}, {"shared_traces": "true"},
                       {"attach_concurrency": 0},
                       {"attach_concurrency": True}, {"device_budget_percent": 0}, {"device_budget_percent": 101},
                       {"device_budget_percent": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                EnvironmentDeploymentConfig.from_dict({"trusted_keys_file": "/etc/producers.json", **change})

    def test_deployment_settings_reach_the_worker_backend_command(self):
        from dataclasses import replace
        from tests.test_policy import node
        from sandboxes import cli
        with TemporaryDirectory() as temporary:
            trust = provision(Path(temporary) / "key")["public_trust_file"]
            base = DeploymentConfig.default(scope_id="test")
            for attach, budget in ((1, 75), (4, 60)):
                selected = EnvironmentDeploymentConfig.from_dict({"trusted_keys_file": trust,
                                                                  "prefetch_enabled": False, "shared_traces": True,
                                                                  "attach_concurrency": attach,
                                                                  "device_budget_percent": budget})
                # Written only once changed, so a rollback still reads the config.
                self.assertEqual("device_budget_percent" in selected.to_dict(), budget != 75)
                with self.subTest(attach=attach), patch.object(cli, "read_bearer_token_source", return_value="t"):
                    options = cli.vm_init_options_for_job(
                        replace(base, immutable_environments=selected), node("worker").job, "sandbox",
                        package_spec="/tmp/package.tar.gz", package_sha256="a" * 64)
                    script = render_vm_init_script(options)
                    self.assertNotIn("--disable-prefetch", script)
                    self.assertNotIn("--shared-traces", script)
                    self.assertEqual("--attach-concurrency 4" in script, attach == 4)  # Absent at the default.
                    self.assertEqual(options.environment_device_budget_percent, budget)
                    self.assertEqual("--environment-device-budget-percent" in script, budget != 75)
                    self.assertEqual("--environment-device-budget-percent 60" in script, budget == 60)

    def test_the_backend_command_still_accepts_units_rendered_by_0_10_0(self):
        from sandboxes import cli
        common = ["serve-environment-io", "--root", "/state/environment-io", "--socket", "/run/io.sock"]
        self.assertEqual(cli.build_parser().parse_args([*common, "--disable-prefetch", "--shared-traces"]).socket,
                         Path("/run/io.sock"))

    def test_worker_and_builder_lifetimes_and_key_separation(self):
        with TemporaryDirectory() as temporary:
            key = provision(Path(temporary) / "key")
            public = Path(key["public_trust_file"]).read_text()
            private = Path(key["private_key_file"]).read_text()
            common = {"environment_registry_url": "http://registry:5000",
                      "environment_repository": "environments", "environment_trusted_keys_json": public}
            worker = render_vm_init_script(vm_fixtures.VmInitTests._options(**common))
            self.assertIn("serve-environment-io", worker)
            self.assertIn("LimitNOFILE=65536", worker.split("ENVIRONMENT_IO_SERVICE")[1])  # Not 1024 (512-burst EMFILE).
            self.assertIn("systemctl start ucloud-environment-io.service", worker)
            self.assertNotIn("systemctl restart ucloud-environment-io.service", worker)
            self.assertNotIn("PartOf=", worker)
            self.assertIn("PrivateMounts=no", worker)
            self.assertIn("require a fresh worker", worker)
            self.assertNotIn("producer.pem", worker)
            self.assertIn(" --cache-bytes 1073741824 --environment-registry-url", worker)
            builder = render_vm_init_script(vm_fixtures.VmInitTests._options(role="builder", **common,
                environment_signing_key_pem=private, **BUILDER_MATERIAL))
            self.assertIn("--environment-signing-key", builder)
            self.assertNotIn("--environment-allow-path", builder)
            self.assertNotIn("serve-environment-io", builder)
            builder_unit = builder.split("Description=UCloud sandbox node agent", 1)[1].split("NODE_SERVICE", 1)[0]
            self.assertIn("User=root", builder_unit)
            self.assertIn("chown root:root /etc/ucloud-sandboxes/environment/producer.pem", builder)
            for script in (worker, builder):
                syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
                self.assertEqual(syntax.returncode, 0, syntax.stderr)
            with self.assertRaisesRegex(ValueError, "never receive"):
                render_vm_init_script(vm_fixtures.VmInitTests._options(**common, environment_signing_key_pem=private))
            with self.assertRaisesRegex(ValueError, "headroom"):
                render_vm_init_script(vm_fixtures.VmInitTests._options(**common, environment_cache_bytes=100 * 1024 ** 3))
            # Chunk-store workers get the index's read token, never an S3 key.
            chunk = {"environment_chunk_index_url": "http://10.42.0.2:5090", "environment_chunk_index_token": "t" * 64}
            reader = render_vm_init_script(vm_fixtures.VmInitTests._options(**common, **chunk))
            self.assertIn(" --chunk-index-url http://10.42.0.2:5090 --chunk-index-token-file "
                          "/etc/ucloud-sandboxes/environment/chunk-index.token --chunk-concurrent-misses 32", reader)
            self.assertNotIn("chunk-index", worker)
            self.assertNotIn("--chunk-store-url", reader)  # C2.6: only with a store node.
            node = render_vm_init_script(vm_fixtures.VmInitTests._options(
                **common, **chunk, environment_chunk_store_url="http://10.42.0.200:5091"))
            self.assertIn("--chunk-concurrent-misses 32 --chunk-store-url http://10.42.0.200:5091", node)
            # The node agent advertises RAFS (M2 dispatch) only with a store node behind it.
            self.assertIn(" --environment-backend-socket /run/ucloud-environment/io.sock --environment-rafs", node)
            self.assertNotIn("--environment-rafs", reader)
            nydusd = {"environment_chunk_nydusd": "/usr/local/libexec/nydusd", "environment_chunk_nydusd_sha256": "9" * 64}
            self.assertIn("--chunk-store-url http://10.42.0.200:5091 --nydusd /usr/local/libexec/nydusd --nydusd-sha256 "
                          + "9" * 64, render_vm_init_script(vm_fixtures.VmInitTests._options(
                              **common, **chunk, **nydusd, environment_chunk_store_url="http://10.42.0.200:5091")))
            self.assertNotIn("--nydusd", node)
            from sandboxes.environment_config import NYDUSD_INSTALL_PATH
            bundled = render_vm_init_script(vm_fixtures.VmInitTests._options(
                **common, **chunk, environment_chunk_store_url="http://10.42.0.200:5091",
                environment_chunk_nydusd=NYDUSD_INSTALL_PATH, environment_chunk_nydusd_sha256="9" * 64))
            # The bundle's own install path pins the bundle's binary; another path is the operator's.
            self.assertIn('[ "${UCLOUD_BUNDLED_NYDUSD_SHA256:-}" = ' + "9" * 64 + " ]", bundled)
            self.assertNotIn("this bundle does not carry", render_vm_init_script(vm_fixtures.VmInitTests._options(
                **common, **chunk, **nydusd, environment_chunk_store_url="http://10.42.0.200:5091")))
            for invalid in (nydusd, {**nydusd, "environment_chunk_nydusd_sha256": "x"}):
                with self.subTest(nydusd=invalid), self.assertRaises(ValueError):  # Pinned, and only with a store node.
                    render_vm_init_script(vm_fixtures.VmInitTests._options(**common, **chunk, **invalid))
            with self.assertRaisesRegex(ValueError, "store node"):
                render_vm_init_script(vm_fixtures.VmInitTests._options(
                    **common, environment_chunk_store_url="http://10.42.0.200:5091"))
            for invalid in ({**chunk, "environment_chunk_index_token": ""}, {**chunk, "role": "builder",
                            "environment_signing_key_pem": private, **BUILDER_MATERIAL}):
                with self.assertRaisesRegex(ValueError, "chunk index"):
                    render_vm_init_script(vm_fixtures.VmInitTests._options(**common, **invalid))
            altered = json.loads(public)
            altered[next(iter(altered))] = "bad"
            with self.assertRaises(ValueError):
                render_vm_init_script(vm_fixtures.VmInitTests._options(**{**common, "environment_trusted_keys_json": json.dumps(altered)}))


if __name__ == "__main__":
    unittest.main()


class RafsBuilderBootstrapTests(unittest.TestCase):
    """Builds end in the chunk store: builders get its write material."""

    def test_builders_need_a_pinned_nydus_image_and_retired_keys_still_load(self):
        # What production's deployment.json says for 0.10.0.
        production = {"trusted_keys_file": "/etc/producers.json", "signing_key_file": "/etc/producer.pem",
                      "builder_enabled": True, "chunk_store": chunk_store(), "builder_format": "rafs",
                      "allow_paths": ["*"]}
        parsed = EnvironmentDeploymentConfig.from_dict(production)
        self.assertEqual(parsed.to_dict()["chunk_store"]["nydus_image_sha256"], "a" * 64)
        self.assertEqual(EnvironmentDeploymentConfig.from_dict(parsed.to_dict()), parsed)
        for retired in ({"builder_format": "rafs"}, {"allow_paths": ["*"]}, {"allow_paths": []},
                        {"preserve_mtimes": True}, {"preserve_mtimes": False}):
            with self.subTest(retired=retired):
                loaded = EnvironmentDeploymentConfig.from_dict(production | retired)
                self.assertEqual(loaded, parsed)
                self.assertFalse({"builder_format", "allow_paths", "preserve_mtimes"} & set(loaded.to_dict()))
        without = {key: value for key, value in production.items() if key != "chunk_store"}
        for bad, message in (({"builder_format": "erofs"}, "builder_format 'erofs' is not supported"),
                             ({"builder_format": "squashfs"}, "builder_format 'squashfs' is not supported"),
                             ({"preserve_mtimes": 1}, "preserve_mtimes must be boolean"),
                             ({"allow_paths": "*"}, "allow_paths must be a list"),
                             ({"chunk_store": chunk_store(nydus_image_sha256=None)}, "nydus_image_sha256")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, message):
                EnvironmentDeploymentConfig.from_dict(production | bad)
        with self.assertRaisesRegex(ValueError, "nydus_image_sha256"):
            EnvironmentDeploymentConfig.from_dict(without)

    def test_a_rafs_builder_gets_write_material_and_installs_the_pinned_nydus_image(self):
        with TemporaryDirectory() as temporary:
            key = provision(Path(temporary) / "key")
            common = {"environment_registry_url": "http://registry:5000", "environment_repository": "environments",
                      "environment_trusted_keys_json": Path(key["public_trust_file"]).read_text()}
            builder = {**common, "role": "builder", "environment_signing_key_pem": Path(key["private_key_file"]).read_text()}
            material = BUILDER_MATERIAL
            script = render_vm_init_script(vm_fixtures.VmInitTests._options(**builder, **material))
            self.assertIn(" --environment-signing-key /etc/ucloud-sandboxes/environment/producer.pem"
                          " --chunk-store-config /etc/ucloud-sandboxes/chunk-store.json"
                          " --chunk-index-token-file /etc/ucloud-sandboxes/environment/chunk-index-write.token", script)
            unit = script.split("Description=UCloud sandbox node agent", 1)[1].split("NODE_SERVICE", 1)[0]
            self.assertIn("EnvironmentFile=/etc/ucloud-sandboxes/chunk-store.env", unit)
            self.assertIn('"$UCLOUD_PACKAGE_BUNDLE_DIR/runtime/nydus/nydus-image"', script)
            self.assertIn("a" * 64, script)  # Checked before it is installed.
            self.assertNotIn("AKIDBUILDER1", script)  # Written base64-encoded into a root-only file.
            self.assertNotIn("w" * 64, script)
            syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            with self.assertRaisesRegex(ValueError, "nydus_image_sha256"):
                render_vm_init_script(vm_fixtures.VmInitTests._options(**builder))
            with self.assertRaisesRegex(ValueError, "not the read token"):  # Builders never read as workers do.
                render_vm_init_script(vm_fixtures.VmInitTests._options(**builder, **material,
                                                                       chunk_store_read_token="r" * 64))
            with self.assertRaisesRegex(ValueError, "store role and builders"):  # Workers never get write material.
                render_vm_init_script(vm_fixtures.VmInitTests._options(**common, **material))
