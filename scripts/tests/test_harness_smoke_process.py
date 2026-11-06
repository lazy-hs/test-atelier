import os
import subprocess
import sys
import time

import pytest

from scripts import harness_smoke_process


def test_smoke_shutdown_stops_a_spawned_child_tree(tmp_path):
    heartbeat = tmp_path / "model-heartbeat"
    model = (
        "import time\n"
        f"path = {str(heartbeat)!r}\n"
        "while True:\n"
        " with open(path, 'ab') as target: target.write(b'x')\n"
        " time.sleep(.03)\n"
    )
    launcher = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {model!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "while True: time.sleep(1)\n"
    )
    process = harness_smoke_process.spawn_isolated(
        [sys.executable, "-c", launcher], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and (not heartbeat.exists() or heartbeat.stat().st_size < 3):
            time.sleep(.05)
        assert heartbeat.exists() and heartbeat.stat().st_size >= 3
        harness_smoke_process.stop_tree(process)
        before = heartbeat.stat().st_size
        time.sleep(.25)
        assert heartbeat.stat().st_size == before
    finally:
        if os.name != "nt":
            try:
                harness_smoke_process.stop_tree(process)
            except (OSError, RuntimeError):
                pass
        elif process.poll() is None:
            harness_smoke_process.stop_tree(process)


def test_windows_smoke_kills_the_tree_and_fails_closed(monkeypatch):
    class Process:
        pid = 12345

        def poll(self):
            return None

        def wait(self, timeout):
            assert timeout == 5

    calls = []

    def taskkill(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(harness_smoke_process.subprocess, "run", taskkill)
    harness_smoke_process.stop_tree(Process(), platform="nt")
    assert calls[0][0][-4:] == ["/PID", "12345", "/T", "/F"]
    assert calls[0][1]["timeout"] == 10

    monkeypatch.setattr(harness_smoke_process.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 1))
    with pytest.raises(RuntimeError, match="not confirmed"):
        harness_smoke_process.stop_tree(Process(), platform="nt")

    class Exited(Process):
        def poll(self):
            return 1

    with pytest.raises(RuntimeError, match="exited before"):
        harness_smoke_process.stop_tree(Exited(), platform="nt")
