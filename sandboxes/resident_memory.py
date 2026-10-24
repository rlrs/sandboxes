"""Cached, incarnation-fenced cgroup memory observations for wait decisions.

Sampling runs in maintenance, never on request admission. A configured memory
limit is not a resident footprint, and summing shared process RSS is not valid.
Unknown/stale data stays unknown rather than crediting a fictional reclaim.
With ``--rust-admission`` runtime/noded samples and the agent reads its
observations (PublishedResidentMemory).
"""

from dataclasses import dataclass, fields
import json
import logging
import math
import os
from pathlib import Path
import stat
from threading import Lock
import time

from .hibernation import linux_process_start_time_ticks

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResidentMemorySample:
    current_bytes: int
    anonymous_bytes: int
    file_bytes: int
    dirty_bytes: int
    writeback_bytes: int
    refault_file_pages: int
    cgroup_path: str
    cgroup_device: int
    cgroup_inode: int
    sentry_pid: int
    sentry_start_time_ticks: int
    sampled_at: float
    shared_memory_bytes: int = 0
    # memory.peak: this runtime's high-water mark (0 when the kernel lacks it).
    peak_bytes: int = 0
    # memory.swap.current: pages a paused runtime (C1.1) moved out. They are
    # not resident but remain its footprint: a thaw or capture faults them in.
    swap_bytes: int = 0


class ResidentMemorySampler:
    def __init__(
        self,
        *,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        proc_root: Path = Path("/proc"),
        max_age_seconds: float = 2.5,
    ):
        self.cgroup_root = cgroup_root
        self.proc_root = proc_root
        self.max_age_seconds = max_age_seconds
        self._samples: dict[tuple[str, int], ResidentMemorySample] = {}
        # (cgroup path, dev, inode) whose containment resolve() already
        # proved. Symlink resolution walks every component, once a second
        # for every live sandbox; an unchanged directory needs it only once.
        self._contained: dict[tuple[str, int], tuple[str, int, int]] = {}
        self._guard = Lock()

    def get(self, key: tuple[str, int]) -> ResidentMemorySample | None:
        with self._guard:
            sample = self._samples.get(key)
        if (
            sample is None
            or time.monotonic() - sample.sampled_at > self.max_age_seconds
        ):
            return None
        return sample

    def historical(self, key: tuple[str, int]) -> ResidentMemorySample | None:
        """Last charge for this incarnation, even after its runtime parks.

        Consumers must apply their own age fence; this is never live admission
        evidence. Normal retain/forget ownership cleanup also bounds this cache.
        """
        with self._guard:
            return self._samples.get(key)

    def forget(self, key) -> None:
        with self._guard:
            self._samples.pop(key, None)
            self._contained.pop(key, None)

    def retain(self, keys) -> None:
        keys = set(keys)
        with self._guard:
            self._samples = {
                key: sample for key, sample in self._samples.items() if key in keys
            }
            self._contained = {
                key: proof for key, proof in self._contained.items() if key in keys
            }

    def sample(
        self,
        key,
        *,
        pid: int,
        start_time_ticks: int,
        expected_path: str,
    ) -> ResidentMemorySample | None:
        try:
            if (
                linux_process_start_time_ticks(pid, proc_root=self.proc_root)
                != start_time_ticks
            ):
                raise ValueError("sentry identity changed")
            memberships = (
                (self.proc_root / str(pid) / "cgroup").read_text().splitlines()
            )
            unified = [line[3:] for line in memberships if line.startswith("0::")]
            if len(unified) != 1:
                raise ValueError("runtime has no unique unified memory cgroup")
            raw = unified[0]
            relative = Path(raw.removeprefix("/"))
            if (
                not raw.startswith("/")
                or not relative.parts
                or any(part in {".", ".."} for part in raw.split("/")[1:])
            ):
                raise ValueError("runtime cgroup path is invalid")
            if raw != expected_path:
                raise ValueError("runtime cgroup differs from OCI ownership")
            path = self.cgroup_root / relative
            identity = path.stat()
            with self._guard:
                proof = self._contained.get(key)
            if proof != (raw, identity.st_dev, identity.st_ino) or not stat.S_ISDIR(identity.st_mode):
                if path.resolve() != path or not path.is_dir():
                    raise ValueError("runtime cgroup escaped its trusted root")
                identity = path.stat()
            current = int((path / "memory.current").read_text().strip())
            try:
                peak = int((path / "memory.peak").read_text().strip())
            except FileNotFoundError:
                peak = 0
            try:
                swap = int((path / "memory.swap.current").read_text().strip())
            except FileNotFoundError:
                swap = 0
            counters = {}
            for line in (path / "memory.stat").read_text().splitlines():
                name, value = line.split()
                counters[name] = int(value)
            required = (
                "anon",
                "file",
                "file_dirty",
                "file_writeback",
                "workingset_refault_file",
            )
            if (
                current < 0
                or counters["shmem"] < 0
                or any(counters[name] < 0 for name in required)
            ):
                raise ValueError("memory accounting is negative")
            if (
                linux_process_start_time_ticks(pid, proc_root=self.proc_root)
                != start_time_ticks
            ):
                raise ValueError("sentry identity changed during sampling")
            after = path.stat()
            if (identity.st_dev, identity.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("runtime cgroup was replaced during sampling")
            result = ResidentMemorySample(
                current,
                *(counters[name] for name in required),
                raw,
                identity.st_dev,
                identity.st_ino,
                pid,
                start_time_ticks,
                time.monotonic(),
                counters["shmem"],
                max(peak, current) if peak else 0,
                max(0, swap),
            )
        except (OSError, ValueError, KeyError):
            with self._guard:
                self._samples.pop(key, None)
                self._contained.pop(key, None)
            return None
        with self._guard:
            self._samples[key] = result
            self._contained[key] = (raw, identity.st_dev, identity.st_ino)
        return result


_SAMPLE_FIELDS = tuple(field.name for field in fields(ResidentMemorySample))
_PUBLISHED_KEYS = frozenset(("sandbox_id", "generation", *_SAMPLE_FIELDS))
_PUBLISHED_MAX_BYTES = 16 * 1024 * 1024


def _published_sample(raw: object) -> tuple[tuple[str, int], ResidentMemorySample] | None:
    """One ``resident.json`` sample, or None when it is not exactly valid."""
    if not isinstance(raw, dict) or raw.keys() != _PUBLISHED_KEYS:
        return None
    sandbox_id, generation = raw["sandbox_id"], raw["generation"]
    if not isinstance(sandbox_id, str) or not sandbox_id or type(generation) is not int or generation < 1:
        return None
    values = {}
    for name in _SAMPLE_FIELDS:
        value = raw[name]
        if name == "cgroup_path":
            if not isinstance(value, str) or not value.startswith("/"):
                return None
        elif name == "sampled_at":
            if type(value) not in (int, float) or not math.isfinite(value):
                return None
            value = float(value)
        elif type(value) is not int or value < 0:
            return None
        values[name] = value
    return (sandbox_id, generation), ResidentMemorySample(**values)


class PublishedResidentMemory:
    """noded's resident observations behind the sampler's interface (phase 3b).

    With ``--rust-admission`` runtime/noded samples every owned, parkable,
    managed or paused sandbox once a second, with the same fields as
    ResidentMemorySampler on the same cgroup, and replaces
    ``<state_root>/noded/resident.json`` after each pass:
    ``{"seq", "session", "samples": [{sandbox_id, generation, <every
    ResidentMemorySample field>}]}``, ``sampled_at`` in CLOCK_MONOTONIC
    seconds (``time.monotonic()``). The agent no longer samples on its own
    schedule; ``load()`` reads a new pass and the caller credits it.

    - Age fences stay with the consumers, as for the sampler's own samples:
      ``get()`` at ``max_age_seconds``, growth credit at 30 s. A stopped
      noded's last samples therefore age out and charge whole bounds.
    - A missing, unreadable or invalid file, or an invalid sample, is no
      observation.
    - ``forget()`` (park, restore, delete) hides every observation taken
      before it until a newer one arrives: a pass that sampled before the
      forget may publish after it.
    - The agent's own reclaims still measure on demand through ``sample()``;
      ``get()`` and ``historical()`` return the newer of that and noded's.
    """

    def __init__(self, path: Path, sampler: ResidentMemorySampler):
        self.path = path
        self.sampler = sampler
        self.cgroup_root = sampler.cgroup_root
        self.proc_root = sampler.proc_root
        self.max_age_seconds = sampler.max_age_seconds
        self._published: dict[tuple[str, int], ResidentMemorySample] = {}
        self._fences: dict[tuple[str, int], float] = {}
        self._version: tuple[str, int] | None = None
        self._identity: tuple[int, ...] | None = None
        self._logged = float("-inf")
        self._guard = Lock()

    def load(self) -> bool:
        """Read ``resident.json``; True when its (session, seq) changed."""
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            return self._replace(None, None, {})
        except OSError as exc:
            return self._invalid(f"unreadable: {exc}")
        try:
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
                if identity == self._identity:
                    return False
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    return self._invalid("others may write it")
                data = stream.read(_PUBLISHED_MAX_BYTES + 1)
            raw = json.loads(data)
        except (OSError, ValueError) as exc:
            return self._invalid(f"unreadable: {exc}")
        if len(data) > _PUBLISHED_MAX_BYTES or not isinstance(raw, dict):
            return self._invalid("not an object")
        seq, session, samples = raw.get("seq"), raw.get("session"), raw.get("samples")
        if (type(seq) is not int or seq < 0 or not isinstance(session, str)
                or not 0 < len(session) <= 128 or not isinstance(samples, list)):
            return self._invalid("seq, session or samples is invalid")
        published = {}
        for item in samples:
            parsed = _published_sample(item)
            if parsed is not None:
                published[parsed[0]] = parsed[1]
        return self._replace((session, seq), identity, published)

    def _invalid(self, reason: str) -> bool:
        now = time.monotonic()
        if now - self._logged >= 60.0:
            self._logged = now
            _LOG.warning("noded resident.json ignored (%s): no resident observations", reason)
        return self._replace(None, None, {})

    def _replace(self, version, identity, published) -> bool:
        with self._guard:
            changed = version != self._version
            self._version, self._identity, self._published = version, identity, published
            for key, sample in published.items():
                if self._fences.get(key, sample.sampled_at) < sample.sampled_at:
                    self._fences.pop(key)  # Superseded by a later observation.
            return changed

    def historical(self, key: tuple[str, int]) -> ResidentMemorySample | None:
        own = self.sampler.historical(key)
        with self._guard:
            published, fence = self._published.get(key), self._fences.get(key)
        candidates = [sample for sample in (own, published)
                      if sample is not None and (fence is None or sample.sampled_at > fence)]
        return max(candidates, key=lambda sample: sample.sampled_at, default=None)

    def get(self, key: tuple[str, int]) -> ResidentMemorySample | None:
        sample = self.historical(key)
        if sample is None or time.monotonic() - sample.sampled_at > self.max_age_seconds:
            return None
        return sample

    def forget(self, key) -> None:
        with self._guard:
            self._fences[key] = time.monotonic()
        self.sampler.forget(key)

    def retain(self, keys) -> None:
        keys = set(keys)
        with self._guard:
            self._fences = {key: fence for key, fence in self._fences.items() if key in keys}
        self.sampler.retain(keys)

    def sample(self, key, **kwargs) -> ResidentMemorySample | None:
        """Measure now (the agent's reclaims); a failure is no observation."""
        result = self.sampler.sample(key, **kwargs)
        if result is None:
            # As the sampler drops its own: unknown until a later observation.
            with self._guard:
                self._fences[key] = time.monotonic()
        return result
