"""Deterministic coverage for the reasoning-traces file lock.

The API server and the deriver both append to REASONING_TRACES_FILE, so writes
must be serialized. `trace_lock_concurrency.py` stress-tests that under real
contention, but a stress test cannot pin down the failure branches: retry
exhaustion, unlock-on-exception, and the caller skipping its write. Those are
covered here.

The Windows branch is exercised on every platform by faking `sys.platform` and
the `msvcrt` module, so a POSIX CI run still covers the msvcrt code path. This
matters because the POSIX branch cannot distinguish a working lock from no lock
at all -- O_APPEND writes to a regular file are atomic on Linux -- so without
these fakes the Windows-specific logic would only ever be checked on a Windows
runner.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import IO, Any, final

import pytest

from src.telemetry import reasoning_traces


def _no_sleep(_seconds: float) -> None:
    """Replace time.sleep so retry backoff does not slow the suite."""
    return None


@final
class FakeLockingModule:
    """Stand-in for `msvcrt`, recording calls and failing on demand.

    Args:
        fail_times: Number of initial acquisition attempts that raise OSError.
            Use a value >= _LOCK_RETRIES to force retry exhaustion.
    """

    LK_NBLCK = 1
    LK_UNLCK = 0

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.attempts = 0
        self.calls: list[tuple[int, int]] = []

    def locking(self, _fd: int, mode: int, nbytes: int) -> None:
        if mode == self.LK_NBLCK:
            self.attempts += 1
            if self.attempts <= self.fail_times:
                raise OSError("lock held by another process")
        self.calls.append((mode, nbytes))

    @property
    def lock_calls(self) -> int:
        return sum(1 for mode, _ in self.calls if mode == self.LK_NBLCK)

    @property
    def unlock_calls(self) -> int:
        return sum(1 for mode, _ in self.calls if mode == self.LK_UNLCK)


@pytest.fixture
def as_windows(monkeypatch: pytest.MonkeyPatch):
    """Run the Windows lock branch regardless of the host platform.

    Returns:
        A factory taking `fail_times` and returning the installed fake module.
    """

    def _install(fail_times: int = 0) -> FakeLockingModule:
        fake = FakeLockingModule(fail_times=fail_times)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(reasoning_traces, "locking_module", fake)
        # Keep the suite fast: the real delay is 100ms per retry.
        monkeypatch.setattr(time, "sleep", _no_sleep)
        return fake

    return _install


@pytest.fixture
def handle(tmp_path: Path):
    """An open append-mode file handle in a temporary directory."""
    path = tmp_path / "traces.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        yield f


def test_acquires_and_releases_on_success(as_windows: Any, handle: IO[str]) -> None:
    fake = as_windows()

    with reasoning_traces._locked(handle) as acquired:  # pyright: ignore[reportPrivateUsage]
        assert acquired is True
        assert fake.unlock_calls == 0, "unlocked while the block was still running"

    assert fake.lock_calls == 1
    assert fake.unlock_calls == 1


def test_releases_when_the_guarded_block_raises(
    as_windows: Any, handle: IO[str]
) -> None:
    """A caller exception must not leak the lock and wedge the other process."""
    fake = as_windows()

    with (
        pytest.raises(ValueError, match="boom"),
        reasoning_traces._locked(handle) as acquired,  # pyright: ignore[reportPrivateUsage]
    ):
        assert acquired is True
        raise ValueError("boom")

    assert fake.unlock_calls == 1


def test_retries_then_succeeds(as_windows: Any, handle: IO[str]) -> None:
    fake = as_windows(fail_times=reasoning_traces._LOCK_RETRIES - 1)  # pyright: ignore[reportPrivateUsage]

    with reasoning_traces._locked(handle) as acquired:  # pyright: ignore[reportPrivateUsage]
        assert acquired is True

    assert fake.attempts == reasoning_traces._LOCK_RETRIES  # pyright: ignore[reportPrivateUsage]
    assert fake.unlock_calls == 1


def test_retry_exhaustion_yields_false_and_warns(
    as_windows: Any, handle: IO[str], caplog: pytest.LogCaptureFixture
) -> None:
    """The invariant: never append unlocked. Exhaustion must yield False."""
    fake = as_windows(fail_times=reasoning_traces._LOCK_RETRIES + 5)  # pyright: ignore[reportPrivateUsage]

    with (
        caplog.at_level(logging.WARNING, logger=reasoning_traces.__name__),
        reasoning_traces._locked(handle) as acquired,  # pyright: ignore[reportPrivateUsage]
    ):
        assert acquired is False, (
            "yielded True without the lock - callers would append unlocked"
        )

    assert fake.attempts == reasoning_traces._LOCK_RETRIES  # pyright: ignore[reportPrivateUsage]
    assert fake.unlock_calls == 0, "released a lock that was never acquired"
    assert any(
        "Could not lock reasoning traces file" in r.message for r in caplog.records
    ), "retry exhaustion was silent"


def test_exhaustion_does_not_raise_into_the_llm_call_path(
    as_windows: Any, handle: IO[str]
) -> None:
    """Tracing is opt-in debugging; it must never take down a live LLM call."""
    as_windows(fail_times=reasoning_traces._LOCK_RETRIES + 1)  # pyright: ignore[reportPrivateUsage]

    with reasoning_traces._locked(handle) as acquired:  # pyright: ignore[reportPrivateUsage]
        assert acquired is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock branch")
def test_posix_branch_locks_and_unlocks(
    monkeypatch: pytest.MonkeyPatch, handle: IO[str]
) -> None:
    """The POSIX branch yields True unconditionally and always unlocks."""
    calls: list[str] = []

    @final
    class FakeFcntl:
        LOCK_EX: int = 2
        LOCK_UN: int = 8

        def flock(self, _fd: int, op: int) -> None:
            calls.append("lock" if op == self.LOCK_EX else "unlock")

    monkeypatch.setattr(reasoning_traces, "locking_module", FakeFcntl())

    with reasoning_traces._locked(handle) as acquired:  # pyright: ignore[reportPrivateUsage]
        assert acquired is True

    assert calls == ["lock", "unlock"]
