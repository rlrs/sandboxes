from sandboxes.transition_admission import MemoryDemand
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sandboxes.background_io import Pressure, PressureSampler
from sandboxes.resource_evidence import MemoryBackingCapacity
from sandboxes.warm_park import WarmParkDeferred, WarmParkPolicy, decide_resident_wait

GIB = 1024**3


class ResidentMemoryBackingTests(TestCase):
    def pressure(self, free=2 * GIB):
        return Pressure(
            0.2, 0, 0, 20 * GIB, MemoryBackingCapacity(95 * GIB, free, "mount")
        )

    def test_tmpfs_deficit_skips_known_file_heaps_and_credits_ram_only(self):
        pressure = [self.pressure(20 * GIB)]
        policy = WarmParkPolicy(lambda: pressure[0])
        for key, ram in (('file', 0), ('ram', 6 * GIB)):
            with self.assertRaises(WarmParkDeferred):
                with policy.defer(key, memory_bytes=6 * GIB, ram_bytes=ram, blocking=False):
                    self.fail('healthy headroom must retain both backings')
        pressure[0] = self.pressure(2 * GIB)
        self.assertFalse(policy.ready('file', memory_bytes=6 * GIB, ram_bytes=0))
        self.assertTrue(policy.ready('ram', memory_bytes=6 * GIB, ram_bytes=6 * GIB))
        with policy.defer('ram', memory_bytes=6 * GIB, ram_bytes=6 * GIB, blocking=False):
            self.assertFalse(policy.ready('file', memory_bytes=6 * GIB, ram_bytes=0))

    def test_pressure_refresh_preserves_configured_unknown_after_sample_expiry(self):
        with TemporaryDirectory() as temporary:
            proc = Path(temporary)
            (proc / "meminfo").write_text(
                "MemTotal: 104857600 kB\nMemAvailable: 20971520 kB\n"
            )
            sampler = PressureSampler(proc, memory_backing_root=Path("/active-memory"))
            with (
                patch(
                    "sandboxes.background_io.time.monotonic", return_value=100
                ),
                patch(
                    "sandboxes.background_io.sample_memory_backing",
                    return_value=self.pressure().memory_backing,
                ) as read,
            ):
                first = sampler.sample()
                self.assertIs(sampler.sample(), first)
                read.assert_called_once_with(Path("/active-memory"), proc_root=proc)
            with (
                patch(
                    "sandboxes.background_io.time.monotonic", return_value=101
                ),
                patch(
                    "sandboxes.background_io.sample_memory_backing",
                    return_value=MemoryBackingCapacity(),
                ),
            ):
                newer = sampler.sample()
            self.assertEqual(newer.memory_backing, MemoryBackingCapacity())
            self.assertTrue(decide_resident_wait(newer, MemoryDemand(0, 0)).backing_reclaim)
