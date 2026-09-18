"""Windows entry point for the Honcho API server.

`fastapi run src/main.py` and a plain single-process `uvicorn` both fail on
Windows: psycopg's async implementation cannot run on the ProactorEventLoop,
and uvicorn builds its loop from its own factory rather than from the asyncio
policy, so the usual workarounds do not reach it.

Measured on uvicorn 0.46.0 / Python 3.13.15 / win32, via
`uvicorn.config.Config(...).get_loop_factory()`:

    plain (production, 1 process)  -> ProactorEventLoop    psycopg FAILS
    --loop asyncio                 -> ProactorEventLoop    psycopg FAILS
    workers=1                      -> ProactorEventLoop    psycopg FAILS
    reload=True  (`fastapi dev`)   -> SelectorEventLoop     works
    workers=2                      -> SelectorEventLoop     works

uvicorn's own source explains it (`uvicorn/loops/asyncio.py`)::

    def asyncio_loop_factory(use_subprocess: bool = False):
        if sys.platform == "win32" and not use_subprocess:
            return asyncio.ProactorEventLoop
        return asyncio.SelectorEventLoop

So the selector loop is only reached in subprocess mode. `fastapi dev` works
by accident of `--reload`, and is not a production configuration; running two
workers purely to obtain a working event loop would be a poor trade for a
local single-user server. Hence this launcher: build the loop explicitly and
drive `Server.serve()` on it.

`uvicorn.Config` accepts no `loop_factory` argument, so the loop is supplied
through `asyncio.run(..., loop_factory=...)` rather than
`asyncio.set_event_loop_policy()` -- the policy system is deprecated in 3.14
and slated for removal in 3.16.

Usage (from the repository root)::

    uv run python windows/run_api.py
    uv run python windows/run_api.py --host 127.0.0.1 --port 8000

SECURITY: the default bind is 127.0.0.1 deliberately. Honcho ships with
AUTH_USE_AUTH=false, and the v3 API exposes conclusion read, write and delete
with no credential -- the unauthenticated default is only safe because the
socket is unreachable from off-host. Do not widen the bind without enabling
authentication and issuing scoped keys first. For remote access prefer an SSH
tunnel or a private overlay network, which keep the socket private.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn

# Running this as a plain script puts windows/ on sys.path rather than the
# repository root, so `src` would not be importable. The Scheduled Task
# launcher invokes it by path, so relying on `-m windows.run_api` or on the
# caller's working directory is not an option.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings  # noqa: E402 - must follow the sys.path fix above

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse launcher arguments.

    Args:
        argv: Argument list to parse. Defaults to ``sys.argv[1:]``.

    Returns:
        The parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        prog="run_api.py",
        description="Run the Honcho API server on Windows (selector event loop).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            "Interface to bind. Defaults to %(default)s. "
            "Binding beyond localhost requires enabling authentication first."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Port to bind (default: %(default)s).",
    )
    parser.add_argument(
        "--log-level",
        default=settings.LOG_LEVEL.lower(),
        help="uvicorn log level (default: the configured LOG_LEVEL, %(default)s).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Start the API server.

    Returns:
        A process exit code: 0 on a clean shutdown, 1 on a fatal error.
    """
    args = parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding {args.host} exposes the API beyond this host. Honcho's v3 API allows conclusion read/write/delete without a credential unless AUTH_USE_AUTH is enabled.",
            file=sys.stderr,
        )

    server = uvicorn.Server(
        uvicorn.Config(
            "src.main:app",
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
    )

    try:
        if sys.platform == "win32":
            # The whole reason this file exists -- see the module docstring.
            asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
        else:
            # Nothing here is Windows-specific by necessity; on POSIX defer to
            # uvicorn so this stays a thin shim rather than a second code path.
            server.run()
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - top-level launcher boundary
        import logging

        logging.getLogger(__name__).exception("API server exited with an error")
        # Non-zero so a supervisor can tell a crash from a clean stop.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
