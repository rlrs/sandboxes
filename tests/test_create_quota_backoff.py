from datetime import timedelta
import unittest

from sandboxes.autoscaler_state import ProviderOperation
from sandboxes.cli import CREATE_QUOTA_BACKOFF, quota_blocked_roles
from sandboxes.models import utc_now
from sandboxes.providers.base import QUOTA_EXCEEDED


def failed_create(role: str, *, quota: bool, age: timedelta) -> ProviderOperation:
    at = utc_now() - age
    return ProviderOperation(
        operation_id=f"provider-{role}-{int(age.total_seconds())}-{quota}", intent_key="k",
        kind="create", deployment_id="d", role=role, state="failed", request={},
        response={QUOTA_EXCEEDED: True} if quota else {}, target_job_ids=(),
        created_at=at, updated_at=at, last_error="refused",
    )


class QuotaBackoffTests(unittest.TestCase):
    def test_a_quota_refusal_blocks_its_role_until_the_backoff_passes(self):
        now = utc_now()
        operations = [
            failed_create("sandbox", quota=True, age=CREATE_QUOTA_BACKOFF - timedelta(seconds=5)),
            failed_create("builder", quota=True, age=CREATE_QUOTA_BACKOFF + timedelta(seconds=5)),
        ]

        self.assertEqual(quota_blocked_roles(operations, now), {"sandbox"})

    def test_other_rejections_do_not_back_off(self):
        operations = [failed_create("sandbox", quota=False, age=timedelta(seconds=1))]

        self.assertEqual(quota_blocked_roles(operations, utc_now()), set())


if __name__ == "__main__":
    unittest.main()
