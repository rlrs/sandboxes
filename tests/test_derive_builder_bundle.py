import hashlib
import io
import json
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.release.derive_builder_bundle import derive_builder_bundle
from sandboxes.environment_bootstrap import BUNDLED_NYDUS_IMAGE
from sandboxes.vm_init import (
    BUILDER_RUNTIME_PACKAGES,
    RUNTIME_KERNEL_MODULES,
    SANDBOX_RUNTIME_PACKAGES,
)


def _metadata(path: str, payload: bytes) -> dict[str, object]:
    return {
        "file": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


NYDUS_IMAGE = b"nydus-image"
NYDUS_IMAGE_SHA256 = hashlib.sha256(NYDUS_IMAGE).hexdigest()


class DeriveBuilderBundleTests(unittest.TestCase):
    def derive(self, root: Path, *, nydus_image_sha256: str = NYDUS_IMAGE_SHA256) -> Path:
        source = root / "sandbox.tar.gz"
        output = root / "builder.tar.gz"
        buildx = root / "docker-buildx-plugin_1.0_amd64.deb"
        buildx.write_bytes(b"buildx")
        nydus_image = root / "nydus-image"
        nydus_image.write_bytes(NYDUS_IMAGE)
        package = b"docker"
        agent = b"agent"
        runsc = b"runsc"
        managed = b"init"
        storage = b"storage"
        storage_manifest = b"{}\n"
        storage_license = b"MIT\n"
        module = b"module"
        manifest = {
            "version": 1,
            "runtime": {
                "role": "sandbox",
                "platform": {
                    "os_id": "ubuntu",
                    "version_id": "26.04",
                    "codename": "resolute",
                    "architecture": "amd64",
                },
                "packages": list(SANDBOX_RUNTIME_PACKAGES),
                "files": [
                    {
                        "name": "docker.deb",
                        "sha256": hashlib.sha256(package).hexdigest(),
                        "size": len(package),
                    }
                ],
                "agent": _metadata("runtime/agent/node-agent-runtime.tar", agent),
                "direct_runsc": {
                    **_metadata("runtime/direct/runsc", runsc),
                    "commit": "a" * 40,
                },
                "managed_init": _metadata(
                    "runtime/direct/ucloud-sandbox-init", managed
                ),
                "storage_native": {
                    **_metadata("runtime/storage-native/backend", storage),
                    "manifest_file": "runtime/storage-native/build-manifest.json",
                    "manifest_sha256": hashlib.sha256(storage_manifest).hexdigest(),
                    "license_file": "runtime/storage-native/LICENSE",
                    "license_sha256": hashlib.sha256(storage_license).hexdigest(),
                },
                "kernel": {
                    "release": "7.0.0-test",
                    "load": list(RUNTIME_KERNEL_MODULES),
                    "files": [
                        {
                            "name": "xfs.ko.zst",
                            "sha256": hashlib.sha256(module).hexdigest(),
                            "size": len(module),
                        }
                    ],
                },
            },
        }
        files = {
            "package-bundle.json": json.dumps(manifest).encode(),
            "runtime/debs/docker.deb": package,
            "runtime/agent/node-agent-runtime.tar": agent,
            "runtime/direct/runsc": runsc,
            "runtime/direct/ucloud-sandbox-init": managed,
            "runtime/storage-native/backend": storage,
            "runtime/storage-native/build-manifest.json": storage_manifest,
            "runtime/storage-native/LICENSE": storage_license,
            "runtime/kernel/7.0.0-test/xfs.ko.zst": module,
        }
        with tarfile.open(source, "w:gz") as archive:
            for name, payload in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": "Package: docker-buildx-plugin\nArchitecture: amd64\n",
                "stderr": "",
            },
        )()
        with patch(
            "scripts.release.derive_builder_bundle.subprocess.run", return_value=completed
        ):
            derive_builder_bundle(
                source, buildx, output,
                nydus_image=nydus_image, nydus_image_sha256=nydus_image_sha256,
            )
        return output

    def test_derivation_adds_buildx_and_removes_sandbox_only_artifacts(self) -> None:
        with TemporaryDirectory() as raw_dir:
            output = self.derive(Path(raw_dir))
            with tarfile.open(output, "r:gz") as archive:
                member_names = set(archive.getnames())
                source_manifest = archive.extractfile("package-bundle.json")
                assert source_manifest is not None
                derived = json.load(source_manifest)
            runtime = derived["runtime"]
            self.assertEqual(runtime["role"], "builder")
            self.assertEqual(runtime["packages"], list(BUILDER_RUNTIME_PACKAGES))
            self.assertIn("docker-buildx-plugin_1.0_amd64.deb", {item["name"] for item in runtime["files"]})
            self.assertNotIn("direct_runsc", runtime)
            self.assertNotIn("managed_init", runtime)
            self.assertNotIn("storage_native", runtime)
            self.assertNotIn("runtime/direct/runsc", member_names)
            self.assertNotIn("runtime/storage-native/backend", member_names)
            self.assertTrue(output.with_name(output.name + ".sha256").is_file())

    def test_builders_get_the_pinned_nydus_image_where_vm_init_installs_it_from(self) -> None:
        with TemporaryDirectory() as raw_dir:
            output = self.derive(Path(raw_dir))
            with tarfile.open(output, "r:gz") as archive:
                member = archive.getmember(BUNDLED_NYDUS_IMAGE)
                binary = archive.extractfile(member)
                assert binary is not None
                self.assertEqual(binary.read(), NYDUS_IMAGE)
                manifest = archive.extractfile("package-bundle.json")
                assert manifest is not None
                runtime = json.load(manifest)["runtime"]
            self.assertEqual(member.mode & 0o777, 0o755)
            self.assertEqual(runtime["nydus_image"]["sha256"], NYDUS_IMAGE_SHA256)

    def test_a_nydus_image_other_than_the_pinned_one_is_refused(self) -> None:
        with TemporaryDirectory() as raw_dir:
            with self.assertRaises(ValueError):
                self.derive(Path(raw_dir), nydus_image_sha256="0" * 64)
            self.assertFalse((Path(raw_dir) / "builder.tar.gz").exists())


if __name__ == "__main__":
    unittest.main()
