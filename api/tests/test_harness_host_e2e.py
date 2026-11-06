import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from api import harness_bridge
from api.db import AnalysisRun, HarnessTask, Project, SessionLocal
from api.tests.test_ai_workflow import context  # noqa: F401


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_real_node_host_projects_a_fake_harness_result(context, monkeypatch, tmp_path):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("requirements.md", b"# Login\nOpen the home page")}, headers=headers)
    assert upload.status_code == 200
    source_id = upload.json()["id"]

    fake = tmp_path / "fake-dsh.mjs"
    fake.write_text(
            """
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
const prompt = process.argv.at(-1) || '';
const taskData = JSON.parse(prompt.split('\\n\\n').at(-1));
const sourceId = taskData.source_documents?.[0]?.id || taskData.analysis?.partial_results?.[0]?.source_document_ids?.[0] || 'missing';
const taskId = process.env.DSH_TASK_ID;
// A real dsh allocates a new Session; DSH_SESSION_ID is only the Host placeholder.
const sessionId = `dsh-${taskId}`;
const sessionDir = join(process.env.HARNESS_SESSION_ROOT, 'session');
mkdirSync(sessionDir, { recursive: true });
writeFileSync(join(sessionDir, 'session.v4.jsonl'), [
  JSON.stringify({type:'session', version:4, id:sessionId, createdAt:Date.now(), cwd:process.cwd()}),
  JSON.stringify({type:'assistant/message', seq:1, data:{content:[{type:'text', text:'fake result'}]}}),
].join('\\n') + '\\n');
const generic = /task type skill-execution/.test(prompt);
const caseTask = /task type test-cases/.test(prompt);
const oversizedResult = taskData.source_documents?.some(item => item.filename === 'huge-result.md');
const skillId = prompt.match(/exact name ([a-z-]+)/)?.[1] || 'api-parsing';
if (taskData.source_documents?.some(item => item.filename === 'events.md')) {
  for (let index = 0; index < 160; index++) {
    console.log(JSON.stringify({type:'text', text:'p'.repeat(600)}));
  }
}
const cases = caseTask ? (taskData.analysis?.partial_results
  ? taskData.analysis.partial_results.flatMap(item => item.data.cases)
  : taskData.analysis.requirements.map(item => ({id:'TC-' + item.id, title:'Case ' + item.id,
    requirement_ids:[item.id], preconditions:[], steps:[{order:1,action:'Act',expected:'Done'}],
    expected:'Done', method:'EP', priority:'medium', labels:[],
    traceability:[{requirement_id:item.id, acceptance_criterion_id:null}], quality_warnings:[]}))) : [];
const result = caseTask ? {
  schema:'test-cases.v1', result_id:'staged-case-result', task_id:taskId,
  session_id:sessionId, source_document_ids:[sourceId], analysis_run_id:taskData.analysis_run_id || 'run-oversized',
  generated_at:'2026-10-23T00:00:00Z', warnings:[], data:{cases}
} : generic ? {
  schema:'skill-execution.v1', result_id:'generic-result', task_id:taskId,
  session_id:sessionId, skill_id:skillId, source_document_ids:[sourceId],
  generated_at:'2026-10-23T00:00:00Z', warnings:[], data:{output:'fake generic result', items:[]}
} : {
  schema:'requirement-analysis.v1', result_id:'e2e-result', task_id:taskId,
  session_id:sessionId, source_document_ids:[sourceId], generated_at:'2026-10-23T00:00:00Z',
  report_markdown:'# Login analysis\\n\\nFake skill report\\n',
  warnings:[], data:{requirements:oversizedResult ? [{id:'REQ-1',title:'Too large',
    description:'x'.repeat(70000),priority:'medium',source_refs:[{document_id:sourceId,locator:'1'}],
    acceptance_criteria:[]}] : [], ambiguities:[], quality_warnings:[]}
};
console.log(JSON.stringify({type:'session', sessionId}));
console.log(JSON.stringify({type:'final', text: JSON.stringify(result)}));
""".strip(),
        encoding="utf-8",
    )
    port = _free_port()
    secret = os.environ["AI_STUDIO_SESSION_SECRET"]
    extension_root = os.getenv(
        "TEST_ATELIER_HARNESS_EXTENSIONS",
        str(Path(__file__).resolve().parents[3] / "test-atelier-harness-extensions"),
    )
    if not (Path(extension_root) / "host" / "server.mjs").is_file():
        pytest.skip("独立 Harness Extension checkout 未提供；本地集成 smoke 需要单独 checkout")
    token_file = tmp_path / "host-token"
    token_file.write_bytes(b"host-api-token-for-e2e-1234\r\n")
    env = {
        **os.environ,
        "TEST_ATELIER_HARNESS_PORT": str(port),
        "TEST_ATELIER_SCOPE_SECRET": secret,
        "TEST_ATELIER_HARNESS_TOKEN": "",
        "TEST_ATELIER_HARNESS_TOKEN_FILE": str(token_file),
        "HARNESS_DSH_COMMAND": "node",
        "HARNESS_DSH_ARGS_JSON": json.dumps([str(fake)]),
        "HARNESS_TASK_ROOT": str(tmp_path / "tasks"),
        "HARNESS_SESSION_ROOT": str(tmp_path / "sessions"),
    }
    # The host is a separate process, just as it is in the deployment model.
    def start_host():
        process = subprocess.Popen(
            ["node", "host/server.mjs"], cwd=extension_root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Harness Host exited on startup: {process.returncode}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.3) as response:
                    if response.status == 200:
                        return process
            except OSError:
                time.sleep(0.05)
        process.terminate()
        process.wait(timeout=5)
        raise AssertionError("Harness Host did not become ready")

    host = start_host()
    try:
        # The API and the independent Host must share the dedicated
        # bridge-signing secret; subprocess-only injection is insufficient.
        monkeypatch.setenv("TEST_ATELIER_SCOPE_SECRET", secret)
        monkeypatch.delenv("TEST_ATELIER_HARNESS_TOKEN", raising=False)
        monkeypatch.setenv("TEST_ATELIER_HARNESS_TOKEN_FILE", str(token_file))
        monkeypatch.setenv("TEST_ATELIER_HARNESS_URL", f"http://127.0.0.1:{port}")
        response = client.post(f"{root}/harness/tasks", json={
            "task_type": "requirement-analysis",
            "skill_id": "requirement-analysis",
            "source_document_ids": [source_id],
            "idempotency_key": "node-host-e2e",
        }, headers=headers)
        assert response.status_code == 200, response.text
        task = response.json()
        events = client.get(f"{root}/harness/tasks/{task['task_id']}/events?after={task['sequence']}")
        assert events.status_code == 200, events.text
        assert "task.result" in events.text
        assert "task.completed" in events.text
        with SessionLocal() as db:
            run = db.query(AnalysisRun).filter(AnalysisRun.provider == "harness").one()
            assert run.status == "ready"
            assert run.result["harness_result"]["schema"] == "requirement-analysis.v1"
        analysis_result = client.get(f"{root}/harness/tasks/{task['task_id']}/result").json()
        assert analysis_result["project_id"] == int(root.rsplit("/", 1)[-1])
        assert analysis_result["analysis_run_id"] == task["analysis_run_id"]
        assert analysis_result["source_document_ids"] == [source_id]
        report = client.get(f"{root}/harness/tasks/{task['task_id']}/report.md")
        assert report.status_code == 200 and report.text == "# Login analysis\n\nFake skill report\n"

        generic = client.post(f"{root}/harness/tasks", json={
            "task_type": "skill-execution",
            "skill_id": "api-parsing",
            "source_document_ids": [source_id],
            "idempotency_key": "node-host-generic-skill-e2e",
        }, headers=headers)
        assert generic.status_code == 200, generic.text
        generic_task = generic.json()
        generic_events = client.get(f"{root}/harness/tasks/{generic_task['task_id']}/events?after={generic_task['sequence']}")
        assert generic_events.status_code == 200, generic_events.text
        assert "task.result" in generic_events.text
        assert "task.completed" in generic_events.text
        generic_result = client.get(f"{root}/harness/tasks/{generic_task['task_id']}/result")
        assert generic_result.status_code == 200, generic_result.text
        assert generic_result.json()["result_schema"] == "skill-execution.v1"
        assert generic_result.json()["result"]["skill_id"] == "api-parsing"

        # Below the HTTP cap, but above the ordinary 128-window safe budget:
        # the API must stage the input before the independent Host runs it.
        medium_text = "登录\n" + "x" * 500000
        medium_document = client.post(f"{root}/documents", files={
            "file": ("medium.md", medium_text.encode("utf-8")),
        }, headers=headers)
        assert medium_document.status_code == 200, medium_document.text
        medium = client.post(f"{root}/harness/tasks", json={
            "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
            "source_document_ids": [medium_document.json()["id"]],
            "idempotency_key": "node-host-medium-staged-e2e",
        }, headers=headers)
        assert medium.status_code == 200, medium.text
        medium_task = medium.json()
        medium_events = client.get(f"{root}/harness/tasks/{medium_task['task_id']}/events?after={medium_task['sequence']}")
        assert medium_events.status_code == 200, medium_events.text
        assert "chunk_started" in medium_events.text and "reduce_completed" in medium_events.text
        assert "task.completed" in medium_events.text, medium_events.text[-1000:]
        medium_result = client.get(f"{root}/harness/tasks/{medium_task['task_id']}/result")
        assert medium_result.status_code == 200, medium_result.text
        assert medium_result.json()["source_document_ids"] == [medium_document.json()["id"]]
        assert not (tmp_path / "sessions" / medium_task["task_id"] / "input").exists()

        # Exercise the API's analysis-only staging against the actual Node
        # Host, including bounded map/reduce and the business result gate.
        with SessionLocal() as db:
            db.add(AnalysisRun(id="run-oversized", project_id=int(root.rsplit("/", 1)[-1]),
                               user_id=1, provider="harness", status="ready", source_ids=[source_id],
                               result={"harness_result": {
                                   "schema": "requirement-analysis.v1", "result_id": "oversized-analysis-result",
                                   "task_id": "oversized-analysis-task", "session_id": "oversized-analysis-session",
                                   "source_document_ids": [source_id], "generated_at": "2026-10-23T00:00:00Z",
                                   "warnings": [],
                                   "data": {"requirements": [
                                       {"id": f"REQ-{index}", "title": "Login", "description": "x" * 40000,
                                        "priority": "medium", "source_refs": [
                                            {"document_id": source_id, "locator": "requirements.md:1"}],
                                        "acceptance_criteria": []}
                                       for index in (1, 2)], "ambiguities": [], "quality_warnings": [],
                                   }}, "requirements": [{"id": f"REQ-{index}"} for index in (1, 2)],
                                   "ambiguities": []}))
            db.commit()
        monkeypatch.setenv("TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES", "65536")
        staged_cases = client.post(f"{root}/harness/tasks", json={
            "task_type": "test-cases", "skill_id": "test-cases", "analysis_run_id": "run-oversized",
            "idempotency_key": "node-host-staged-case-e2e",
        }, headers=headers)
        assert staged_cases.status_code == 200, staged_cases.text
        case_task = staged_cases.json()
        case_events = client.get(f"{root}/harness/tasks/{case_task['task_id']}/events?after={case_task['sequence']}")
        assert case_events.status_code == 200, case_events.text
        assert "chunk_started" in case_events.text and "reduce_completed" in case_events.text
        case_result = client.get(f"{root}/harness/tasks/{case_task['task_id']}/result")
        assert case_result.status_code == 200, case_result.text
        assert case_result.json()["analysis_run_id"] == case_task["analysis_run_id"]
        assert case_result.json()["project_id"] == analysis_result["project_id"]
        assert case_result.json()["source_document_ids"] == analysis_result["source_document_ids"]
        assert "task.completed" in case_events.text, case_result.json().get("error")
        assert [case["requirement_ids"] for case in case_result.json()["result"]["data"]["cases"]] == [
            ["REQ-1"], ["REQ-2"]]
        assert not (tmp_path / "sessions" / case_task["task_id"] / "input").exists()

        # A bounded Bridge response must not strand a completed Host task
        # behind an ever-growing SSE replay. Each page resumes its remote ID.
        event_document = client.post(f"{root}/documents", files={"file": ("events.md", b"short source")}, headers=headers)
        assert event_document.status_code == 200, event_document.text
        with monkeypatch.context() as paged_env:
            paged_env.setenv("TEST_ATELIER_HARNESS_MAX_RESPONSE_BYTES", "65536")
            paged = client.post(f"{root}/harness/tasks", json={
                "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
                "source_document_ids": [event_document.json()["id"]],
                "idempotency_key": "node-host-paged-sse-e2e",
            }, headers=headers)
            assert paged.status_code == 200, paged.text
            paged_task = paged.json()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                paged_detail = client.get(f"{root}/harness/tasks/{paged_task['task_id']}")
                assert paged_detail.status_code == 200, paged_detail.text
                if paged_detail.json()["status"] in {"completed", "failed", "cancelled"}:
                    break
            assert paged_detail.json()["status"] == "completed", paged_detail.json()
            with SessionLocal() as db:
                assert db.get(AnalysisRun, paged_task["analysis_run_id"]).status == "ready"
            owner = {
                "subject_user_id": 1, "project_id": int(root.rsplit("/", 1)[-1]),
                "task_id": paged_task["task_id"], "allowed_operation": "task.events",
            }
            cursor = 0
            sequences = []
            page_count = 0
            while page_count < 10:
                page = list(harness_bridge.HarnessBridge().stream_events(
                    paged_task["task_id"], cursor, "paged-replay-test", owner,
                ))
                assert page
                page_count += 1
                sequences.extend(item["sequence"] for _, item in page)
                cursor = sequences[-1]
                if page[-1][0] == "task.completed":
                    break
            assert page_count > 1
            assert page[-1][0] == "task.completed"
            assert sequences == list(range(1, len(sequences) + 1))
            assert len(sequences) > 160

            huge_document = client.post(f"{root}/documents", files={"file": ("huge-result.md", b"short")}, headers=headers)
            assert huge_document.status_code == 200, huge_document.text
            huge_task_response = client.post(f"{root}/harness/tasks", json={
                "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
                "source_document_ids": [huge_document.json()["id"]],
                "idempotency_key": "node-host-oversized-result-e2e",
            }, headers=headers)
            assert huge_task_response.status_code == 200, huge_task_response.text
            huge_task = huge_task_response.json()
            huge_events = client.get(f"{root}/harness/tasks/{huge_task['task_id']}/events?after=0")
            assert "output_too_large" in huge_events.text
            assert "task.result" not in huge_events.text
            huge_result = client.get(f"{root}/harness/tasks/{huge_task['task_id']}/result")
            assert huge_result.json()["status"] == "failed"
            with SessionLocal() as db:
                assert db.get(AnalysisRun, huge_task["analysis_run_id"]).status == "failed"

        # Restart the independently deployed Host without clearing its two
        # persistent roots. Local business projection must stay unchanged and
        # the Host must still authorize the original event/Session replays.
        host.terminate()
        host.wait(timeout=5)
        host = start_host()
        restored = client.get(f"{root}/harness/tasks/{task['task_id']}/result")
        assert restored.status_code == 200, restored.text
        assert restored.json()["result"]["result_id"] == "e2e-result"
        with SessionLocal() as db:
            run = db.get(AnalysisRun, task["analysis_run_id"])
            assert run.status == "ready"
            assert run.result["harness_result"]["result_id"] == "e2e-result"
            owner_id = db.get(HarnessTask, task["task_id"]).user_id
        project_id = int(root.rsplit("/", 1)[-1])
        scope = {
            "subject_user_id": owner_id,
            "project_id": project_id,
            "task_id": task["task_id"],
            "allowed_operation": "task.events",
        }
        events_after_restart = list(harness_bridge.HarnessBridge().stream_events(
            task["task_id"], 0, "host-restart-replay", scope,
        ))
        assert [event for event, _ in events_after_restart].count("task.result") == 1
        assert [event for event, _ in events_after_restart].count("task.completed") == 1
        session = client.get(f"{root}/harness/tasks/{task['task_id']}/session")
        assert session.status_code == 200, session.text
        assert session.json()["session_id"] == restored.json()["result"]["session_id"]
        assert session.json()["events"][0]["type"] == "assistant/message"
        unrelated = client.get(f"{root}/harness/tasks/{task['task_id']}/session?session_id=not-owned")
        assert unrelated.status_code == 409

        host.terminate()
        host.wait(timeout=5)
        unavailable = client.delete(root, headers=headers)
        assert unavailable.status_code == 503, unavailable.text
        with SessionLocal() as db:
            assert db.get(Project, project_id).deletion_pending_at
            assert db.get(HarnessTask, task["task_id"]) is not None
        host = start_host()
        deleted = client.delete(root, headers=headers)
        assert deleted.status_code == 200, deleted.text
        with SessionLocal() as db:
            assert db.get(Project, project_id) is None
            assert db.get(HarnessTask, task["task_id"]) is None
        for task_id in (task["task_id"], generic_task["task_id"], medium_task["task_id"], case_task["task_id"],
                        paged_task["task_id"], huge_task["task_id"]):
            assert not (tmp_path / "tasks" / f"{task_id}.json").exists()
            assert not (tmp_path / "tasks" / f"{task_id}.events.json").exists()
            assert not (tmp_path / "sessions" / task_id).exists()
            assert (tmp_path / "tasks" / f"{task_id}.deleted.json").exists()
    finally:
        host.terminate()
        host.wait(timeout=5)
