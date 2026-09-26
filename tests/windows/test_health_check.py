"""Coverage for the notification logic and log scanning in windows/health_check.py.

The checks themselves are thin queries and HTTP calls, exercised against a live
install. What must not regress silently is the decision of *when to tell a
human*: alert on the transition, remind daily while it persists, announce the
recovery, and never re-alert every 15 minutes. That logic is pure and is
pinned here, along with the log scanner's handling of rotation.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[2] / "windows" / "health_check.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("health_check", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module through sys.modules while the class body
    # executes, so register before exec_module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hc = _load()
T0 = dt.datetime(2026, 9, 26, 3, 0, tzinfo=dt.UTC)


def _run(
    previous: dict[str, dict[str, Any]], results: list[Any], now: dt.datetime
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    return hc.decide_notifications(previous, results, now)


def test_first_run_alerts_on_failures_and_stays_quiet_on_success():
    state, notes = _run(
        {}, [hc.Result("api", True, "ok"), hc.Result("probe:x", False, "401")], T0
    )
    assert [(n["kind"], n["id"]) for n in notes] == [("alert", "probe:x")]
    assert state["probe:x"]["notified"] == T0.isoformat()
    assert state["api"]["notified"] is None


def test_persisting_alert_is_not_repeated_within_a_day():
    state, _ = _run({}, [hc.Result("q", False, "err")], T0)
    for minutes in (15, 30, 60 * 23):
        state, notes = _run(
            state, [hc.Result("q", False, "err")], T0 + dt.timedelta(minutes=minutes)
        )
        assert notes == []


def test_persisting_alert_is_reminded_after_a_day():
    state, _ = _run({}, [hc.Result("q", False, "err")], T0)
    state, notes = _run(
        state, [hc.Result("q", False, "err")], T0 + dt.timedelta(hours=24)
    )
    assert [n["kind"] for n in notes] == ["reminder"]
    # and the reminder clock restarts
    _, notes = _run(state, [hc.Result("q", False, "err")], T0 + dt.timedelta(hours=25))
    assert notes == []


def test_recovery_is_announced_once():
    state, _ = _run({}, [hc.Result("q", False, "err")], T0)
    state, notes = _run(
        state, [hc.Result("q", True, "fine")], T0 + dt.timedelta(minutes=15)
    )
    assert [(n["kind"], n["id"]) for n in notes] == [("recovered", "q")]
    _, notes = _run(
        state, [hc.Result("q", True, "fine")], T0 + dt.timedelta(minutes=30)
    )
    assert notes == []


def test_since_tracks_the_last_state_change():
    state, _ = _run({}, [hc.Result("q", False, "err")], T0)
    later = T0 + dt.timedelta(hours=2)
    state, _ = _run(state, [hc.Result("q", False, "err")], later)
    assert state["q"]["since"] == T0.isoformat()
    state, _ = _run(state, [hc.Result("q", True, "ok")], later)
    assert state["q"]["since"] == later.isoformat()


def test_a_check_that_disappears_is_dropped_silently():
    state, _ = _run({}, [hc.Result("probe:old", False, "401")], T0)
    state, notes = _run(
        state, [hc.Result("api", True, "ok")], T0 + dt.timedelta(minutes=15)
    )
    assert "probe:old" not in state
    assert notes == []


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "deriver.log"


PATTERN = re.compile(r"switching from .* to backup", re.IGNORECASE)
SWITCH = "WARNING - Final retry attempt 3/3: switching from openai/model-router to backup openai/x"


def test_scan_log_missing_file(log: Path):
    assert hc.scan_log(log, 123, PATTERN) == ([], 0)


def test_scan_log_reads_only_new_lines(log: Path):
    log.write_text(f"{SWITCH}\nother\n", encoding="utf-8")
    hits, offset = hc.scan_log(log, 0, PATTERN)
    assert len(hits) == 1
    with log.open("a", encoding="utf-8") as f:
        f.write("unrelated\n")
    assert hc.scan_log(log, offset, PATTERN)[0] == []
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{SWITCH}\n")
    assert len(hc.scan_log(log, offset, PATTERN)[0]) == 1


def test_scan_log_restarts_after_rotation(log: Path):
    log.write_text("x" * 1000 + "\n", encoding="utf-8")
    _, offset = hc.scan_log(log, 0, PATTERN)
    log.write_text(f"{SWITCH}\n", encoding="utf-8")  # rotated: now smaller than offset
    hits, _ = hc.scan_log(log, offset, PATTERN)
    assert len(hits) == 1


def test_scan_log_survives_undecodable_bytes(log: Path):
    log.write_bytes(b"\xff\xfe broken " + SWITCH.encode() + b"\n")
    assert len(hc.scan_log(log, 0, PATTERN)[0]) == 1


def test_fallback_pattern_matches_the_real_llm_warning():
    # The exact shape logged by src/llm/runtime.py plan_attempt().
    assert hc.FALLBACK_PATTERN.search(SWITCH)


HOLD = dt.timedelta(hours=6)


def test_log_alert_is_held_through_quiet_windows():
    hit = [hc.Result("fallback-active", False, "3 calls switched")]
    quiet = [hc.Result("fallback-active", True, "no fallback use")]
    results, last = hc.hold_log_alerts(hit, {}, T0, HOLD)
    assert not results[0].ok
    # a quiet 15-minute window inside the hold stays in alert, no flapping
    results, last = hc.hold_log_alerts(quiet, last, T0 + dt.timedelta(minutes=15), HOLD)
    assert not results[0].ok and "clears after" in results[0].detail
    # after the hold with no new hits, it clears and forgets
    results, last = hc.hold_log_alerts(quiet, last, T0 + dt.timedelta(hours=6), HOLD)
    assert results[0].ok and last == {}


def test_a_new_hit_restarts_the_hold():
    hit = [hc.Result("summary-errors", False, "1 failed")]
    quiet = [hc.Result("summary-errors", True, "none")]
    _, last = hc.hold_log_alerts(hit, {}, T0, HOLD)
    _, last = hc.hold_log_alerts(hit, last, T0 + dt.timedelta(hours=5), HOLD)
    results, _ = hc.hold_log_alerts(quiet, last, T0 + dt.timedelta(hours=7), HOLD)
    assert not results[0].ok  # only 2 h since the second hit


def test_check_logs_classifies_each_pattern(tmp_path: Path):
    (tmp_path / "deriver.log").write_text(
        f"{SWITCH}\n"
        + "2026-09-25 - src.utils.summarizer - ERROR - Error generating summary!\n"
        + f"{SWITCH}\n",
        encoding="utf-8",
    )
    results, offsets = hc.check_logs(tmp_path, {})
    by_id = {r.id: r for r in results}
    assert not by_id["fallback-active"].ok
    assert by_id["fallback-active"].detail.startswith("2 call(s)")
    assert not by_id["summary-errors"].ok
    # nothing new since: both clear
    results, _ = hc.check_logs(tmp_path, offsets)
    assert all(r.ok for r in results)
