"""Concurrency check for the reasoning-traces file lock.

Spawns N processes that each append M JSONL records to one file through
src.telemetry.reasoning_traces._locked, then verifies every record arrived
intact.

    python trace_lock_concurrency.py
    python trace_lock_concurrency.py --processes 6 --writes 40

Each record carries (worker, sequence), so PASS requires the exact expected set
-- not merely the expected count. A line count alone cannot tell a duplicate
masking a dropped record from a correct run.

PASS requires all of:

  * actual lines == processes * writes
  * 0 malformed lines      -- two writers interleaved; the lock did not hold
  * 0 missing records      -- a writer exhausted its retry budget and dropped a
                              trace (the _locked failure path); widen the budget
  * 0 duplicate records    -- the same (worker, sequence) written twice
  * 0 unexpected records   -- stale file contents, or a payload bug
  * every worker exit 0    -- a worker exits 1 if it skipped any write, so a
                              partially-failed writer is visible even when
                              another writer's duplicate keeps the count right

This is a contention test. The deterministic failure branches -- retry
exhaustion, unlock-on-exception, the warning, and the caller skipping its
write -- are covered in tests/telemetry/test_reasoning_traces_lock.py, which
fakes the locking module so the Windows path is exercised on any platform.

Platform caveat, measured rather than assumed: on Linux this test passes even
with the flock removed entirely, at payloads from 200 B to 512 KB. O_APPEND
writes to a regular file are atomic there, so the POSIX run cannot distinguish
a working lock from no lock at all -- it is a no-regression check only. The
discriminating run is Windows, where the msvcrt byte-range lock is doing real
work; that is the run whose output is worth reporting.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing as mp
import os
import sys
import tempfile
import types
from pathlib import Path

MODULE_PATH = Path("src/telemetry/reasoning_traces.py")
DEFAULT_PAYLOAD_BYTES = 4096


def _sample(items: list[tuple[int, int]], limit: int = 5) -> str:
    """Render a short sample of offending keys, or nothing when there are none."""
    return f"  e.g. {items[:limit]}" if items else ""


def load_locked():
    """Import the real reasoning_traces module standalone.

    src.config pulls in application settings and pydantic is only needed for an
    isinstance check elsewhere in the module, so both are stubbed. _locked itself
    is imported unmodified — this test exercises the shipped code, not a copy.
    """
    for name in ("src", "src.config", "pydantic"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.__getattr__ = lambda name: object  # noqa: ARG005
            sys.modules[name] = stub

    spec = importlib.util.spec_from_file_location(
        "reasoning_traces_under_test", MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "_locked"):
        raise SystemExit("_locked not found in module")
    return module


def worker(path: str, worker_id: int, writes: int, payload_bytes: int) -> None:
    """Append `writes` identifiable records through the shipped lock.

    Each record carries (worker_id, sequence) so the parent can check exact set
    membership rather than only counting lines -- a duplicate masking a dropped
    record keeps the count correct while the data is wrong.

    Exits non-zero if any write was skipped, so a writer that exhausted its
    lock-retry budget is visible in the process exit code and not only in the
    line count.
    """
    module = load_locked()
    payload = "x" * payload_bytes
    skipped = 0
    for i in range(writes):
        with open(path, "a") as f, module._locked(f) as acquired:
            if acquired:
                record = {"w": worker_id, "i": i, "pid": os.getpid(), "pad": payload}
                f.write(json.dumps(record) + "\n")
                f.flush()
            else:
                skipped += 1
    raise SystemExit(1 if skipped else 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--processes", type=int, default=6)
    parser.add_argument("--writes", type=int, default=40)
    parser.add_argument("--payload-bytes", type=int, default=DEFAULT_PAYLOAD_BYTES)
    args = parser.parse_args()

    if not MODULE_PATH.exists():
        raise SystemExit(f"run this from the repo root; {MODULE_PATH} not found")

    expected = args.processes * args.writes
    fd, path = tempfile.mkstemp(prefix="trace_lock_", suffix=".jsonl")
    os.close(fd)

    try:
        procs = [
            mp.Process(target=worker, args=(path, w, args.writes, args.payload_bytes))
            for w in range(args.processes)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()

        bad_exits = [(i, p.exitcode) for i, p in enumerate(procs) if p.exitcode != 0]

        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()

        malformed = 0
        seen: dict[tuple[int, int], int] = {}
        for line in lines:
            try:
                rec = json.loads(line)
                key = (rec["w"], rec["i"])
            except (json.JSONDecodeError, KeyError, TypeError):
                malformed += 1
                continue
            seen[key] = seen.get(key, 0) + 1

        want = {(w, i) for w in range(args.processes) for i in range(args.writes)}
        missing = sorted(want - set(seen))
        duplicated = sorted(k for k, n in seen.items() if n > 1)
        unexpected = sorted(set(seen) - want)

        ok = (
            len(lines) == expected
            and malformed == 0
            and not missing
            and not duplicated
            and not unexpected
            and not bad_exits
        )

        print(f"platform          : {sys.platform}")
        print(f"processes         : {args.processes}")
        print(f"writes each       : {args.writes}")
        print(f"record payload    : {args.payload_bytes} bytes")
        print(f"expected records  : {expected}")
        print(f"actual lines      : {len(lines)}")
        print(f"malformed records : {malformed}")
        print(f"missing records   : {len(missing)}{_sample(missing)}")
        print(f"duplicate records : {len(duplicated)}{_sample(duplicated)}")
        print(f"unexpected records: {len(unexpected)}{_sample(unexpected)}")
        print(f"worker exit codes : {bad_exits if bad_exits else 'all 0'}")
        print(f"RESULT            : {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        os.unlink(path)


if __name__ == "__main__":
    mp.freeze_support()  # Windows spawn-start safety
    raise SystemExit(main())
