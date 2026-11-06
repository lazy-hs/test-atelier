#!/usr/bin/env python3
"""Exercise a disposable Host's real staged HTTP upload without a model API."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

import httpx

if __package__:
    from api.harness_chunking import iter_transport_chunks
    from api.harness_routes import _staged_input_manifest
    from .harness_smoke_process import spawn_isolated, stop_tree
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from api.harness_chunking import iter_transport_chunks
    from api.harness_routes import _staged_input_manifest
    from harness_smoke_process import spawn_isolated, stop_tree


def scope(secret: str, task_id: str, operation: str) -> str:
    claims = {"project_id": 1, "subject_user_id": 1, "task_id": task_id,
              "allowed_operation": operation, "expires_at": int(time.time()) + 90}
    encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"v1.{encoded}.{signature}"


def isolated_host_environment(temporary: str, port: int, secret: str, node: str) -> dict[str, str]:
    # Do not pass model credentials, Node preloads or application configuration
    # from the invoking shell to this disposable process.
    inherited = {name: os.environ[name] for name in (
        "PATH", "SystemRoot", "WINDIR", "ComSpec", "PATHEXT", "LANG", "LC_ALL",
    ) if name in os.environ}
    if os.name == "nt":
        inherited.update({"USERPROFILE": temporary, "APPDATA": str(Path(temporary) / "AppData"),
                          "LOCALAPPDATA": str(Path(temporary) / "LocalAppData"),
                          "TEMP": temporary, "TMP": temporary})
    else:
        inherited.update({"HOME": temporary, "TMPDIR": temporary})
    return {**inherited, "TEST_ATELIER_SCOPE_SECRET": secret,
            "TEST_ATELIER_HARNESS_PORT": str(port), "HARNESS_DSH_COMMAND": node,
            "HARNESS_DSH_ARGS_JSON": json.dumps(["-e", "setInterval(() => {}, 1000)"]),
            "HARNESS_DSH_PROTOCOL": "headless", "HARNESS_DSH_INPUT_MODE": "stdin",
            "HARNESS_MAX_REQUEST_BYTES": str(8 * 1024 * 1024),
            "HARNESS_MAX_ACTIVE_TASKS": "1", "DSH_HOME": str(Path(temporary) / "dsh"),
            "HARNESS_TASK_ROOT": str(Path(temporary) / "tasks"),
            "HARNESS_SESSION_ROOT": str(Path(temporary) / "sessions")}


def run(size: int, *, control: bool = False, tamper: bool = False) -> dict[str, int | float]:
    if size < 10 or size > 100 * 1024 * 1024 or size % 10:
        raise ValueError("--bytes must be a multiple of 10 between 10 and 104857600")
    if sys.platform not in {"darwin", "win32", "linux"}:
        raise RuntimeError("staged transport smoke supports only macOS, Windows and Linux")
    extension = Path(os.getenv("HARNESS_EXTENSIONS_ROOT", str(Path(__file__).resolve().parents[2] /
                                                       "test-atelier-harness-extensions"))).resolve()
    if not (extension / "host" / "server.mjs").is_file():
        raise RuntimeError("independent Harness Extension Host is unavailable")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node executable is unavailable")
    with tempfile.TemporaryDirectory(prefix="ta-staged-http-") as temporary:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        task_id = "staged-" + secrets.token_hex(8)
        secret = secrets.token_hex(32)
        environment = isolated_host_environment(temporary, port, secret, node)
        log_path = Path(temporary) / "host.log"
        log_file = log_path.open("w", encoding="utf-8")
        host = spawn_isolated([node, "host/server.mjs"], cwd=extension, env=environment,
                              stdout=log_file, stderr=log_file)
        origin = f"http://127.0.0.1:{port}"
        started = time.monotonic()
        phase = "Host health check"
        try:
            with httpx.Client(timeout=60) as client:
                for _ in range(100):
                    if host.poll() is not None:
                        raise RuntimeError("disposable Host exited before its health check")
                    try:
                        if client.get(origin + "/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("disposable Host did not become ready")
                text = "\x00" * size if control else "你好🙂" * (size // 10)
                manifest, chunk_bytes, encoded = _staged_input_manifest([{"id": "source", "text": text}], 8 * 1024 * 1024)
                endpoint = origin + f"/v1/tasks/{task_id}"

                def post(url: str, operation: str, payload: dict) -> None:
                    response = client.post(url, headers={"x-test-atelier-scope": scope(secret, task_id, operation)},
                                           json=payload)
                    if response.status_code not in {200, 202}:
                        raise RuntimeError(f"Host {operation} returned HTTP {response.status_code}")

                phase = "task creation"
                post(origin + "/v1/tasks", "task.create", {
                    "task_id": task_id, "project_id": 1, "user_id": 1,
                    "task_type": "skill-execution", "skill_id": "api-parsing",
                    "source_document_ids": ["source"],
                    "source_documents": [{"id": "source", "filename": "synthetic.md", "text": ""}],
                    "input_manifest": manifest,
                })
                count = 0
                for index, part in enumerate(iter_transport_chunks(encoded[0], chunk_bytes, 8 * 1024 * 1024 - 4096)):
                    phase = f"chunk {index + 1}/{manifest[0]['chunks']}"
                    if tamper and index == 0:
                        part = ("X" if control else "他") + part[1:]
                    post(endpoint + "/input/chunks", "task.input",
                         {"document_index": 0, "chunk_index": index, "text": part})
                    count += 1
                if count != manifest[0]["chunks"]:
                    raise RuntimeError("uploaded chunk count differs from the signed manifest")
                phase = "Host input assembly"
                if tamper:
                    response = client.post(endpoint + "/input/start", headers={
                        "x-test-atelier-scope": scope(secret, task_id, "task.input")}, json={})
                    if response.status_code != 409 or response.json().get("error") != "input document checksum mismatch":
                        raise RuntimeError(f"tampered input was not rejected (HTTP {response.status_code})")
                    index = json.loads((Path(temporary) / "tasks" / f"{task_id}.json").read_text(encoding="utf-8"))
                    if index["status"] != "starting" or index.get("runner_spawn_intent") is not False:
                        raise RuntimeError("tampered input started the model runner")
                else:
                    post(endpoint + "/input/start", "task.input", {})
                phase = "task cancellation"
                try:
                    post(endpoint + "/cancel", "task.cancel", {})
                except httpx.TransportError:
                    # A stale keep-alive socket can close while the Host plans
                    # large windows. Cancel is idempotent, unlike input/start.
                    with httpx.Client(timeout=90) as retry_client:
                        response = retry_client.post(
                            endpoint + "/cancel",
                            headers={"x-test-atelier-scope": scope(secret, task_id, "task.cancel")},
                            json={},
                        )
                        if response.status_code != 200:
                            raise RuntimeError(f"Host cancellation retry returned HTTP {response.status_code}")
                index_path = Path(temporary) / "tasks" / f"{task_id}.json"
                input_path = Path(temporary) / "sessions" / task_id / "input"
                for _ in range(100):
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                    if index["status"] == "cancelled" and not input_path.exists():
                        break
                    time.sleep(0.05)
                else:
                    raise RuntimeError("Host did not cancel and remove the staged private input")
                return {"bytes": size, "chunks": count, "elapsed_seconds": round(time.monotonic() - started, 2)}
        except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            log_file.flush()
            events = []
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and isinstance(value.get("event"), str):
                    events.append(value["event"])
            raise RuntimeError(f"{phase} failed (Host exit={host.poll()}, elapsed={time.monotonic() - started:.1f}s, events={events})") from exc
        finally:
            try:
                stop_tree(host)
            finally:
                log_file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--control", action="store_true", help="upload JSON-escaped NUL characters instead of UTF-8")
    parser.add_argument("--tamper", action="store_true", help="verify that a modified chunk fails integrity checks before model startup")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.bytes, control=args.control, tamper=args.tamper)))
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"disposable staged transport smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
