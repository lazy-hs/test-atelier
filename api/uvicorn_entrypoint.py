"""Start Uvicorn without allowing import/startup tracebacks into log streams.

The normal production launcher runs migrations first, but a direct local
launch can still fail while importing ``api.main`` (for example when a
session secret is missing).  Uvicorn's CLI prints that traceback as plain
text.  This small wrapper keeps the same JSON startup contract for both
direct and container launches.
"""

from __future__ import annotations

import argparse
import os

from .entrypoint import _log
from .observability import env_float


class _ArgumentParser(argparse.ArgumentParser):
    """Turn malformed operator arguments into the structured launch event."""

    def error(self, message: str) -> None:
        raise ValueError(message)


def run_server(host: str, port: int) -> None:
    """Import the application only after the wrapper is ready to catch errors."""
    import uvicorn

    from .main import app

    # Keep Uvicorn's own lifespan deadline longer than the application's
    # worker shutdown budget.  Otherwise Uvicorn's default 15-second timeout
    # can interrupt the 25-second AI cancellation/drain window and leave jobs
    # in an ambiguous state during a container rollout.
    application_timeout = env_float("AI_STUDIO_SHUTDOWN_TIMEOUT", 25, 0.0)
    graceful_timeout = min(300.0, max(1.0, application_timeout + 5.0))

    # ``log_config=None`` prevents Uvicorn from replacing the application's
    # JSON handlers with its default colorized text configuration.
    uvicorn.run(app, host=host, port=port, log_config=None,
                timeout_graceful_shutdown=graceful_timeout)


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("AI_STUDIO_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=os.getenv("AI_STUDIO_PORT", "8000"))
    try:
        args = parser.parse_args(argv)
    except (argparse.ArgumentError, ValueError) as exc:
        _log("application.launch.failed", "ERROR", component="uvicorn", error_type="InvalidArguments")
        return 2
    except SystemExit as exc:
        # ``-h`` is an intentional human-facing exit; malformed arguments are
        # converted to ValueError by _ArgumentParser.error above.
        return exc.code if isinstance(exc.code, int) else 1
    try:
        run_server(args.host, args.port)
    except KeyboardInterrupt:
        _log("application.stopped", "INFO", component="uvicorn", reason="keyboard_interrupt")
        return 130
    except SystemExit as exc:
        # Preserve a server-provided exit code while keeping an unexpected
        # textual message out of the structured stream.
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            _log("application.launch.failed", "ERROR", component="uvicorn", error_type="SystemExit",
                 returncode=code)
        return code
    except Exception as exc:  # noqa: BLE001 - boundary must protect the log contract
        _log("application.launch.failed", "ERROR", component="uvicorn", error_type=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
