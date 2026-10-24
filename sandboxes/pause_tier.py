"""Pause tier (C1.1): stop idle sandboxes in place, reclaim them under pressure.

runtime/noded runs the policy (pause_handoff.pause_config sends it the
constants below); the Warden pauses, thaws and prefetches with what is here.

A pause changes no ownership: the Warden journal stays RUNNING/LIVE, the route
stays running, and the Warden thaws before any runsc exec. Nothing here grants
lifecycle authority. Its state is disposable; Warden pause markers are truth.

`runsc pause` stops the guest's tasks inside the Sentry. It does not freeze the
cgroup (2026-10-02 qualification: `frozen 0`), and the Warden does not write
cgroup.freeze either: it changed no reclaim measurably, and a frozen cgroup must
be thawed before every runsc resume and exec, a second state that every Warden
path and crash window would have to undo first.
"""

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import threading

# One node-wide reclaim budget (qualification: one reclaim moves 225-380 MiB/s
# with zswap off; eight at once share about 0.85 GiB/s, and with zswap they are
# CPU-bound at 21 MiB/s each). Two in flight at one shared rate keep a single
# sandbox's speed and leave disk for captures and pulls. A window is one write;
# 16 MiB windows halved one reclaim's rate (105 against 188 MB/s, benchmarks/
# pause-reclaim-2026-10-03), so a window is 128 MiB, still cancellable between.
RECLAIM_CONCURRENCY = 2
RECLAIM_BYTES_PER_SECOND = 512 * 1024**2
RECLAIM_WINDOW_BYTES = 128 * 1024**2
# A reclaim freeing less than this (or less than its whole target) made no
# progress. A wait retries after a doubling backoff and escalates to hibernate
# only after MAX_STALLS such reclaims in a row: one unlucky window is no proof
# that swap cannot take the wait (DSec: hibernate is for offload).
STALL_BYTES = 16 * 1024**2
STALL_BACKOFF_SECONDS = 10.0
MAX_STALLS = 2
# zswap keeps incompressible pages nearly 1:1 in a pool charged to the same
# cgroup, so a reclaim through it frees nothing (1,568 MB moved, 32 MB freed:
# benchmarks/pause-reclaim-2026-10-03). When zswap is on, each paused cgroup
# may hold at most this share of its memory bound there; the kernel sends the
# rest to swap. A fixed bound, whatever the guest's memory compresses to.
ZSWAP_SHARE_OF_BOUND = 0.25
# Free swap below this share is the kernel's: reclaim stops short of it and
# the rest of the deficit escalates to hibernate.
SWAP_RESERVE_FRACTION = 0.10
ESCALATION_CONCURRENCY = 2
# Thaw prefetch (qualification: 4 MiB pieces over 8 threads swapped 645 MiB
# back in 0.81-0.95 s, where the guest's own faults took 3.6-5.0 s; eight thaws
# with 64 readers reached 1.5 GiB/s together). A thaw waits for at most 1 GiB
# (about 1.3 s alone) and 2 s; demand faults bring back the rest, in
# proportion to what the guest touches.
PREFETCH_THREADS = 8
PREFETCH_NODE_THREADS = 64
PREFETCH_PIECE_BYTES = 4 * 1024**2
PREFETCH_READ_BYTES = 1024**2
PREFETCH_MAX_BYTES = 1024**3
PREFETCH_SECONDS = 2.0
# Less swap than this is about the Sentry's and gofer's own heaps (15 MiB,
# which no memory-file read restores): nothing worth a prefetch moved out.
PREFETCH_MIN_SWAP_BYTES = 32 * 1024**2


def cap_zswap(pid, *, proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup"),
              zswap_enabled=Path("/sys/module/zswap/parameters/enabled")):
    """Bound a process's cgroup's zswap to ZSWAP_SHARE_OF_BOUND of its memory
    bound, once; the bytes set, or None (zswap off, no bound, already set)."""
    try:
        if zswap_enabled.read_text().strip() not in {"Y", "1"}:
            return None
        unified = [line[4:] for line in (proc_root / str(pid) / "cgroup").read_text().splitlines()
                   if line.startswith("0::/")]
        if len(unified) != 1 or {".", ".."} & set(unified[0].split("/")):
            return None
        cgroup = cgroup_root / unified[0]
        bound = (cgroup / "memory.max").read_text().strip()
        if bound == "max" or (cgroup / "memory.zswap.max").read_text().strip() != "max":
            return None
        cap = int(int(bound) * ZSWAP_SHARE_OF_BOUND)
        (cgroup / "memory.zswap.max").write_text(str(cap))
        return cap
    except (OSError, ValueError):
        return None  # Optional: without the cap a reclaim only frees less.


def cgroup_swap_bytes(pid, *, proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup")):
    """memory.swap.current of a process's unified cgroup, or None."""
    try:
        unified = [line[4:] for line in (proc_root / str(pid) / "cgroup").read_text().splitlines()
                   if line.startswith("0::/")]
        if len(unified) != 1 or {".", ".."} & set(unified[0].split("/")):
            return None
        return int((cgroup_root / unified[0] / "memory.swap.current").read_text())
    except (OSError, ValueError):
        return None


def memory_pieces(fd, *, budget_bytes=PREFETCH_MAX_BYTES, piece_bytes=PREFETCH_PIECE_BYTES):
    """(start, end) pieces of fd's data extents in file order, up to a budget.

    On tmpfs a swapped-out page is still data; a hole holds nothing to read.
    """
    pieces, offset, size = [], 0, os.fstat(fd).st_size
    while offset < size and budget_bytes > 0:
        try:
            start = os.lseek(fd, offset, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                break  # Only a hole remains.
            raise
        offset = min(os.lseek(fd, start, os.SEEK_HOLE), start + budget_bytes)
        pieces += [(at, min(at + piece_bytes, offset)) for at in range(start, offset, piece_bytes)]
        budget_bytes -= offset - start
    return pieces


def prefetch(fd, pieces, *, slots, cancelled, threads=PREFETCH_THREADS):
    """Read pieces of fd back in parallel; the bytes read.

    Each reader holds one of the node's `slots` for its whole life, so however
    many thaws run, the node has at most that many readers and read buffers.
    A thaw waits for its first slot and then takes only free ones, up to
    `threads`. Readers stop between reads once `cancelled()` is true or one
    fails. This returns only after every reader has exited, so the caller may
    close fd; a reader's error is raised after that.
    """
    pending, guard, counts, errors = iter(pieces), threading.Lock(), [], []

    def reader():
        count = 0
        try:
            buffer = memoryview(bytearray(PREFETCH_READ_BYTES))
            while not errors and not cancelled():
                with guard:
                    piece = next(pending, None)
                if piece is None:
                    break
                at, end = piece
                while at < end and not cancelled():
                    read = os.preadv(fd, [buffer[:end - at]], at)
                    if not read:
                        break  # The file shrank: nothing more here.
                    at, count = at + read, count + read
        except OSError as exc:
            errors.append(exc)
        finally:
            counts.append(count)
            slots.release()

    readers = []
    while len(readers) < min(threads, len(pieces)) and not cancelled():
        if not (slots.acquire(blocking=False) if readers else slots.acquire(timeout=0.05)):
            if readers:
                break
            continue
        readers.append(threading.Thread(target=reader, name="thaw-prefetch", daemon=True))
        try:
            readers[-1].start()
        except RuntimeError:  # No thread to spare: fewer readers, never a failed thaw.
            readers.pop()
            slots.release()
            break
    for item in readers:
        item.join()
    if errors:
        raise errors[0]
    return sum(counts)


STOP_COUNTERS = {"target_reached": "pause_reclaim_target_reached",
                 "not_shrinking": "pause_reclaim_not_shrinking",
                 "partial_reclaim": "pause_reclaim_partial", "raised": "pause_reclaim_errors"}
# The heartbeat's resident_wait names (models.ResidentWaitMetrics), exactly.
PAUSE_STAT_NAMES = (
    "pauses", "thaws", "thaw_ms_total", "thaw_ms_max", "pause_reclaims",
    "pause_reclaimed_bytes", "pause_reclaim_ms_total",
    "pause_reclaim_cancellations", "pause_reclaim_stalls", "pause_escalations",
    "thaw_prefetches", "thaw_prefetched_bytes", "thaw_prefetch_ms_total",
    *STOP_COUNTERS.values())


class PauseStats:
    """Monotonic counters; pause and thaw happen in the Warden on any path."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = dict.fromkeys(PAUSE_STAT_NAMES, 0)

    def add(self, **amounts):
        with self._lock:  # Exact sums; only the reported snapshot rounds.
            for name, amount in amounts.items():
                previous = self._values[name]
                self._values[name] = (max(previous, amount) if name.endswith("_max")
                                      else previous + amount)

    def snapshot(self):
        with self._lock:
            return {name: round(value) for name, value in self._values.items()}


# A thaw holds an exclusive flock on its pause marker from finding it until
# after the unlink: the cross-process "thaw in progress" signal that
# runtime/noded's paused reclaim probes (src/pause/marker.rs, mirrored here).
# The lock is on an inode the thaw then unlinks, so a later pause's new file is
# never blocked by a stale holder; every locker re-opens by path and checks,
# once locked, that the path still names its descriptor's inode.
_MARKER_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_marker(path):
    try:
        return os.open(path, _MARKER_FLAGS)
    except FileNotFoundError:
        return None


def _names(path, fd):
    """Whether ``path`` names ``fd``'s inode; None once the path is gone."""
    held = os.fstat(fd)
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return None
    return (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)


def marker_thawing(path):
    """A thaw, in any process, holds this marker's flock (observation only)."""
    try:
        fd = _open_marker(path)
    except OSError:
        return False
    if fd is None:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)  # Releases a probe's own shared lock.
    return False


@contextmanager
def thaw_hold(path):
    """Hold LOCK_EX on the marker ``path`` names; yields False when there is none.

    Non-blocking first, then blocking: a probe holds the shared lock for
    microseconds, and thaws exclude each other with the warden flock first.
    Closing the descriptor (never LOCK_UN) releases it, after the unlink.
    """
    while True:
        fd = _open_marker(path)
        if fd is None:
            yield False
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fcntl.flock(fd, fcntl.LOCK_EX)
            names = _names(path, fd)
        except BaseException:
            os.close(fd)
            raise
        if names:
            break
        os.close(fd)  # Replaced by a newer pause (probe that one) or thawed meanwhile.
        if names is None:
            yield False
            return
    try:
        yield True
    finally:
        os.close(fd)
