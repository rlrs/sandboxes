import unittest

from sandboxes.agent import (
    build_heartbeat as _build_heartbeat,
    detect_job_id,
)
from sandboxes.models import (
    ResourceQuantity,
)

TEST_TIER = "contract"


def build_heartbeat(**kwargs):
    kwargs.setdefault("deployment_id", "test-deployment")
    return _build_heartbeat(**kwargs)


class AgentTests(unittest.TestCase):
    def test_detects_job_id_from_environment_mapping(self) -> None:
        self.assertEqual(detect_job_id({"UCLOUD_JOB_ID": "123"}), "123")
        self.assertEqual(detect_job_id({"JOB_ID": "456"}), "456")
        self.assertIsNone(detect_job_id({}))

    def test_rejects_missing_job_id(self) -> None:
        with self.assertRaises(ValueError):
            build_heartbeat(job_id="")

    def test_rejects_missing_deployment_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "deployment_id"):
            build_heartbeat(job_id="123", deployment_id="")

    def test_rejects_invalid_resources(self) -> None:
        with self.assertRaises(ValueError):
            build_heartbeat(
                job_id="123",
                used_resources=ResourceQuantity(memory_mb=-1),
            )


if __name__ == "__main__":
    unittest.main()
