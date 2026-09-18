"""Concurrency check for the reasoning-traces file lock.

Spawns N processes that each append M JSONL records to one file through
src.telemetry.reasoning_traces._locked, then verifies every record arrived
intact.

    python trace_lock_concurrency.py
    python trace_lock_concurrency.py --processes 6 --writes 40

PASS requires actual lines == processes * writes with 0 malformed records.

  * Fewer lines than expected: a writer exhausted its lock-retry budget and
    dropped its trace (the _locked failure path). Widen the retry budget.
  * Malformed lines: two writers interleaved, i.e. the lock did not hold.

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


def load_locked():
    """Import the real reasoning_traces module standalone.

    src.config pulls in application settings and pydantic is only needed for an
    isinstance check elsewhere in the module, so both are stubbed. _locked itself
    is imported unmodified — this test exercises the shipped code, not a copy.
    """
    for name in ("src", "src.config", "pydantic"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.__getattr__ = lambda _attr: object  # type: ignore[attr-defined]
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


def worker(path: str, writes: int, payload_bytes: int) -> None:
    module = load_locked()
    payload = "x" * payload_bytes
    for i in range(writes):
        with open(path, "a") as f, module._locked(f) as acquired:
            if acquired:
                f.write(json.dumps({"pid": os.getpid(), "i": i, "pad": payload}) + "\n")
                f.flush()


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
            mp.Process(target=worker, args=(path, args.writes, args.payload_bytes))
            for _ in range(args.processes)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()

        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()

        malformed = 0
        for line in lines:
            try:
                json.loads(line)
            except json.JSONDecodeError:
                malformed += 1

        ok = len(lines) == expected and malformed == 0
        print(f"platform         : {sys.platform}")
        print(f"processes        : {args.processes}")
        print(f"writes each      : {args.writes}")
        print(f"record payload   : {args.payload_bytes} bytes")
        print(f"expected records : {expected}")
        print(f"actual lines     : {len(lines)}")
        print(f"malformed records: {malformed}")
        print(f"RESULT           : {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        os.unlink(path)


if __name__ == "__main__":
    mp.freeze_support()  # Windows spawn-start safety
    raise SystemExit(main())
