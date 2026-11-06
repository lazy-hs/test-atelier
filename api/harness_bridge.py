"""Small HTTP adapter for the independently versioned Harness Node host.

The adapter owns no business state and never opens the Test Atelier database.
It is disabled until ``TEST_ATELIER_HARNESS_URL`` is configured, which keeps
the existing application usable while the separate extension profile is built.
"""

from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from scripts.harness_secret_file import host_token, scope_secret
from scripts.deployment_secret_file import configured_secret


class HarnessBridgeError(RuntimeError):
    """A transport or protocol error from the external Harness host."""


class HarnessBridgeUnavailable(HarnessBridgeError):
    """The external host is not configured or cannot be reached."""


class HarnessBridgeHostUnavailable(HarnessBridgeError):
    """The configured Host could not accept a network connection."""


class HarnessBridgeResponseTooLarge(HarnessBridgeError):
    """The Host response exceeded the bounded transport budget."""


PUBLIC_HOST_ERROR_CODES = frozenset({
    "provider_unavailable", "host_unavailable", "queue_full", "input_timeout", "task_conflict",
    "window_policy_mismatch",
    "confirmation_unavailable", "confirmation_id_mismatch", "confirmation_too_large",
    "invalid_confirmation_decision", "invalid_confirmation_comment", "invalid_ambiguity_choice",
    "task_not_waiting_confirmation", "task_process_unavailable",
    "task_process_termination_unconfirmed", "task_deleted", "not_found",
    "invalid_task_id", "invalid_task_type", "invalid_task_input", "invalid_source_documents",
    "invalid_staged_analysis", "invalid_staged_kind", "staged_task_type_not_supported",
    "invalid_event_frame_budget", "invalid_event_page_budget", "input_chunk_too_large",
    "event_frame_exceeds_transport_budget", "session_resume_unavailable", "session_task_mismatch",
    "scope_payload_mismatch", "scope_task_mismatch", "internal_error",
})


class HarnessBridgeHTTPError(HarnessBridgeError):
    """An upstream Host response with a stable HTTP status and error code."""

    def __init__(self, status_code: int, code: str, message: str = ""):
        # Neither an upstream message nor an unknown code is safe to replay
        # into project task events or expose as an HTTP response detail.
        safe_code = code if isinstance(code, str) and code in PUBLIC_HOST_ERROR_CODES else "provider_unavailable"
        super().__init__(safe_code)
        self.status_code = status_code
        self.code = safe_code


@dataclass(frozen=True)
class HarnessBridgeConfig:
    base_url: str
    token: str
    timeout_seconds: float
    max_response_bytes: int = 16 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "HarnessBridgeConfig":
        raw_timeout = os.getenv("TEST_ATELIER_HARNESS_TIMEOUT", "30")
        try:
            timeout = max(1.0, min(float(raw_timeout), 300.0))
        except ValueError:
            timeout = 30.0
        return cls(
            base_url=os.getenv("TEST_ATELIER_HARNESS_URL", "").strip().rstrip("/"),
            token=host_token(),
            timeout_seconds=timeout,
            max_response_bytes=_bounded_response_bytes(os.getenv("TEST_ATELIER_HARNESS_MAX_RESPONSE_BYTES")),
        )


def _bounded_response_bytes(raw: str | None) -> int:
    try:
        value = int(raw or "16777216")
    except ValueError:
        value = 16 * 1024 * 1024
    return max(64 * 1024, min(value, 64 * 1024 * 1024))


class HarnessBridge:
    """Call the extension-owned task API without importing Harness internals."""

    def __init__(self, config: HarnessBridgeConfig | None = None):
        self.config = config or HarnessBridgeConfig.from_env()

    def _client(self, *, timeout_seconds: float | None = None) -> httpx.Client:
        if not self.config.base_url:
            raise HarnessBridgeUnavailable("DeepSeek Harness 未配置")
        try:
            parsed = urlsplit(self.config.base_url)
        except ValueError as exc:
            raise HarnessBridgeUnavailable("DeepSeek Harness 地址格式无效") from exc
        # Keep the bridge target credential-free even when runtime environment
        # variables bypass the production preflight script.
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise HarnessBridgeUnavailable("DeepSeek Harness 地址必须是无凭据、无查询参数的 HTTP(S) 地址")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        timeout = self.config.timeout_seconds if timeout_seconds is None else max(0.1, min(float(timeout_seconds), self.config.timeout_seconds))
        return httpx.Client(timeout=timeout, headers=headers, follow_redirects=False)

    def _request_id_headers(self, request_id: str, scope: dict[str, Any] | None = None) -> dict[str, str]:
        headers = {"X-Request-ID": request_id}
        if scope is not None:
            try:
                configured_secret = scope_secret()
            except ValueError as exc:
                raise HarnessBridgeUnavailable("Harness Bridge 作用域密钥文件不可用") from exc
            if self.config.base_url and not configured_secret:
                raise HarnessBridgeUnavailable("Harness Bridge 必须配置独立作用域密钥")
            headers["X-Test-Atelier-Scope"] = create_scope_token({**scope, "request_id": request_id})
        return headers

    def _json_response(self, response: httpx.Response) -> Any:
        """Limit decoded bytes while streaming, before buffering or parsing JSON."""
        size = response.headers.get("content-length")
        if size and size.isascii() and size.isdecimal():
            size = size.lstrip("0") or "0"
            limit = str(self.config.max_response_bytes)
            if len(size) > len(limit) or (len(size) == len(limit) and size > limit):
                raise HarnessBridgeResponseTooLarge("DeepSeek Harness 响应过大")
        content = bytearray()
        # The Host speaks plain JSON. Read raw bytes so a compressed response
        # cannot expand before the limit is applied.
        try:
            for chunk in response.iter_raw():
                if len(content) + len(chunk) > self.config.max_response_bytes:
                    raise HarnessBridgeResponseTooLarge("DeepSeek Harness 响应过大")
                content.extend(chunk)
        except httpx.StreamConsumed:
            # Mock transports and callers that pre-read a response expose only
            # the buffered body. Keep the same byte budget in that case.
            buffered = response.content
            if len(buffered) > self.config.max_response_bytes:
                raise HarnessBridgeResponseTooLarge("DeepSeek Harness 响应过大")
            content.extend(buffered)
        try:
            return json.loads(content)
        except ValueError as exc:
            raise HarnessBridgeError("DeepSeek Harness 返回了无效 JSON") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        code = "provider_unavailable"
        try:
            value = self._json_response(response)
        except HarnessBridgeResponseTooLarge:
            raise
        except HarnessBridgeError:
            value = None
        if isinstance(value, dict):
            error = value.get("error")
            if isinstance(error, str) and error:
                code = error
            elif isinstance(error, dict):
                if isinstance(error.get("code"), str) and error["code"]:
                    code = error["code"]
        raise HarnessBridgeHTTPError(response.status_code, code)

    def health(self) -> dict[str, Any]:
        """Return the extension Host health payload.

        The health endpoint is intentionally unauthenticated by the Host and
        does not require a project scope token.  Transport failures are
        surfaced as ``HarnessBridgeHostUnavailable`` so callers can
        distinguish a configured-but-stopped Host from a missing setup.
        """

        try:
            with self._client() as client:
                # Stream the body so the same bounded decoder used by task
                # responses can enforce the response-size limit. Calling
                # ``client.get`` first would preload the body and make
                # ``iter_raw`` raise ``StreamConsumed`` in httpx.
                with client.stream("GET", f"{self.config.base_url}/health") as response:
                    self._raise_for_status(response)
                    value = self._json_response(response)
        except HarnessBridgeUnavailable:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise HarnessBridgeHostUnavailable("DeepSeek Harness Host 不可用") from exc
        except HarnessBridgeError:
            raise
        except httpx.HTTPError as exc:
            raise HarnessBridgeHostUnavailable("DeepSeek Harness Host 不可用") from exc
        if not isinstance(value, dict):
            raise HarnessBridgeError("DeepSeek Harness 返回了无效健康状态")
        return value

    def create_task(self, payload: dict[str, Any], request_id: str, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create a task in the external Harness extension host."""

        try:
            value = self._json_mutation("POST", "/v1/tasks", request_id, {
                **payload, "event_frame_max_bytes": self.config.max_response_bytes - 4096,
            }, scope)
        except HarnessBridgeUnavailable:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Keep a dead/stopped Host distinct from a runner or provider
            # failure.  The API can then tell the operator to restart the
            # Host instead of suggesting that the model credentials failed.
            raise HarnessBridgeHostUnavailable("DeepSeek Harness Host 不可用") from exc
        except HarnessBridgeError:
            raise
        except httpx.HTTPError as exc:
            raise HarnessBridgeError("DeepSeek Harness 任务创建失败") from exc
        if not isinstance(value, dict) or not isinstance(value.get("task_id"), str):
            raise HarnessBridgeError("DeepSeek Harness 返回了无效任务响应")
        return value

    def upload_input_chunk(self, task_id: str, document_index: int, chunk_index: int, text: str,
                           request_id: str, scope: dict[str, Any]) -> None:
        value = self._json_mutation("POST", f"/v1/tasks/{task_id}/input/chunks", request_id, {
            "document_index": document_index, "chunk_index": chunk_index, "text": text,
        }, scope)
        if value.get("task_id") != task_id or value.get("document_index") != document_index or value.get("chunk_index") != chunk_index:
            raise HarnessBridgeError("DeepSeek Harness 返回了无效分块回执")

    def start_staged_task(self, task_id: str, request_id: str, scope: dict[str, Any]) -> dict[str, Any]:
        value = self._json_mutation("POST", f"/v1/tasks/{task_id}/input/start", request_id, {}, scope)
        if value.get("task_id") != task_id or value.get("status") not in {"starting", "running"}:
            raise HarnessBridgeError("DeepSeek Harness 返回了无效启动回执")
        return value

    def cancel_task(self, task_id: str, request_id: str, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._json_mutation("POST", f"/v1/tasks/{task_id}/cancel", request_id, {}, scope)

    def delete_task(self, task_id: str, request_id: str, scope: dict[str, Any]) -> dict[str, Any]:
        value = self._json_mutation("DELETE", f"/v1/tasks/{task_id}", request_id, {}, scope)
        if value.get("task_id") != task_id or value.get("status") != "deleted":
            raise HarnessBridgeError("DeepSeek Harness 返回了无效删除回执")
        return value

    def confirm_task(self, task_id: str, payload: dict[str, Any], request_id: str, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._json_mutation("POST", f"/v1/tasks/{task_id}/confirm", request_id, payload, scope)

    def read_session(self, task_id: str, session_id: str, request_id: str, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read the bounded raw Session replay owned by one Harness task."""

        try:
            with self._client() as client:
                with client.stream(
                    "GET",
                    f"{self.config.base_url}/v1/tasks/{task_id}/session",
                    params={"session_id": session_id},
                    headers=self._request_id_headers(request_id, scope),
                ) as response:
                    self._raise_for_status(response)
                    value = self._json_response(response)
        except HarnessBridgeUnavailable:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise HarnessBridgeHostUnavailable("DeepSeek Harness Host 不可用") from exc
        except HarnessBridgeError:
            raise
        except httpx.HTTPError as exc:
            raise HarnessBridgeError("DeepSeek Harness Session 回放不可用") from exc
        if not isinstance(value, dict) or value.get("session_id") != session_id or not isinstance(value.get("events"), list):
            raise HarnessBridgeError("DeepSeek Harness 返回了无效 Session 回放")
        return value

    def _json_mutation(self, method: str, path: str, request_id: str, payload: dict[str, Any], scope: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            with self._client() as client:
                with client.stream(method, f"{self.config.base_url}{path}", json=payload,
                                   headers=self._request_id_headers(request_id, scope)) as response:
                    self._raise_for_status(response)
                    value = self._json_response(response)
        except HarnessBridgeUnavailable:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise HarnessBridgeHostUnavailable("DeepSeek Harness Host 不可用") from exc
        except HarnessBridgeError:
            raise
        except httpx.HTTPError as exc:
            raise HarnessBridgeError("DeepSeek Harness 请求失败") from exc
        if not isinstance(value, dict):
            raise HarnessBridgeError("DeepSeek Harness 返回了无效响应")
        return value

    def stream_events(self, task_id: str, after: int, request_id: str, scope: dict[str, Any] | None = None,
                      *, idle_timeout_seconds: float | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield parsed SSE events and reject redirects before credentials move.

        ``idle_timeout_seconds`` is used by bounded projector ticks.  Live
        browser subscriptions keep the configured long-poll timeout, while a
        background/detail catch-up must not hold an API request indefinitely
        when the remote task is still running.
        """

        try:
            client = self._client(timeout_seconds=idle_timeout_seconds) if idle_timeout_seconds is not None else self._client()
            with client.stream(
                "GET",
                f"{self.config.base_url}/v1/tasks/{task_id}/events",
                params={"after": str(max(after, 0)), "max_bytes": str(self.config.max_response_bytes - 4096)},
                headers=self._request_id_headers(request_id, scope),
            ) as response:
                self._raise_for_status(response)
                event_name = "message"
                data_lines: list[str] = []
                response_bytes = 0
                pending = bytearray()
                for chunk in response.iter_raw():
                    response_bytes += len(chunk)
                    if response_bytes > self.config.max_response_bytes:
                        raise HarnessBridgeError("DeepSeek Harness 事件流过大")
                    pending.extend(chunk)
                    while (newline := pending.find(b"\n")) >= 0:
                        try:
                            line = bytes(pending[:newline]).rstrip(b"\r").decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise HarnessBridgeError("DeepSeek Harness 事件编码无效") from exc
                        del pending[:newline + 1]
                        if line.startswith("event:"):
                            event_name = line[6:].strip() or "message"
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                            continue
                        if line != "" or not data_lines:
                            continue
                        raw = "\n".join(data_lines)
                        try:
                            payload = json.loads(raw)
                        except ValueError as exc:
                            raise HarnessBridgeError("DeepSeek Harness 返回了无效事件") from exc
                        if not isinstance(payload, dict):
                            raise HarnessBridgeError("DeepSeek Harness 事件必须是对象")
                        yield event_name, payload
                        event_name, data_lines = "message", []
        except HarnessBridgeUnavailable:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise HarnessBridgeHostUnavailable("DeepSeek Harness Host 不可用") from exc
        except HarnessBridgeError:
            raise
        except httpx.ReadTimeout:
            # A bounded projector poll may legitimately observe no event yet.
            # Treat the idle read as an empty page so the projector can release
            # its lease without emitting a user-visible connection failure.
            return
        except httpx.HTTPError as exc:
            raise HarnessBridgeError("DeepSeek Harness 事件流不可用") from exc
        finally:
            if "client" in locals():
                client.close()


def create_scope_token(scope: dict[str, Any], ttl_seconds: int = 60) -> str:
    """Create a short-lived HMAC token for one project-scoped operation."""

    # Keep the bridge credential independent from browser-session encryption.
    # Fall back to the existing secret for backwards-compatible local setups.
    try:
        secret = scope_secret() or configured_secret("AI_STUDIO_SESSION_SECRET")
    except ValueError as exc:
        raise HarnessBridgeUnavailable("Harness Bridge 作用域密钥文件不可用") from exc
    secret = secret.encode("utf-8")
    if len(secret) < 32:
        raise HarnessBridgeUnavailable("Test Atelier 会话密钥不可用于 Harness 作用域凭据")
    expires_at = int(time.time()) + max(5, min(ttl_seconds, 300))
    claims = {**scope, "expires_at": expires_at}
    encoded = base64.urlsafe_b64encode(json.dumps(claims, ensure_ascii=False, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"v1.{encoded}.{signature}"
