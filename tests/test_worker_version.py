import argparse
import json
from pathlib import Path
import tempfile
import unittest

from sandboxes.cli import load_config
from sandboxes.config import DeploymentConfig
from sandboxes.deployment import (
    agent_version_is_schedulable,
    bundle_agent_version,
    package_version,
    use_worker_bundle,
    worker_agent_version,
)
from tests.support import write_node_bundle


class BundleAgentVersionTests(unittest.TestCase):
    def test_reads_the_release_the_bundle_installs_on_workers(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle = write_node_bundle(Path(raw) / "sandbox-node-package.tar.gz", "0.9.1")

            self.assertEqual(bundle_agent_version(bundle), "0.9.1")

    def test_a_bundle_without_an_agent_release_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle = write_node_bundle(Path(raw) / "sandbox-node-package.tar.gz", None)

            with self.assertRaises(ValueError):
                bundle_agent_version(bundle)


class WorkerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        raw = tempfile.TemporaryDirectory()
        self.addCleanup(raw.cleanup)
        self.root = Path(raw.name)
        # Leave the process expecting its own release, as without a bundle.
        self.addCleanup(use_worker_bundle, self.root / "absent.tar.gz")

    def test_workers_must_run_the_configured_bundle_not_the_gateway_release(self):
        older = "0.0.1" if package_version() != "0.0.1" else "0.0.2"
        use_worker_bundle(write_node_bundle(self.root / "bundle.tar.gz", older))

        self.assertEqual(worker_agent_version(), older)
        self.assertTrue(agent_version_is_schedulable(older))
        self.assertFalse(agent_version_is_schedulable(package_version()))

    def test_loading_the_config_expects_its_bundle(self):
        older = "0.0.1" if package_version() != "0.0.1" else "0.0.2"
        write_node_bundle(self.root / "sandbox-node-package.tar.gz", older)
        raw = DeploymentConfig.default(scope_id="project-1").to_dict()
        raw["node_package_root"] = str(self.root)
        (self.root / "deployment.json").write_text(json.dumps(raw))

        load_config(argparse.Namespace(config=self.root / "deployment.json"))

        self.assertEqual(worker_agent_version(), older)

    def test_without_a_bundle_workers_must_run_this_release(self):
        use_worker_bundle(self.root / "absent.tar.gz")

        self.assertEqual(worker_agent_version(), package_version())
        self.assertTrue(agent_version_is_schedulable(package_version()))


if __name__ == "__main__":
    unittest.main()
