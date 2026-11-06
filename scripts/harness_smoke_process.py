"""Own and stop subprocess trees started by disposable Harness smokes."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


def spawn_isolated(command: list[str], **kwargs) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        start_new_session=(os.name != "nt"),
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
        **kwargs,
    )


def stop_tree(process: subprocess.Popen[str], *, platform: str | None = None) -> None:
    """Never report a clean smoke shutdown after only its launcher stopped."""
    platform = platform or os.name
    if platform == "nt":
        if process.poll() is not None:
            raise RuntimeError("Harness smoke launcher exited before its child tree could be verified")
        taskkill = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "taskkill.exe"
        try:
            stopped = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
            if stopped.returncode == 0:
                process.wait(timeout=5)
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise RuntimeError("Harness smoke process tree termination was not confirmed")

    # Each launcher has its own POSIX session; stopping the group also covers
    # npm/corepack and model children that outlive the direct parent.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Harness smoke process tree termination was not confirmed") from exc
