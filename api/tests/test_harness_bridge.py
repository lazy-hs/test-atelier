import json
import os

import pytest
import httpx

from api.harness_bridge import HarnessBridge, HarnessBridgeConfig, HarnessBridgeError, HarnessBridgeHTTPError, HarnessBridgeUnavailable, create_scope_token


class _Response:
    def __init__(self, payload=None, error=None, status_code=200):
        self.payload = payload
        self.error = error
        self.status_code = status_code
        self.content = json.dumps(payload).encode() if payload is not None else b""
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self.error:
            raise self.error
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def iter_raw(self):
        if self.error:
            raise self.error
        for offset in range(0, len(self.content), 64 * 1024):
            yield self.content[offset:offset + 64 * 1024]


class _Client:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, *_args, **_kwargs):
        return self.response

    def request(self, *_args, **_kwargs):
        return self.response

    def stream(self, *_args, **_kwargs):
        return self.response

    def close(self):
        return None


class _StreamResponse:
    status_code = 200
    is_success = True

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self):
        yield from self.lines

    def iter_raw(self):
        for line in self.lines:
            yield line.encode("utf-8") + b"\n"


def test_scope_token_is_short_lived_and_signed(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_SESSION_SECRET", "x" * 32)
    token = create_scope_token({"project_id": 4, "task_id": "task-1", "allowed_operation": "task.events"})
    assert token.startswith("v1.")
    assert len(token.rsplit(".", 1)[-1]) == 64


def test_scope_token_prefers_dedicated_bridge_secret(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("TEST_ATELIER_SCOPE_SECRET", "y" * 32)
    dedicated = create_scope_token({"project_id": 4})
    monkeypatch.setenv("TEST_ATELIER_SCOPE_SECRET", "z" * 32)
    rotated = create_scope_token({"project_id": 4})
    assert dedicated != rotated


def test_bridge_reads_host_token_file_and_rejects_conflicting_inline_value(monkeypatch, tmp_path):
    path = tmp_path / "host-token"
    path.write_bytes(b"h" * 24 + b"\r\n")
    monkeypatch.setenv("TEST_ATELIER_HARNESS_TOKEN_FILE", str(path))
    monkeypatch.delenv("TEST_ATELIER_HARNESS_TOKEN", raising=False)
    assert HarnessBridgeConfig.from_env().token == "h" * 24
    monkeypatch.setenv("TEST_ATELIER_HARNESS_TOKEN", "another-host-token")
    with pytest.raises(ValueError):
        HarnessBridgeConfig.from_env()


def test_scope_token_uses_file_secret_without_inline_compose_value(monkeypatch, tmp_path):
    secret_file = tmp_path / "scope-key"
    secret_file.write_bytes(b"f" * 40 + b"\r\n")
    monkeypatch.delenv("TEST_ATELIER_SCOPE_SECRET", raising=False)
    monkeypatch.setenv("TEST_ATELIER_SCOPE_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("AI_STUDIO_SESSION_SECRET", "x" * 40)
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    headers = bridge._request_id_headers("req-file", {"task_id": "task-file"})
    assert headers["X-Test-Atelier-Scope"].startswith("v1.")
    with pytest.raises(HarnessBridgeUnavailable, match="密钥文件不可用"):
        monkeypatch.setenv("TEST_ATELIER_SCOPE_SECRET", "inline-secret")
        bridge._request_id_headers("req-file", {"task_id": "task-file"})


def test_scope_token_requires_session_secret(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_SESSION_SECRET", "short")
    monkeypatch.delenv("TEST_ATELIER_SCOPE_SECRET", raising=False)
    with pytest.raises(HarnessBridgeUnavailable):
        create_scope_token({"project_id": 4})


def test_configured_bridge_rejects_missing_dedicated_scope_secret(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_SESSION_SECRET", "x" * 32)
    monkeypatch.delenv("TEST_ATELIER_SCOPE_SECRET", raising=False)
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    with pytest.raises(HarnessBridgeUnavailable, match="独立作用域密钥"):
        bridge._request_id_headers("request-1", {"task_id": "task-1"})


def test_bridge_config_bounds_timeout(monkeypatch):
    monkeypatch.setenv("TEST_ATELIER_HARNESS_TIMEOUT", "9999")
    config = HarnessBridgeConfig.from_env()
    assert config.timeout_seconds == 300


def test_health_reads_host_payload(monkeypatch):
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    monkeypatch.setattr(bridge, "_client", lambda: _Client(_Response({"status": "ok", "service": "test-atelier-harness-host"})))
    assert bridge.health()["status"] == "ok"


def test_health_streams_httpx_response_before_bounded_decode():
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"status": "ok"}))
    monkeypatch_client = lambda: httpx.Client(transport=transport)
    bridge._client = monkeypatch_client
    assert bridge.health() == {"status": "ok"}


def test_health_maps_connection_failure_to_host_unavailable(monkeypatch):
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    monkeypatch.setattr(bridge, "_client", lambda: _Client(_Response(error=httpx.ConnectError("down"))))
    from api.harness_bridge import HarnessBridgeHostUnavailable
    with pytest.raises(HarnessBridgeHostUnavailable):
        bridge.health()


@pytest.mark.parametrize("url", [
    "ftp://harness.internal",
    "http://user:password@harness.internal",
    "http://harness.internal/?token=secret",
    "http://harness.internal/#secret",
])
def test_bridge_rejects_credentialed_or_non_http_target(url):
    bridge = HarnessBridge(HarnessBridgeConfig(url, "", 2))
    with pytest.raises(HarnessBridgeUnavailable, match="无凭据"):
        bridge._client()


def test_bridge_rejects_malformed_target():
    bridge = HarnessBridge(HarnessBridgeConfig("http://[invalid", "", 2))
    with pytest.raises(HarnessBridgeUnavailable, match="地址格式无效"):
        bridge._client()


def test_read_session_validates_binding_and_event_shape(monkeypatch):
    response = _Response({
        "session_id": "session-1",
        "header": {"type": "session"},
        "events": [{"type": "assistant/message"}],
        "event_count": 1,
        "truncated": False,
        "format": "v3",
    })
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    monkeypatch.setattr(bridge, "_client", lambda: _Client(response))
    result = bridge.read_session("task-1", "session-1", "request-1")
    assert result["event_count"] == 1


def test_read_session_rejects_mismatched_response(monkeypatch):
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    monkeypatch.setattr(bridge, "_client", lambda: _Client(_Response({"session_id": "other", "events": []})))
    with pytest.raises(HarnessBridgeError, match="无效 Session"):
        bridge.read_session("task-1", "session-1", "request-1")


def test_read_session_maps_http_failure_to_bridge_error(monkeypatch):
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    monkeypatch.setattr(bridge, "_client", lambda: _Client(_Response(error=httpx.HTTPError("upstream down"))))
    with pytest.raises(HarnessBridgeError, match="Session 回放不可用"):
        bridge.read_session("task-1", "session-1", "request-1")


def test_bridge_preserves_upstream_error_code_and_status(monkeypatch):
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    response = _Response({"error": "confirmation_unavailable"}, status_code=409)
    monkeypatch.setattr(bridge, "_client", lambda: _Client(response))
    with pytest.raises(HarnessBridgeHTTPError) as caught:
        bridge.confirm_task("task-1", {}, "request-1")
    assert caught.value.status_code == 409
    assert caught.value.code == "confirmation_unavailable"


@pytest.mark.parametrize("upstream_error", [
    {"code": "provider_unavailable", "message": "SECRET source text"},
    {"code": "SECRET_source_text", "message": "SECRET source text"},
    "SECRET source text",
])
def test_bridge_error_never_exposes_upstream_message_or_unknown_code(monkeypatch, upstream_error):
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    monkeypatch.setattr(bridge, "_client", lambda: _Client(_Response(
        {"error": upstream_error}, status_code=503,
    )))
    with pytest.raises(HarnessBridgeHTTPError) as caught:
        bridge.create_task({"task_id": "task-1"}, "request-1")
    assert caught.value.status_code == 503
    assert caught.value.code == "provider_unavailable"
    assert "SECRET" not in str(caught.value)


def test_bridge_delete_uses_scoped_operation_and_validates_receipt(monkeypatch):
    monkeypatch.setenv("TEST_ATELIER_SCOPE_SECRET", "s" * 32)
    calls = []

    class Client(_Client):
        def stream(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return self.response

    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    monkeypatch.setattr(bridge, "_client", lambda: Client(_Response({"task_id": "task-1", "status": "deleted"})))
    scope = {"subject_user_id": 1, "project_id": 4, "task_id": "task-1", "allowed_operation": "task.delete"}
    assert bridge.delete_task("task-1", "req-1", scope)["status"] == "deleted"
    assert calls[0][0:2] == ("DELETE", "http://host/v1/tasks/task-1")
    assert calls[0][2]["headers"]["X-Test-Atelier-Scope"].startswith("v1.")
    monkeypatch.setattr(bridge, "_client", lambda: Client(_Response({"task_id": "other", "status": "deleted"})))
    with pytest.raises(HarnessBridgeError, match="无效删除回执"):
        bridge.delete_task("task-1", "req-1", scope)


def test_bridge_coordinates_event_frame_and_replay_page_budgets(monkeypatch):
    seen = []

    class Client(_Client):
        def stream(self, method, url, **kwargs):
            seen.append((method, url, kwargs))
            return self.response

    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2, max_response_bytes=64 * 1024))
    payload = {"task_id": "task-1"}
    monkeypatch.setattr(bridge, "_client", lambda: Client(_Response({"task_id": "task-1"})))
    bridge.create_task(payload, "request-1")
    assert payload == {"task_id": "task-1"}
    assert seen[-1][2]["json"]["event_frame_max_bytes"] == 64 * 1024 - 4096
    monkeypatch.setattr(bridge, "_client", lambda: Client(_StreamResponse([
        "event: task.progress", 'data: {"sequence":1}', "",
    ])))
    assert list(bridge.stream_events("task-1", 0, "request-1")) == [("task.progress", {"sequence": 1})]
    assert seen[-1][2]["params"] == {"after": "0", "max_bytes": str(64 * 1024 - 4096)}


def test_event_stream_is_bounded(monkeypatch):
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2, max_response_bytes=64 * 1024))
    response = _StreamResponse(["data: " + "x" * (64 * 1024), "", ""])
    monkeypatch.setattr(bridge, "_client", lambda: _Client(response))
    with pytest.raises(HarnessBridgeError, match="事件流过大"):
        list(bridge.stream_events("task-1", 0, "request-1"))


def test_event_stream_decodes_split_utf8_lines_without_prefetch(monkeypatch):
    raw = 'event: task.progress\r\ndata: {"text":"中文"}\r\n\r\n'.encode()

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            offset = raw.index("中".encode()) + 1
            yield raw[:offset]
            yield raw[offset:]

    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2))
    monkeypatch.setattr(bridge, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=Chunks()))))
    assert list(bridge.stream_events("task-1", 0, "request-1")) == [("task.progress", {"text": "中文"})]


def test_event_stream_rejects_newline_free_bytes_before_reading_next_chunk(monkeypatch):
    chunks_read = []

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(3):
                chunks_read.append(index)
                yield b"x" * 40000

    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2, max_response_bytes=64 * 1024))
    monkeypatch.setattr(bridge, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=Chunks()))))
    with pytest.raises(HarnessBridgeError, match="事件流过大"):
        list(bridge.stream_events("task-1", 0, "request-1"))
    assert chunks_read == [0, 1]


def test_read_session_rejects_oversized_payload(monkeypatch):
    response = _Response({"session_id": "session-1", "events": []})
    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2, max_response_bytes=64 * 1024))
    monkeypatch.setattr(bridge, "_client", lambda: _Client(response))
    response.content = b"x" * (64 * 1024 + 1)
    with pytest.raises(HarnessBridgeError, match="响应过大"):
        bridge.read_session("task-1", "session-1", "request-1")


@pytest.mark.parametrize("status_code", [200, 502])
def test_bridge_limits_streamed_success_and_error_responses_before_buffering(monkeypatch, status_code):
    chunks_read = []

    class CountingStream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(4):
                chunks_read.append(index)
                yield b"x" * 40000

    def respond(_request):
        return httpx.Response(status_code, stream=CountingStream())

    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2, max_response_bytes=64 * 1024))
    monkeypatch.setattr(bridge, "_client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(HarnessBridgeError, match="响应过大"):
        bridge.create_task({"task_id": "task-1"}, "request-1")
    assert chunks_read == [0, 1]


@pytest.mark.parametrize("declared_size", [str(64 * 1024 + 1), "9" * 5000])
def test_bridge_rejects_content_length_without_reading_body(monkeypatch, declared_size):
    chunks_read = []

    class CountingStream(httpx.SyncByteStream):
        def __iter__(self):
            chunks_read.append(1)
            yield b"private result"

    def respond(_request):
        return httpx.Response(413, headers={"Content-Length": declared_size}, stream=CountingStream())

    bridge = HarnessBridge(HarnessBridgeConfig("http://host", "", 2, max_response_bytes=64 * 1024))
    monkeypatch.setattr(bridge, "_client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(HarnessBridgeError, match="响应过大"):
        bridge.create_task({"task_id": "task-1"}, "request-1")
    assert chunks_read == []
