#!/usr/bin/env python3
"""Run a real DeepSeek Harness -> Test Atelier bridge smoke without GNU timeout.

The API key is read from OPENAI_API_KEY and is never printed or persisted. This
script uses an in-memory SQLite database and temporary DSH_HOME/task/session roots.
"""
from __future__ import annotations

import os
import io
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.harness_smoke_process import spawn_isolated, stop_tree  # noqa: E402
from scripts.harness_browser_smoke import real_smoke_host_environment, real_dsh_args  # noqa: E402


def sibling_checkout(name: str) -> Path:
    """Resolve the conventional sibling checkout without a machine path."""
    return ROOT.parent / name


EXTENSIONS = Path(os.getenv("HARNESS_EXTENSIONS_ROOT", str(sibling_checkout("test-atelier-harness-extensions"))))
HARNESS_ROOT = Path(os.getenv("HARNESS_ROOT", str(sibling_checkout("deepseek-harness"))))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


PORT = int(os.getenv("TEST_ATELIER_HARNESS_PORT", "0")) or free_port()
# Generate ephemeral signing secrets when the caller does not provide them.
# Predictable development defaults must never be reused for a real model run.
SECRET = os.getenv("AI_STUDIO_SESSION_SECRET") or secrets.token_urlsafe(32)
SCOPE_SECRET = os.getenv("TEST_ATELIER_SCOPE_SECRET") or secrets.token_urlsafe(32)
PROTOCOL = os.getenv("HARNESS_DSH_PROTOCOL", "headless").strip().lower()
API_BASE = os.getenv("OPENAI_API_BASE", "http://code.ugreencloud.com:8000/v1").strip()
MODEL = os.getenv("OPENAI_MODEL", "ugreen-ai-model").strip()

if PROTOCOL not in {"headless", "acp"}:
    raise SystemExit("HARNESS_DSH_PROTOCOL must be headless or acp")

if not os.getenv("OPENAI_API_KEY"):
    raise SystemExit("OPENAI_API_KEY must be set; the key is never read from a file")
if not API_BASE or not MODEL:
    raise SystemExit("OPENAI_API_BASE and OPENAI_MODEL must be non-empty")
if len(SECRET) < 32:
    raise SystemExit("AI_STUDIO_SESSION_SECRET must contain at least 32 characters")
if len(SCOPE_SECRET) < 32:
    raise SystemExit("TEST_ATELIER_SCOPE_SECRET must contain at least 32 characters")

# Import the application only after the smoke-specific environment is set.
os.environ.update(
    DATABASE_URL="sqlite://",
    AI_STUDIO_SESSION_SECRET=SECRET,
    TEST_ATELIER_SCOPE_SECRET=SCOPE_SECRET,
    AI_STUDIO_ADMIN_PASSWORD=os.getenv("AI_STUDIO_ADMIN_PASSWORD", "test-admin-password-123"),
    TEST_ATELIER_HARNESS_URL=f"http://127.0.0.1:{PORT}",
    TEST_ATELIER_HARNESS_TIMEOUT="300",
)
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient  # noqa: E402
from api.db import Base, HarnessTaskEvent, SessionLocal, engine  # noqa: E402
from api.main import app  # noqa: E402


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="test-atelier-harness-smoke-") as directory:
        temp = Path(directory)
        token_file = temp / "host-api-token"
        token_file.write_bytes(secrets.token_urlsafe(32).encode("ascii") + b"\r\n")
        # Exercise the same file-only Host credential path as combined Compose.
        os.environ["TEST_ATELIER_HARNESS_TOKEN"] = ""
        os.environ["TEST_ATELIER_HARNESS_TOKEN_FILE"] = str(token_file)
        host_env = real_smoke_host_environment(os.environ, token_file)
        dsh_profile = "test-atelier-acp" if PROTOCOL == "acp" else "test-atelier"
        dsh_args = real_dsh_args(HARNESS_ROOT, PROTOCOL)
        host_env.update(
            DSH_HOME=str(temp / "dsh"),
            HARNESS_ROOT=str(HARNESS_ROOT),
            TEST_ATELIER_SKILL_ROOT=str(Path(os.environ.get("TEST_ATELIER_SKILL_ROOT", str(ROOT.parent / "qa-doc-skills"))).resolve()),
            HARNESS_DSH_COMMAND="node" if sys.platform == "win32" else "corepack",
            HARNESS_DSH_ARGS_JSON=json.dumps(dsh_args),
            HARNESS_DSH_PROTOCOL=PROTOCOL,
            TEST_ATELIER_HARNESS_PORT=str(PORT),
            TEST_ATELIER_SCOPE_SECRET=SCOPE_SECRET,
            HARNESS_TASK_ROOT=str(temp / "tasks"),
            HARNESS_SESSION_ROOT=str(temp / "sessions"),
            OPENAI_API_BASE=API_BASE,
            OPENAI_MODEL=MODEL,
        )
        if PROTOCOL == "headless":
            host_env["HARNESS_DSH_INPUT_MODE"] = "stdin"
        # Use the dependency-free Python installer so this smoke can run from
        # native macOS, Windows, and Linux launchers without requiring Bash.
        subprocess.run(
            [
                sys.executable,
                str(EXTENSIONS / "scripts/install_profile.py"),
                "--harness-root",
                str(HARNESS_ROOT),
                "--dsh-home",
                str(temp / "dsh"),
            ],
            cwd=EXTENSIONS,
            env=host_env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        # Run the same production preflight used by native Host launchers. It
        # validates the checkout, transport, writable roots and model env
        # without ever printing the credential.
        preflight = subprocess.run(
            [sys.executable, str(EXTENSIONS / "scripts/preflight_host.py"), "--production"],
            cwd=EXTENSIONS,
            env=host_env,
            check=False,
            capture_output=True,
            text=True,
        )
        if preflight.returncode:
            detail = (preflight.stdout + preflight.stderr)[-4000:]
            detail = detail.replace(os.getenv("OPENAI_API_KEY", ""), "<redacted>")
            raise RuntimeError(f"Harness Host preflight failed:\n{detail}")

        host_log = temp / "host.log"
        host_handle = host_log.open("w", encoding="utf-8")
        host = spawn_isolated(
            ["node", "host/server.mjs"],
            cwd=EXTENSIONS,
            env=host_env,
            stdout=host_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_for_host_health(host, PORT, host_log)

            Base.metadata.drop_all(engine)
            with TestClient(app) as client:
                csrf = client.get("/api/v1/auth/csrf").json()["token"]
                login = client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": os.environ["AI_STUDIO_ADMIN_PASSWORD"]},
                    headers={"x-csrf-token": csrf},
                )
                assert login.status_code == 200, login.text
                headers = {"x-csrf-token": login.json()["csrf"]}
                project = client.post("/api/v1/projects", json={"name": "Harness smoke"}, headers=headers)
                assert project.status_code == 200, project.text
                project_root = f"/api/v1/projects/{project.json()['id']}"
                upload = client.post(
                    f"{project_root}/documents",
                    files={"file": ("req.md", "系统支持用户使用正确账号登录。\n" * (
                        4500 if os.getenv("TA_LOCAL_SMOKE_STAGED") == "1"
                        else 100 if os.getenv("TA_LOCAL_SMOKE_CHUNKED") == "1" else 1
                    ))},
                    headers=headers,
                )
                assert upload.status_code == 200, upload.text
                source_id = upload.json()["id"]

                analysis = client.post(
                    f"{project_root}/harness/tasks",
                    json={
                        "task_type": "requirement-analysis",
                        "task_version": "v1",
                        "source_document_ids": [source_id],
                        "skill_id": "requirement-analysis",
                        "input": {"language": "zh-CN", "output_mode": "structured"},
                        "idempotency_key": f"analysis-{uuid.uuid4().hex}",
                    },
                    headers=headers,
                )
                assert analysis.status_code == 200, analysis.text
                analysis_task = analysis.json()
                wait_for_task_completion(client, project_root, analysis_task["task_id"], headers=headers,
                                         expect_checkpoint=os.getenv("TA_LOCAL_SMOKE_CHECKPOINT") == "1")
                verify_session_replay(client, project_root, analysis_task["task_id"])
                analysis_response = client.get(f"{project_root}/harness/tasks/{analysis_task['task_id']}/result")
                assert analysis_response.status_code == 200, analysis_response.text
                analysis_result = analysis_response.json()
                assert analysis_result["status"] == "completed"
                assert analysis_result["project_id"] == project.json()["id"]
                assert analysis_result["analysis_run_id"] == analysis_task["analysis_run_id"]
                assert analysis_result["source_document_ids"] == [source_id]
                if os.getenv("TA_LOCAL_SMOKE_CHUNKED") == "1" or os.getenv("TA_LOCAL_SMOKE_STAGED") == "1":
                    task_detail = client.get(f"{project_root}/harness/tasks/{analysis_task['task_id']}").json()
                    task_id = analysis_task["task_id"]
                    assert len(task_detail["session_ids"]) >= 3, "Map/reduce did not bind multiple real Sessions"
                    progress = client.get(f"{project_root}/harness/tasks/{analysis_task['task_id']}/events?after=0")
                    assert "chunk_started" in progress.text and "reduce_completed" in progress.text
                    bound_sessions = set()
                    for line in progress.text.splitlines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        if event.get("kind") == "session_bound":
                            bound_sessions.add(event["session_id"])
                    assert bound_sessions == set(task_detail["session_ids"]), "Synthetic map/reduce IDs were authorized"
                    assert all(not session.startswith(f"ta-{task_id}") for session in bound_sessions)
                    host_index = json.loads((temp / "tasks" / f"{task_id}.json").read_text(encoding="utf-8"))
                    assert set(host_index["session_ids"]) == bound_sessions, "Host index retained synthetic Session IDs"
                    for session_id in bound_sessions:
                        replay = client.get(
                            f"{project_root}/harness/tasks/{task_id}/session",
                            params={"session_id": session_id},
                        )
                        assert replay.status_code == 200, f"Bound Session is not replayable: HTTP {replay.status_code}"
                        assert replay.json()["session_id"] == session_id and replay.json()["events"]
                    synthetic = client.get(
                        f"{project_root}/harness/tasks/{task_id}/session",
                        params={"session_id": f"ta-{task_id}-map-1"},
                    )
                    assert synthetic.status_code == 409, "Synthetic map ID unexpectedly authorized"
                analysis_id = analysis_task["analysis_run_id"]
                run_row = client.get(f"{project_root}/analyses/{analysis_id}").json()
                assert run_row["status"] == "ready"
                for ambiguity in run_row["result"].get("ambiguities", []):
                    if ambiguity.get("critical") and not ambiguity.get("decision"):
                        decision = client.patch(
                            f"{project_root}/analyses/{analysis_id}/ambiguities/{ambiguity['id']}",
                            json={"version": run_row["version"], "decision": "按系统默认规则确认"},
                            headers=headers,
                        )
                        assert decision.status_code == 200, decision.text
                        run_row = decision.json()

                cases = client.post(
                    f"{project_root}/harness/tasks",
                    json={
                        "task_type": "test-cases",
                        "task_version": "v1",
                        "analysis_run_id": analysis_id,
                        "skill_id": "test-cases",
                        "input": {"language": "zh-CN", "output_mode": "structured"},
                        "idempotency_key": f"cases-{uuid.uuid4().hex}",
                    },
                    headers=headers,
                )
                assert cases.status_code == 200, cases.text
                wait_for_task_completion(client, project_root, cases.json()["task_id"],
                                         headers=headers, expect_case_checkpoint=os.getenv("TA_LOCAL_SMOKE_CASE_CHECKPOINT") == "1")
                verify_session_replay(client, project_root, cases.json()["task_id"])
                cases_response = client.get(f"{project_root}/harness/tasks/{cases.json()['task_id']}/result")
                assert cases_response.status_code == 200, cases_response.text
                cases_result = cases_response.json()
                assert cases_result["status"] == "completed"
                assert cases_result["project_id"] == analysis_result["project_id"]
                assert cases_result["source_document_ids"] == analysis_result["source_document_ids"]
                assert cases_result["analysis_run_id"] == analysis_result["analysis_run_id"]
                assert cases_result["result"]["analysis_run_id"] == analysis_result["analysis_run_id"]
                if os.getenv("TA_LOCAL_SMOKE_STAGED_CASES") == "1" and os.getenv("TA_LOCAL_SMOKE_CASE_CHECKPOINT") == "1":
                    artifact = client.get(f"{project_root}/harness/tasks/{cases.json()['task_id']}/xmind")
                    assert artifact.status_code == 200, artifact.text[:250] if artifact.status_code != 200 else ""
                    with zipfile.ZipFile(io.BytesIO(artifact.content)) as workbook:
                        assert workbook.testzip() is None
                        assert "content.xml" in workbook.namelist()
                        content = workbook.read("content.xml").decode("utf-8")
                        assert "风险点" in content and "验证 REQ-90" in content
                if os.getenv("TA_LOCAL_SMOKE_STAGED_CASES") == "1" or os.getenv("TA_LOCAL_SMOKE_FRAGMENT_CASES") == "1":
                    case_task_id = cases.json()["task_id"]
                    case_detail = client.get(f"{project_root}/harness/tasks/{case_task_id}").json()
                    assert len(case_detail["session_ids"]) >= 3, "Case windows did not bind real Sessions"
                    case_progress = client.get(f"{project_root}/harness/tasks/{case_task_id}/events?after=0")
                    assert "chunk_started" in case_progress.text and "reduce_completed" in case_progress.text
                    case_result = client.get(f"{project_root}/harness/tasks/{case_task_id}/result")
                    assert case_result.status_code == 200, case_result.text
                    case_count = len(case_result.json()["result"]["data"]["cases"])
                    if os.getenv("TA_LOCAL_SMOKE_FRAGMENT_CASES") == "1":
                        assert case_count >= 2, "Long requirement fragments lost their cases"
                    else:
                        assert case_count == 90
                    assert not (temp / "sessions" / case_task_id / "input").exists()
                # A terminal task must replay from the local projection after a
                # browser refresh without contacting or duplicating the Host.
                replay = client.get(
                    f"{project_root}/harness/tasks/{cases.json()['task_id']}/events?after=0"
                )
                assert replay.status_code == 200 and "task.completed" in replay.text
                drafts = client.get(f"{project_root}/drafts?analysis_id={analysis_id}")
                assert drafts.status_code == 200 and drafts.json(), drafts.text
                draft = drafts.json()[0]
                saved = client.patch(
                    f"{project_root}/drafts/{draft['id']}",
                    json={"version": draft["version"], "payload": {**draft["payload"], "title": "Harness smoke draft"}},
                    headers=headers,
                )
                assert saved.status_code == 200, saved.text
                published = client.post(f"{project_root}/drafts/{draft['id']}/publish", headers=headers)
                assert published.status_code == 200, published.text
                if os.getenv("TA_LOCAL_SMOKE_ALL_GENERIC") == "1":
                    for skill_name, content in (
                        ("api-parsing", "GET /health returns HTTP 200"),
                        ("dev-doc-analysis", "Architecture: API and worker use a shared database"),
                        ("figma-to-prd", "Screen: login with account and password fields"),
                    ):
                        source = client.post(f"{project_root}/documents", files={
                            "file": (f"{skill_name}.md", content.encode("utf-8")),
                        }, headers=headers)
                        assert source.status_code == 200, source.text
                        created_skill = client.post(f"{project_root}/harness/tasks", json={
                            "task_type": "skill-execution", "skill_id": skill_name,
                            "source_document_ids": [source.json()["id"]],
                            "idempotency_key": f"generic-{skill_name}-{uuid.uuid4().hex}",
                        }, headers=headers)
                        assert created_skill.status_code == 200, created_skill.text
                        task_id = created_skill.json()["task_id"]
                        wait_for_task_completion(client, project_root, task_id)
                        skill_result = client.get(f"{project_root}/harness/tasks/{task_id}/result")
                        assert skill_result.status_code == 200, skill_result.text
                        assert skill_result.json()["result_schema"] == "skill-execution.v1"
                        assert skill_result.json()["result"]["skill_id"] == skill_name
                        assert skill_result.json()["result"]["source_document_ids"] == [source.json()["id"]]
                        verify_session_replay(client, project_root, task_id)
                if os.getenv("TA_LOCAL_SMOKE_STAGED") == "1":
                    spec = client.post(
                        f"{project_root}/documents",
                        files={"file": ("api.md", "GET /health\n" * 7000)},
                        headers=headers,
                    )
                    assert spec.status_code == 200, spec.text
                    skill = client.post(f"{project_root}/harness/tasks", json={
                        "task_type": "skill-execution", "skill_id": "api-parsing",
                        "source_document_ids": [spec.json()["id"]],
                        "idempotency_key": f"generic-staged-{uuid.uuid4().hex}",
                    }, headers=headers)
                    assert skill.status_code == 200, skill.text
                    skill_id = skill.json()["task_id"]
                    wait_for_task_completion(client, project_root, skill_id)
                    skill_result = client.get(f"{project_root}/harness/tasks/{skill_id}/result")
                    assert skill_result.status_code == 200, skill_result.text
                    assert skill_result.json()["result_schema"] == "skill-execution.v1"
                    assert skill_result.json()["result"]["source_document_ids"] == [spec.json()["id"]]
                    progress = client.get(f"{project_root}/harness/tasks/{skill_id}/events?after=0")
                    assert "chunk_started" in progress.text and "reduce_completed" in progress.text
                print("real Harness bridge smoke: analysis, ambiguity confirmation, cases, draft save and publish passed")
        finally:
            try:
                stop_tree(host)
            finally:
                host_handle.close()


def wait_for_task_completion(client: TestClient, project_root: str, task_id: str, timeout: float = 90.0,
                             *, headers: dict[str, str] | None = None, expect_checkpoint: bool = False,
                             expect_case_checkpoint: bool = False) -> None:
    """Wait for real dsh execution, not merely for task creation to return."""
    endpoint = f"{project_root}/harness/tasks/{task_id}"
    deadline = time.monotonic() + timeout
    last_status = "unknown"
    confirmed_checkpoint = False
    confirmed_ambiguity = False
    confirmed_case_checkpoint = False
    confirmed_case_ambiguity = False
    confirmed_case_selection = False
    while time.monotonic() < deadline:
        response = client.get(endpoint)
        assert response.status_code == 200, f"Task detail failed: HTTP {response.status_code}"
        task = response.json()
        last_status = task.get("status", "unknown")
        if last_status == "waiting_confirmation" and expect_checkpoint:
            with SessionLocal() as db:
                event = db.query(HarnessTaskEvent).filter_by(task_id=task_id, event="task.confirmation_required") \
                    .order_by(HarnessTaskEvent.sequence.desc()).first()
                confirmation = event.payload.get("confirmation", {}) if event is not None else {}
            pending = client.get(f"{endpoint}/result").json()
            assert pending["status"] == "waiting_confirmation" and not pending["result"]
            if confirmation.get("kind") == "skill_checkpoint":
                assert not confirmed_checkpoint and confirmation.get("phase") == "2.5"
                assert "解析完成" in confirmation.get("summary", "")
                answer = {"confirmation_id": confirmation["confirmation_id"], "decision": "approve",
                          "comment": "仅 Web 适用"}
                confirmed_checkpoint = True
            else:
                assert confirmed_checkpoint and not confirmed_ambiguity
                assert confirmation.get("kind") == "ambiguity_checkpoint" and confirmation.get("phase") == "2.6"
                assert confirmation.get("source_text") == "系统支持用户使用正确账号登录。"
                answer = {"confirmation_id": confirmation["confirmation_id"], "decision": "approve",
                          "ambiguity_choice": "mark_pending"}
                confirmed_ambiguity = True
            decision = client.post(f"{endpoint}/confirm", json=answer, headers=headers)
            assert decision.status_code == 200, decision.text
        if last_status == "waiting_confirmation" and expect_case_checkpoint:
            with SessionLocal() as db:
                event = db.query(HarnessTaskEvent).filter_by(task_id=task_id, event="task.confirmation_required") \
                    .order_by(HarnessTaskEvent.sequence.desc()).first()
                confirmation = event.payload.get("confirmation", {}) if event is not None else {}
            pending = client.get(f"{endpoint}/result").json()
            assert pending["status"] == "waiting_confirmation" and not pending["result"]
            if confirmation.get("kind") == "case_skill_checkpoint":
                assert not confirmed_case_checkpoint and confirmation.get("phase") == "2.5"
                assert "用例解析完成" in confirmation.get("summary", "")
                answer = {"confirmation_id": confirmation["confirmation_id"], "decision": "approve", "comment": "Web"}
                confirmed_case_checkpoint = True
            elif confirmation.get("kind") == "ambiguity_checkpoint":
                assert confirmed_case_checkpoint and not confirmed_case_ambiguity
                assert confirmation.get("phase") == "2.6"
                expected_excerpt = ("系统支持用户登录。" if os.getenv("TA_LOCAL_SMOKE_STAGED_CASES") == "1"
                                    else "用户可以使用账号登录")
                assert confirmation.get("source_text") == expected_excerpt
                answer = {"confirmation_id": confirmation["confirmation_id"], "decision": "approve",
                          "ambiguity_choice": "accept_suggestion"}
                confirmed_case_ambiguity = True
            else:
                assert confirmation.get("kind") == "case_tag_selection" and confirmed_case_ambiguity
                assert not confirmed_case_selection and confirmation.get("phase") == "3"
                answer = {"confirmation_id": confirmation["confirmation_id"], "decision": "approve", "comment": ""}
                confirmed_case_selection = True
            decision = client.post(f"{endpoint}/confirm", json=answer, headers=headers)
            assert decision.status_code == 200, decision.text
        if last_status == "completed":
            if expect_checkpoint:
                assert confirmed_checkpoint and confirmed_ambiguity, "Phase 2.5/2.6 checkpoint was bypassed"
            if expect_case_checkpoint:
                assert confirmed_case_checkpoint and confirmed_case_ambiguity and confirmed_case_selection, "Case Phase 2.5/2.6/3 was bypassed"
            events = client.get(f"{endpoint}/events?after=0")
            assert events.status_code == 200, f"Task events failed: HTTP {events.status_code}"
            assert "task.result" in events.text and "task.completed" in events.text
            if expect_case_checkpoint:
                frames = [json.loads(line[6:]) for line in events.text.splitlines() if line.startswith("data: ")]
                gate = next((item for item in frames if item.get("kind") == "case_checkpoint"
                             and item.get("phase") == "2.8" and item.get("status") == "pass"), None)
                result = next((item for item in frames if item.get("result", {}).get("schema") == "test-cases.v1"), None)
                assert gate and result and gate["sequence"] < result["sequence"], "Case Phase 2.8 was not gated before result"
            return
        if last_status in {"failed", "cancelled"}:
            result = client.get(f"{endpoint}/result")
            error = result.json().get("error", {}) if result.status_code == 200 else {}
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            detail = error.get("details", {}) if isinstance(error, dict) else {}
            schema = detail.get("schema", "unknown") if isinstance(detail, dict) else "unknown"
            # Projection details contain only bounded schema keys, sizes and
            # hashes; never print the model output or document text here.
            diagnostic = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else "{}"
            raise AssertionError(f"Harness task ended as {last_status} ({code}; schema={schema}; details={diagnostic})")
        time.sleep(0.25)
    raise TimeoutError(f"Harness task did not complete within {timeout}s (last status: {last_status})")


def verify_session_replay(client: TestClient, project_root: str, task_id: str) -> None:
    """Check the real dsh artifact, not merely a task with a Session ID."""
    endpoint = f"{project_root}/harness/tasks/{task_id}"
    task = client.get(endpoint).json()
    replay = client.get(f"{endpoint}/session")
    assert replay.status_code == 200, f"Real dsh Session replay failed: HTTP {replay.status_code}"
    assert replay.json()["session_id"] == task["session_id"]
    assert replay.json()["events"], "Real dsh Session replay is empty"


def wait_for_host_health(process: subprocess.Popen[str], port: int, log_path: Path, timeout: float = 30.0) -> None:
    """Prove the HTTP Host is reachable before submitting a real task."""

    def log_tail() -> str:
        detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        key = os.getenv("OPENAI_API_KEY", "")
        return detail.replace(key, "<redacted>") if key else detail

    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = log_tail()
            raise RuntimeError(f"Harness Host exited before /health:\n{detail}")
        try:
            request = Request(url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=1) as response:
                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
            if payload.get("status") != "ok" or payload.get("service") != "test-atelier-harness-host":
                raise RuntimeError(f"Unexpected Harness Host health payload: {payload!r}")
            return
        except RuntimeError:
            raise
        except Exception:
            time.sleep(0.25)
    detail = log_tail()
    raise TimeoutError(f"Harness Host /health did not become ready:\n{detail}")


if __name__ == "__main__":
    run()
