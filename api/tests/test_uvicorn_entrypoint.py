import json
import sys
from types import SimpleNamespace

from api import uvicorn_entrypoint


def test_wrapper_keeps_import_failure_structured(monkeypatch, capsys):
    monkeypatch.setattr(
        uvicorn_entrypoint,
        "run_server",
        lambda _host, _port: (_ for _ in ()).throw(RuntimeError("secret=must-not-appear")),
    )

    assert uvicorn_entrypoint.main(["--host", "127.0.0.1", "--port", "8765"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "application.launch.failed"
    assert payload["error_type"] == "RuntimeError"
    assert "must-not-appear" not in json.dumps(payload)


def test_wrapper_passes_json_log_config_to_uvicorn(monkeypatch):
    calls = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            calls["app"] = app
            calls["kwargs"] = kwargs

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    monkeypatch.setitem(sys.modules, "api.main", SimpleNamespace(app=object()))
    uvicorn_entrypoint.run_server("127.0.0.1", 8765)
    assert calls["kwargs"] == {
        "host": "127.0.0.1", "port": 8765, "log_config": None,
        "timeout_graceful_shutdown": 30.0,
    }


def test_wrapper_bounds_malformed_or_extreme_shutdown_timeout(monkeypatch):
    calls = {}

    class FakeUvicorn:
        @staticmethod
        def run(_app, **kwargs):
            calls.update(kwargs)

    monkeypatch.setenv("AI_STUDIO_SHUTDOWN_TIMEOUT", "nan")
    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    monkeypatch.setitem(sys.modules, "api.main", SimpleNamespace(app=object()))
    uvicorn_entrypoint.run_server("127.0.0.1", 8765)
    assert calls["timeout_graceful_shutdown"] == 30.0

    monkeypatch.setenv("AI_STUDIO_SHUTDOWN_TIMEOUT", "9999")
    uvicorn_entrypoint.run_server("127.0.0.1", 8765)
    assert calls["timeout_graceful_shutdown"] == 300.0


def test_wrapper_reports_invalid_port_without_plaintext(capsys):
    assert uvicorn_entrypoint.main(["--port", "not-a-port"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "InvalidArguments"
