"""One server-owned execution budget shared by every stage of an image build."""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Callable, ContextManager, Iterator


DEFAULT_BUILD_EXECUTION_TIMEOUT_SECONDS = 1800.0
_EXECUTION_DEADLINE: ContextVar[float | None] = ContextVar(
    "image_build_execution_deadline", default=None
)
BUILD_CANCELLED_ERROR = "image build cancelled by an operator"


class ImageBuildTimeoutError(RuntimeError):
    """The independent server execution budget expired, not a client wait."""


class ImageBuildCancelledError(ImageBuildTimeoutError):
    """An operator cancelled the build: its budget ends now, so every stage
    that stops on an expired budget stops on a cancellation too."""


class BuildCancellation:
    """One build's cancel flag and the wakeups of the stages blocked on it.

    A stage that blocks (the build CLI's output loop) registers a wakeup; the
    budget check that follows the wakeup raises ImageBuildCancelledError, so a
    cancelled build leaves through the same path as an expired one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._wakeups: dict[object, Callable[[], None]] = {}

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> bool:
        """Set the flag and wake blocked stages; False if already cancelled."""
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            for wake in tuple(self._wakeups.values()):
                wake()
        return True

    @contextmanager
    def wakeup(self, wake: Callable[[], None]) -> Iterator[None]:
        key = object()
        with self._lock:
            self._wakeups[key] = wake
            if self._cancelled:
                wake()
        try:
            yield
        finally:
            # Under the lock: once this returns, cancel() never calls wake again.
            with self._lock:
                self._wakeups.pop(key, None)


_CANCELLATION: ContextVar[BuildCancellation | None] = ContextVar(
    "image_build_cancellation", default=None
)


@contextmanager
def build_cancellation(cancellation: BuildCancellation) -> Iterator[None]:
    """Make budget checks in this execution context honour ``cancellation``."""
    token = _CANCELLATION.set(cancellation)
    try:
        yield
    finally:
        _CANCELLATION.reset(token)


def build_cancellation_wakeup(wake: Callable[[], None]) -> ContextManager[None]:
    """Call ``wake`` if the current build is cancelled while this is open."""
    cancellation = _CANCELLATION.get()
    return nullcontext() if cancellation is None else cancellation.wakeup(wake)


def _positive_seconds(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("build execution timeout must be finite and positive")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("build execution timeout must be finite and positive")
    return float(value)


@contextmanager
def build_execution_deadline(timeout_seconds: float) -> Iterator[None]:
    """Start a budget; nested work cannot extend its parent's deadline.

    The deadline is local to this execution context. Blocking operations must
    consume the remaining budget rather than starting a fresh per-step timeout.
    It does not cancel work merely because a waiting HTTP client disconnects.
    """
    deadline = time.monotonic() + _positive_seconds(timeout_seconds)
    previous = _EXECUTION_DEADLINE.get()
    token = _EXECUTION_DEADLINE.set(
        deadline if previous is None else min(previous, deadline)
    )
    try:
        yield
    finally:
        _EXECUTION_DEADLINE.reset(token)


@contextmanager
def without_build_execution_deadline() -> Iterator[None]:
    """Allow finalizers to use their own finite cleanup timeouts after expiry
    or cancellation."""
    token = _EXECUTION_DEADLINE.set(None)
    cancellation_token = _CANCELLATION.set(None)
    try:
        yield
    finally:
        _CANCELLATION.reset(cancellation_token)
        _EXECUTION_DEADLINE.reset(token)


def remaining_build_execution_seconds(limit: float | None = None) -> float | None:
    """Cap an operation timeout by the build budget, raising once it expires
    or the build is cancelled."""
    maximum = None if limit is None else _positive_seconds(limit)
    cancellation = _CANCELLATION.get()
    if cancellation is not None and cancellation.cancelled:
        raise ImageBuildCancelledError(BUILD_CANCELLED_ERROR)
    deadline = _EXECUTION_DEADLINE.get()
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ImageBuildTimeoutError("image build exceeded its server execution deadline")
    return remaining if maximum is None else min(remaining, maximum)
