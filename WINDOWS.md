# Running Honcho natively on Windows

This fork carries a small patch series that lets the Honcho self-hosting stack run
directly on Windows — no WSL, no Docker, no Linux VM.

> **If you are on WSL, Docker, or Linux you do not need this fork.** Use upstream
> [plastic-labs/honcho](https://github.com/plastic-labs/honcho). This exists only for
> running the server natively on a Windows host.

## Why a fork

Upstream declined native Windows support, twice and explicitly:

- [PR #1076](https://github.com/plastic-labs/honcho/pull/1076) — closed unmerged:
  *"we can't afford right now to maintain a native windows implementation of the honcho
  self-hosting stack."*
- [Issue #1075](https://github.com/plastic-labs/honcho/issues/1075) — closed:
  *"We will not support windows as a platform for honcho self-hosters beyond WSL."*

That is a resourcing decision about a support burden, not a judgement on the code, and it is
settled. **Please do not re-open it with them.** This fork exists so the patches stay available
to anyone who needs them.

## What actually breaks on Windows

Seven distinct blockers, all fixed here. Every one was reproduced and measured on
Windows 11 / Python 3.13 / PostgreSQL 18.

| # | Symptom | Cause | Fixed by |
|---|---|---|---|
| 1 | `ModuleNotFoundError: No module named 'fcntl'` | `src/telemetry/reasoning_traces.py` imports `fcntl` at module scope | `msvcrt` byte-range lock behind a `sys.platform` branch |
| 2 | `Error in main:` with a **blank message** | `loop.add_signal_handler()` raises `NotImplementedError`, whose `str()` is empty | `signal.signal()` fallback, bridged onto the loop |
| 3 | `psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop'` | uvicorn builds its loop from its own factory and ignores the asyncio policy | `windows/run_api.py` |
| 4 | `ModuleNotFoundError: No module named 'uvloop'` | `src/deriver/__main__.py` imports uvloop unconditionally | platform-selected loop factory |
| 5 | ~2200 test errors, all `InterfaceError` | pytest-asyncio builds its loop from the asyncio policy | `event_loop_policy` fixture |
| 6 | `OSError: [WinError 10106]` in a test subprocess | a minimal env without `SystemRoot`; Winsock cannot initialise | pass `SystemRoot` through |
| 7 | SDK tests fail with `cleanup is not a function` | the test server thread runs on the Proactor loop, so every request fails | selector loop in `TestServer` |

Blockers 3 and 7 share a root cause with 4: **psycopg's async driver cannot run on
`ProactorEventLoop`, which is Windows' default.** Note that `--loop asyncio` does *not* help —
uvicorn returns `ProactorEventLoop` for any non-subprocess config:

```python
# uvicorn/loops/asyncio.py
def asyncio_loop_factory(use_subprocess: bool = False):
    if sys.platform == "win32" and not use_subprocess:
        return asyncio.ProactorEventLoop
    return asyncio.SelectorEventLoop
```

`fastapi dev` appears to work only because `--reload` implies subprocess mode. It is not a
production configuration, and `fastapi run` fails.

---

## Install

### 1. PostgreSQL and pgvector

```powershell
winget install --id PostgreSQL.PostgreSQL.18
```

A silent install leaves the superuser password at its default on a listening port — **change it
immediately.**

**pgvector is mandatory even if you set `VECTOR_STORE_TYPE=lancedb`**, because the Alembic
migrations run `CREATE EXTENSION vector` unconditionally.

pgvector ships source-only and officially wants MSVC plus `nmake`. To avoid a multi-gigabyte
Visual Studio install, prebuilt Windows binaries exist at
[andreiramani/pgvector_pgsql_windows](https://github.com/andreiramani/pgvector_pgsql_windows).
That is a third-party build which loads inside your database process — decide accordingly.

Copy into the PostgreSQL tree (elevated):

```
lib\vector.dll                     -> C:\Program Files\PostgreSQL\18\lib\
share\extension\*                  -> C:\Program Files\PostgreSQL\18\share\extension\
include\server\extension\vector\*  -> C:\Program Files\PostgreSQL\18\include\server\extension\vector\
```

`CREATE EXTENSION vector` requires **superuser** — run it as `postgres`, not your application
role. Verify:

```sql
SELECT '[1,2,3]'::vector <-> '[4,5,6]'::vector;   -- 5.1962
```

### 2. The application

```powershell
git clone https://github.com/DesarrolloProsis/honcho.git
cd honcho
uv sync
```

Python **3.13+** is required (upstream's floor, not ours).

### 3. Configure

Copy `.env.template` to `.env` and set at minimum `DB_CONNECTION_URI` and your model provider
keys.

> ⚠ **`.env` overrides real environment variables.** `src/config.py` calls
> `load_dotenv(override=True)`, so `$env:FOO = ...` is silently ignored whenever `.env` defines
> `FOO` — the documented `env > .env` precedence is not what happens. To override a single value
> you must set `PYTHON_DOTENV_DISABLED=1` and supply *everything* through the environment.
> Do not write runbook steps that rely on an env-var override.

### 4. Database schema

```powershell
uv run alembic upgrade head
```

### 5. Run it

```powershell
uv run python windows/run_api.py     # API   — NOT `fastapi run`, see blocker 3
uv run python -m src.deriver         # deriver, separate terminal
```

### 6. Run as services

```powershell
.\windows\Install-HonchoTasks.ps1
```

Registers both as logon-triggered Scheduled Tasks, launched through
`windows\start-honcho-hidden.vbs` so no console window appears.

**Stop them with `.\windows\Stop-HonchoService.ps1` — never `Stop-ScheduledTask` alone.**
See [Stopping the service](#stopping-the-service).

---

## Security: the localhost bind is not optional

Honcho ships with `AUTH_USE_AUTH=false`, and the v3 API exposes conclusion **read, write and
delete** with no credential. That default is only safe because the socket is unreachable from
off-host.

**Binding beyond `127.0.0.1` requires enabling `AUTH_USE_AUTH` and issuing scoped keys first.**
Not "consider enabling" — first. `windows/run_api.py` defaults to localhost and warns on a wider
bind.

For remote access, prefer an SSH tunnel or a private overlay network, which keep the socket
private and need no auth change.

---

## Stopping the service

`Stop-ScheduledTask` **does not stop Honcho.** Measured on Windows 11: it terminates only the
task's direct child, reports the task `Ready` with `LastTaskResult 0x41306`, and leaves the
Python process running. The service looks stopped and is not.

This matters during updates: starting the task again after an apparent stop leaves **two
derivers polling the same queue**, the first still holding claimed work.

`MultipleInstances=IgnoreNew` does *not* protect you — it blocks a second *task instance*, and an
orphaned process is not a task instance.

```powershell
.\windows\Stop-HonchoService.ps1
```

attempts `CTRL_BREAK`, then stops the task, then force-kills survivors, then **verifies none
remain** and exits non-zero if any do.

Shutdown behaviour, measured:

| Trigger | Graceful? | Notes |
|---|---|---|
| `Ctrl+C` in a console | **yes** | drains, exits 0 |
| `CTRL_BREAK` to a process **group leader** | **yes** | drains, exits 0 in ~8s |
| `CTRL_BREAK` to a service-launched deriver | **no** | see below |
| `Stop-ScheduledTask` | no | terminates, orphans children |
| Forced termination | no | by definition |

**Be clear about the limit.** `GenerateConsoleCtrlEvent` requires the target PID to be a process
*group leader*. A service-launched deriver is not one — the chain is `wscript → cmd → python`, and
python inherits cmd's group — so for the normal service case the `CTRL_BREAK` is a no-op and the
force-kill does the work. It succeeds only for a console-launched process or one spawned with
`CREATE_NEW_PROCESS_GROUP`.

That is acceptable rather than ideal: force-killing a queue consumer is survivable, because items
it had claimed are reclaimed once their lease expires. The property `Stop-HonchoService.ps1`
guarantees is that the stop is **complete and verified**, not that it was graceful. A successful
run does not mean a clean shutdown.

Windows never delivers `SIGTERM` from another process, so `CTRL_BREAK` is the only external
graceful lever that exists at all. The deriver registers `SIGBREAK` so it works wherever it can.

---

## Operational notes

Things that cost real time to work out.

**Health is a result code, not a state.** `state=Running result=0x41301` is healthy — `0x41301`
means "currently running". `Ready` + `0x1` means it started and died. `0x41303` means it has
never run.

**Set `ExecutionTimeLimit` to zero** or Windows kills the service after three days with no
explanation. `Install-HonchoTasks.ps1` does this.

**A deriver that processes nothing is usually not broken.**
`DERIVER_REPRESENTATION_BATCH_WORK_UNIT_TARGET_TOKENS` (default 512) gates work until enough
tokens accumulate, so small test payloads never trigger a run. Messages stay `processed=false`,
`documents` stays 0, and `queue.error` is NULL. For a low-volume install set
`DERIVER_FLUSH_ENABLED=true`. Diagnose with SQL, since the deriver is silent while gated:

```sql
SELECT processed, count(*) FROM queue GROUP BY processed;
SELECT count(*) FROM documents;
SELECT left(error, 200) FROM queue WHERE error IS NOT NULL LIMIT 3;
```

**Non-ASCII usernames break Scheduled Task arguments.** On a profile such as
`C:\Users\Renée`, an inline command passed to `New-ScheduledTaskAction -Argument` is mangled
during registration (`Ren?e`). The task then fails instantly with `LastTaskResult = 0x1` and
produces **no log output at all**, because the shell never resolves the path. The launcher here
derives every path from its own location, which avoids the problem.

**Save the `.vbs` as ANSI, never UTF-8.** `wscript` parses `.vbs` as ANSI. A UTF-8 file
containing any non-ASCII byte is silently corrupted, `wscript` exits **0**, and nothing launches.
`start-honcho-hidden.vbs` is pure ASCII to sidestep this.

**Restoring a backup needs care.** Create the database, `CREATE EXTENSION vector` **as
superuser**, then `pg_restore --no-owner` — and do **not** pass `--role`. `--role` issues
`SET ROLE`, the application role cannot create extensions, and every table with a vector column
fails. The failure is quiet: `pg_restore` reports "errors ignored" and exits 1 with
`alembic_version` restored and no data.

**A rollback tag restores code, not schema.** `alembic upgrade head` is not reversed by checking
out an older commit. Back up the database before any version jump.

**Non-OpenAI endpoints.** Azure model-router speaks the OpenAI schema — use
`transport = "openai"` plus a per-module base-URL override. Verify three things before
configuring, because a model appearing in `/models` does not mean it is deployed:

```
POST {BASE}/chat/completions   {"model": "...", ...}
POST {BASE}/chat/completions   with "tools":[...]     # tool calling is REQUIRED
POST {BASE}/embeddings         {"model": "..."}       # check the dimension count
```

The auth header is `api-key`, not `Authorization: Bearer`. **Entra ID / OAuth token auth does not
work** — Honcho accepts only a static `LLM_*_API_KEY` and has no refresh hook.

---

## Staying current

This fork rebases its patch series onto upstream **release tags**, not `main`. Upstream tags
roughly weekly; `main` is a rolling integration branch.

```powershell
git fetch upstream --tags
git log --oneline $(git describe --tags --abbrev=0)..upstream/main   # what's new
```

To see the current patch series and its base:

```powershell
git log --oneline $(git merge-base HEAD upstream/main)..HEAD
```

The update loop:

1. Decide whether the release is worth taking. There is no security urgency on a single-user
   self-hosted backend — update for a fix or feature you want.
2. Check `migrations/` **and** `.python-version` / `requires-python`. A release can move the
   Python floor, which is a runtime migration, not just a rebase.
3. Back up the database. **Stop the services first** (`Stop-HonchoService.ps1`), or the backup
   and its verification will not agree — a running deriver changes data underneath you.
4. Rebase onto the new tag. Verify with `git range-diff` that every patch replayed faithfully.
5. Run the checks below, then restart.

Do not let the gap exceed two or three releases: `src/deriver/queue_manager.py` sees regular
upstream churn, and conflict cost grows with distance.

### Verifying a rebase

```powershell
uv sync --all-extras --dev
uv run pytest -q -n auto
uv run ruff check src tests scripts migrations sdks/python sandbox
uv run basedpyright
uv run python trace_lock_concurrency.py        # must PASS on win32
uv run python -m src.deriver                   # must start; Ctrl+C must exit cleanly
```

`trace_lock_concurrency.py` is the discriminating check for the file lock. On Linux it passes
even with the lock removed entirely, because `O_APPEND` appends are atomic there — only the
Windows run proves the `msvcrt` lock is doing real work.

The `Windows Native` CI workflow runs the same checks on every push, and additionally pins the
assumptions this fork depends on — if a future uvicorn stops returning `ProactorEventLoop` for a
plain config, that job fails loudly and `windows/run_api.py` can be reconsidered rather than
lingering unexamined.

---

## What's in `windows/`

| File | Purpose |
|---|---|
| `run_api.py` | API entry point on the selector event loop (blocker 3) |
| `start-honcho-hidden.vbs` | Console-free launcher for Scheduled Tasks |
| `Install-HonchoTasks.ps1` | Register/unregister both services |
| `Stop-HonchoService.ps1` | Stop them **and verify** they actually stopped |

Every script defaults its install directory to its own location, so a clone works unedited.
