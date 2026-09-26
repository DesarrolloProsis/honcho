"""Periodic health check for a self-hosted Honcho install.

Run every 15 minutes by ``windows\\Check-HonchoHealth.ps1`` (the "Honcho Health
Check" task), which turns its notifications into desktop toasts.

Why this exists: on 2026-09-25 the model provider started rejecting Honcho's
key. Every layer handled that "correctly" in isolation -- the LLM layer retried
and logged a warning, the queue marked each item processed-with-error, the
reconciler marked embeddings ``failed`` after its budget, the summarizer
swallowed the exception -- while reads kept working. Nothing told a human for
36 hours, and every message in that window was silently left un-derived.

The checks look for exactly those end states, plus active probes of every
configured provider endpoint, so a dead key is caught within the hour even when
no messages are flowing:

    api             the API answers
    queue-errors    queue items that finished with an error in the last hour
    queue-stuck     unprocessed deriver work older than 2 hours
    embed-failed    embeddings in the terminal ``failed`` state
    embed-backlog   embeddings pending for more than an hour
    fallback-active a model call switched to its backup since the last run.
                    Running on the backup is an alert, not a success: it is
                    the last margin before the next outage.
    summary-errors  a session summary failed; the summarizer skips it and the
                    queue records success, so only the log shows it
    probe:*         one tiny call to each configured endpoint, hourly. Embedding
                    endpoints must also reproduce a stored conclusion's
                    vector (cosine > 0.99), proving the same model

A notification is raised when a check changes state (alert or recovery), plus
a daily reminder while it stays in alert, so a persistent problem is neither
silent nor spammed every 15 minutes.

Database access is read-only (the session is set READ ONLY before querying).
No secret is ever printed; endpoints are identified by host and model.

Usage (from the install root)::

    .venv\\Scripts\\python.exe windows\\health_check.py
    .venv\\Scripts\\python.exe windows\\health_check.py --force-probe

The last line of output is ``HEALTH_JSON {...}`` with the notifications to
raise; every other line is for the log.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Invoked by path, so put the install root (not windows/) on sys.path, as
# run_api.py does.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REMINDER_INTERVAL = dt.timedelta(hours=24)
PROBE_INTERVAL = dt.timedelta(hours=1)
QUEUE_STUCK_AFTER = dt.timedelta(hours=2)
EMBED_BACKLOG_AFTER = dt.timedelta(hours=1)
FALLBACK_PATTERN = re.compile(r"switching from .* to backup", re.IGNORECASE)
# The summarizer catches a model failure, logs this, and skips saving the
# summary; the queue item still counts as processed without error, so the
# log line is the only trace. (src/utils/summarizer.py, _create_summary)
SUMMARY_ERROR_PATTERN = re.compile(r"Error generating summary")
LOG_FILES = ("api.log", "deriver.log")
# check id -> (pattern, message when it matched since the last run, message when clean)
LOG_CHECKS: dict[str, tuple[re.Pattern[str], str, str]] = {
    "fallback-active": (
        FALLBACK_PATTERN,
        "{n} call(s) switched to the backup since the last check; the primary is failing",
        "no fallback use",
    ),
    "summary-errors": (
        SUMMARY_ERROR_PATTERN,
        "{n} session summary(ies) failed and were skipped since the last check",
        "no summary errors",
    ),
}


@dataclass
class Result:
    """Outcome of one check."""

    id: str
    ok: bool
    detail: str


# --- notification logic (pure; unit-tested) ----------------------------------


def decide_notifications(
    previous: dict[str, dict[str, Any]],
    results: list[Result],
    now: dt.datetime,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Compare this run with the last one and decide what to tell a human.

    Args:
        previous: Per-check state from the last run, keyed by check id.
        results: This run's results.
        now: Current time, timezone-aware.

    Returns:
        The new per-check state and the notifications to raise. A check that
        no longer appears (for example an endpoint removed from config) is
        dropped silently.
    """
    state: dict[str, dict[str, Any]] = {}
    notes: list[dict[str, str]] = []
    for r in results:
        prev = previous.get(r.id)
        was_ok = prev is None or prev.get("ok", True)
        entry: dict[str, Any] = {
            "ok": r.ok,
            "detail": r.detail,
            "since": now.isoformat()
            if prev is None or prev.get("ok") != r.ok
            else prev.get("since", now.isoformat()),
            "notified": prev.get("notified") if prev else None,
        }
        if not r.ok and was_ok:
            notes.append({"kind": "alert", "id": r.id, "text": r.detail})
            entry["notified"] = now.isoformat()
        elif not r.ok:
            last = entry["notified"]
            if (
                last is None
                or now - dt.datetime.fromisoformat(last) >= REMINDER_INTERVAL
            ):
                notes.append({"kind": "reminder", "id": r.id, "text": r.detail})
                entry["notified"] = now.isoformat()
        elif prev is not None and not prev.get("ok", True):
            notes.append({"kind": "recovered", "id": r.id, "text": r.detail})
            entry["notified"] = None
        state[r.id] = entry
    return state, notes


def scan_log(
    path: Path, offset: int, pattern: re.Pattern[str]
) -> tuple[list[str], int]:
    """Return lines matching ``pattern`` appended to ``path`` since ``offset``.

    A file smaller than ``offset`` was rotated or truncated, so it is read from
    the start.
    """
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if size < offset:
        offset = 0
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    return [ln for ln in text.splitlines() if pattern.search(ln)], size


# --- checks ------------------------------------------------------------------


def check_api(url: str) -> Result:
    req = urllib.request.Request(
        url.rstrip("/") + "/v3/workspaces/list",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed local URL
            return Result("api", resp.status == 200, f"HTTP {resp.status}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Result("api", False, f"API unreachable: {e}")


def check_database() -> list[Result]:
    from sqlalchemy import create_engine, text

    from src.config import settings

    engine = create_engine(settings.DB.CONNECTION_URI, pool_pre_ping=True)
    q = {
        "queue_errors": """
            select count(*) from queue
            where error is not null and created_at > now() - interval '1 hour'
        """,
        "queue_stuck": """
            select extract(epoch from now() - min(created_at)) from queue
            where not processed and task_type in ('representation', 'summary')
        """,
        "failed": """
            select (select count(*) from message_embeddings where sync_state = 'failed')
                 + (select count(*) from documents
                    where sync_state = 'failed' and deleted_at is null)
        """,
        "backlog": """
            select extract(epoch from now() - min(created_at)) from (
                select created_at from message_embeddings where sync_state = 'pending'
                union all
                select created_at from documents
                where sync_state = 'pending' and deleted_at is null
            ) p
        """,
    }
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            v = {k: conn.execute(text(sql)).scalar() for k, sql in q.items()}
            conn.rollback()
    finally:
        engine.dispose()

    stuck_s = float(v["queue_stuck"] or 0)
    backlog_s = float(v["backlog"] or 0)
    return [
        Result(
            "queue-errors",
            not v["queue_errors"],
            f"{v['queue_errors']} queue item(s) failed in the last hour; see logs\\deriver.log",
        ),
        Result(
            "queue-stuck",
            stuck_s < QUEUE_STUCK_AFTER.total_seconds(),
            f"oldest unprocessed deriver item is {stuck_s / 3600:.1f} h old",
        ),
        Result(
            "embed-failed",
            not v["failed"],
            f"{v['failed']} embedding(s) permanently failed; they will not retry on their own",
        ),
        Result(
            "embed-backlog",
            backlog_s < EMBED_BACKLOG_AFTER.total_seconds(),
            f"oldest pending embedding is {backlog_s / 3600:.1f} h old",
        ),
    ]


def check_logs(
    logs_dir: Path, offsets: dict[str, int]
) -> tuple[list[Result], dict[str, int]]:
    """Run every LOG_CHECKS pattern over the log lines written since the last run."""
    combined = re.compile(
        "|".join(f"(?:{p.pattern})" for p, _, _ in LOG_CHECKS.values())
    )
    hits: list[str] = []
    new_offsets: dict[str, int] = {}
    for name in LOG_FILES:
        found, new_offsets[name] = scan_log(
            logs_dir / name, offsets.get(name, 0), combined
        )
        hits.extend(found)
    results: list[Result] = []
    for check_id, (pattern, bad, good) in LOG_CHECKS.items():
        n = sum(1 for line in hits if pattern.search(line))
        results.append(Result(check_id, n == 0, bad.format(n=n) if n else good))
    return results, new_offsets


def configured_endpoints() -> list[dict[str, Any]]:
    """Every distinct (kind, transport, model, base_url, key) Honcho is configured to use."""
    from src.config import (
        resolve_embedding_model_config,
        resolve_model_config,
        settings,
    )

    configured = [
        settings.DERIVER.MODEL_CONFIG,
        settings.SUMMARY.MODEL_CONFIG,
        *[level.MODEL_CONFIG for level in settings.DIALECTIC.LEVELS.values()],
        settings.DREAM.DEDUCTION_MODEL_CONFIG,
        settings.DREAM.INDUCTION_MODEL_CONFIG,
    ]

    def chat(model: str, transport: str, base_url: str | None, key: str | None):
        # Resolve exactly as src/llm/registry.py client_for_model_config does:
        # a config without its own key uses the transport's global key, and
        # one without a base URL uses the global base URL. Probing with
        # anything else would test a client Honcho never builds.
        if transport == "openai":
            key = key or settings.LLM.OPENAI_API_KEY
            base_url = base_url or settings.LLM.OPENAI_BASE_URL
        return {
            "kind": "chat",
            "transport": transport,
            "model": model,
            "base_url": base_url,
            "api_key": key,
        }

    raw: list[dict[str, Any]] = []
    emb = resolve_embedding_model_config(settings.EMBEDDING.MODEL_CONFIG)
    for e in (emb, emb.fallback):
        if e is not None:
            raw.append(
                {
                    "kind": "embedding",
                    "transport": e.transport,
                    "model": e.model,
                    "base_url": e.base_url,
                    "api_key": e.api_key,
                }
            )
    for c in configured:
        mc = resolve_model_config(c)
        raw.append(chat(mc.model, mc.transport, mc.base_url, mc.api_key))
        if mc.fallback is not None:
            fb = mc.fallback
            raw.append(chat(fb.model, fb.transport, fb.base_url, fb.api_key))

    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for e in raw:
        key_id = hashlib.sha256((e["api_key"] or "").encode()).hexdigest()[:8]
        ident = (e["kind"], e["transport"], e["model"], e["base_url"], key_id)
        if ident in seen:
            continue
        seen.add(ident)
        host = urlparse(e["base_url"] or "").hostname or "default"
        e["id"] = f"probe:{e['kind']}:{host}/{e['model']}"
        out.append(e)
    return out


def reference_vector() -> tuple[str, list[float]] | None:
    """One stored conclusion and its vector, to prove an embedding endpoint's identity."""
    from sqlalchemy import create_engine, text

    from src.config import settings

    engine = create_engine(settings.DB.CONNECTION_URI, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            row = conn.execute(
                text("""
                    select content, embedding::text from documents
                    where sync_state = 'synced' and deleted_at is null
                      and embedding is not null and length(content) between 80 and 400
                    order by id limit 1
                """)
            ).first()
            conn.rollback()
    finally:
        engine.dispose()
    if row is None:
        return None
    return str(row[0]), [float(x) for x in str(row[1]).strip("[]").split(",")]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / ((sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5))


def probe(
    endpoint: dict[str, Any], reference: tuple[str, list[float]] | None = None
) -> Result:
    """One minimal call. Auth, missing deployment, and connection errors fail it.

    An embedding endpoint is also checked for IDENTITY: it embeds a stored
    conclusion and must reproduce the stored vector (cosine > 0.99). A
    different model behind the same name would otherwise write vectors from
    another space into the index, silently breaking search.
    """
    import openai

    if endpoint["transport"] != "openai":
        return Result(
            endpoint["id"], True, f"skipped: transport {endpoint['transport']}"
        )
    if not endpoint["api_key"]:
        return Result(
            endpoint["id"], False, f"{endpoint['model']}: no API key configured"
        )
    client = openai.OpenAI(
        api_key=endpoint["api_key"],
        base_url=endpoint["base_url"],
        timeout=30,
        max_retries=0,
    )
    try:
        if endpoint["kind"] == "embedding":
            text_in = reference[0] if reference else "health check"
            resp = client.embeddings.create(
                model=endpoint["model"], input=text_in, encoding_format="float"
            )
            if reference is not None:
                fresh = list(resp.data[0].embedding)
                model = endpoint["model"]
                if len(fresh) != len(reference[1]):
                    detail = f"{model} returned {len(fresh)} dims, index has {len(reference[1])}"
                    return Result(endpoint["id"], False, detail + ": different model")
                sim = cosine(fresh, reference[1])
                if sim < 0.99:
                    detail = (
                        f"{model} vectors do not match the index (cosine {sim:.4f})"
                    )
                    return Result(
                        endpoint["id"],
                        False,
                        detail + ": different model behind this name",
                    )
                return Result(
                    endpoint["id"], True, f"responding, same vector space ({sim:.6f})"
                )
        else:
            client.chat.completions.create(
                model=endpoint["model"],
                messages=[{"role": "user", "content": "Reply with OK."}],
                max_completion_tokens=16,
            )
        return Result(endpoint["id"], True, "responding")
    except openai.BadRequestError as e:
        # Reached and authenticated; the probe's own parameters were refused.
        return Result(
            endpoint["id"], True, f"reachable, probe rejected: {e.status_code}"
        )
    except openai.APIStatusError as e:
        return Result(
            endpoint["id"], False, f"{endpoint['model']} returned HTTP {e.status_code}"
        )
    except openai.APIConnectionError as e:
        return Result(endpoint["id"], False, f"{endpoint['model']} unreachable: {e}")


# --- main --------------------------------------------------------------------


def run(install_dir: Path, api_url: str, force_probe: bool) -> dict[str, Any]:
    logs = install_dir / "logs"
    state_path = logs / "health-state.json"
    try:
        state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    now = dt.datetime.now(dt.UTC)

    results = [check_api(api_url)]
    try:
        results.extend(check_database())
    except Exception as e:  # noqa: BLE001 - reported as a check, not a crash
        results.append(
            Result("database", False, f"health queries failed: {type(e).__name__}: {e}")
        )
    log_results, state["log_offsets"] = check_logs(logs, state.get("log_offsets", {}))
    results.extend(log_results)

    last_probe = state.get("last_probe")
    due = (
        force_probe
        or last_probe is None
        or (now - dt.datetime.fromisoformat(last_probe) >= PROBE_INTERVAL)
    )
    if due:
        endpoints = configured_endpoints()
        reference = None
        if any(e["kind"] == "embedding" for e in endpoints):
            try:
                reference = reference_vector()
            except Exception as e:  # noqa: BLE001 - probes still run, just without identity
                print(f"[!] no reference vector for the identity probe: {e}")
        probes = [probe(e, reference) for e in endpoints]
        state["probe_results"] = [r.__dict__ for r in probes]
        state["last_probe"] = now.isoformat()
    else:
        probes = [Result(**r) for r in state.get("probe_results", [])]
    results.extend(probes)

    state["checks"], notes = decide_notifications(state.get("checks", {}), results, now)
    state["last_run"] = now.isoformat()
    logs.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    for r in results:
        print(f"[{'OK' if r.ok else 'ALERT'}] {r.id}: {r.detail}")
    return {
        "notifications": notes,
        "alerts": sum(not r.ok for r in results),
        "probed": due,
    }


def main(argv: list[str] | None = None) -> int:
    # No --install-dir on purpose: src.config locates .env relative to the code
    # it imports, so a copy of this script checks the install it lives in,
    # whatever it is pointed at. An option would only let the logs and the
    # settings disagree (a dev checkout reporting on the live database's logs).
    parser = argparse.ArgumentParser(description="Honcho health check")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--force-probe", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run(ROOT, args.api_url, args.force_probe)
    except Exception:  # noqa: BLE001 - the wrapper turns exit 1 into a notification
        traceback.print_exc()
        return 1
    print("HEALTH_JSON " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
