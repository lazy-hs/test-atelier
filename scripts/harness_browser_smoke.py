#!/usr/bin/env python3
"""Run a disposable Harness -> API -> browser integration flow.

This is a deterministic protocol/UI smoke, not a model-quality check. It uses
an isolated SQLite database and normally a fake DSH process. The opt-in real
dsh mode requires a local-only mock model and never reads a real model key.
"""

from __future__ import annotations

import os
import json
import re
import sqlite3
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
sys.path.insert(0, str(ROOT))
from scripts.harness_smoke_process import spawn_isolated, stop_tree  # noqa: E402
from scripts.verify_project_export import verify as verify_project_export  # noqa: E402


def sibling_checkout(name: str) -> Path:
    """Resolve the conventional sibling checkout without a machine path."""
    return ROOT.parent / name


def real_dsh_args(harness_root: Path, protocol: str, *, platform: str | None = None) -> list[str]:
    if protocol not in {"headless", "acp"}:
        raise ValueError("Unsupported Harness protocol")
    if (platform or sys.platform) == "win32":
        try:
            script = json.loads((harness_root / "package.json").read_text(encoding="utf-8"))["scripts"]["dsh"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("Pinned Harness dsh script requires compatibility review") from exc
        match = re.fullmatch(r"node --import tsx/esm (apps/cli/[A-Za-z0-9_./-]+\.ts)", script)
        target = harness_root / match.group(1) if match else None
        if (not match or ".." in match.group(1).split("/") or not target.is_file()
                or target.is_symlink() or not target.resolve().is_relative_to(harness_root.resolve())):
            raise ValueError("Pinned Harness dsh script requires compatibility review")
        args = ["--import", "tsx/esm", match.group(1)]
    else:
        args = ["pnpm@11.28.5", "--dir", str(harness_root), "dsh"]
    args += ["--profile", "test-atelier-acp" if protocol == "acp" else "test-atelier"]
    if protocol == "headless":
        args.append("--json")
    return args


EXTENSIONS = Path(os.getenv("HARNESS_EXTENSIONS_ROOT", str(sibling_checkout("test-atelier-harness-extensions"))))
SKILL_ROOT = Path(os.getenv("TEST_ATELIER_SKILL_ROOT", str(sibling_checkout("qa-doc-skills"))))


def browser_smoke_environment(source: dict[str, str]) -> dict[str, str]:
    """Pass only process essentials and explicit smoke controls to child processes."""
    allowed = (
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP",
        "SystemRoot", "WINDIR", "ComSpec", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "COREPACK_HOME", "PLAYWRIGHT_BROWSERS_PATH",
        "CI", "NO_COLOR", "FORCE_COLOR",
        "HARNESS_DSH_PROTOCOL", "HARNESS_ANALYSIS_CHECKPOINTS", "HARNESS_CASE_CHECKPOINTS",
        "TEST_ATELIER_HARNESS_CHUNK_CHARS", "TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS",
        "TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES",
        "TA_LOCAL_SMOKE_CHUNKED", "TA_LOCAL_SMOKE_STAGED", "TA_LOCAL_SMOKE_ALL_GENERIC",
        "TA_LOCAL_SMOKE_STAGED_CASES", "TA_LOCAL_SMOKE_FRAGMENT_CASES",
        "TA_LOCAL_SMOKE_CHECKPOINT", "TA_LOCAL_SMOKE_CASE_CHECKPOINT",
        "TA_LOCAL_SMOKE_CASE_EXCEPTION", "TA_LOCAL_SMOKE_CASE_USER_EXCEPTION",
    )
    return {name: source[name] for name in allowed if isinstance(source.get(name), str)}


def real_smoke_host_environment(source: dict[str, str], token_file: Path) -> dict[str, str]:
    """Give the real-model Host only its selected provider and disposable token."""
    env = {
        **browser_smoke_environment(source),
        "OPENAI_API_KEY": source["OPENAI_API_KEY"],
        "TEST_ATELIER_HARNESS_TOKEN": "",
        "TEST_ATELIER_HARNESS_TOKEN_FILE": str(token_file),
    }
    if source.get("TEST_ATELIER_SKILL_MEMORY_ROOT"):
        env["TEST_ATELIER_SKILL_MEMORY_ROOT"] = source["TEST_ATELIER_SKILL_MEMORY_ROOT"]
    return env


def is_loopback_mock_model(source: dict[str, str]) -> bool:
    if source.get("OPENAI_API_KEY") != "local-mock-model-only":
        return False
    try:
        url = urlsplit(source.get("OPENAI_API_BASE", ""))
        return (url.scheme == "http" and url.hostname == "127.0.0.1" and url.port is not None
                and url.username is None and url.password is None)
    except ValueError:
        return False

FAKE_DSH = r"""
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
const prompt = process.argv.at(-1) || ''
const taskId = process.env.DSH_TASK_ID
// Host-provided DSH_SESSION_ID is a placeholder; dsh allocates its own Session.
const sessionId = `dsh-${taskId}`
const taskInput = JSON.parse(prompt.slice(prompt.lastIndexOf('\n\n') + 2))
const sourceId = taskInput.source_documents?.[0]?.id || taskInput.analysis?.requirements?.[0]?.source_refs?.[0]?.document_id || 'missing-source'
const analysisId = taskInput.analysis?.analysis_run_id || null
const taskType = prompt.match(/task type ([a-z-]+)/)?.[1] || ''
const skillId = prompt.match(/exact name ([a-z-]+)/)?.[1] || 'api-parsing'
const root = process.env.HARNESS_SESSION_ROOT
const taskSessionRoot = join(root, taskId)
mkdirSync(taskSessionRoot, { recursive: true })
writeFileSync(join(taskSessionRoot, 'session.v4.jsonl'), [
  JSON.stringify({type:'session', version:4, id:sessionId, createdAt:Date.now(), cwd:process.cwd()}),
  JSON.stringify({type:'assistant/message', seq:1, data:{content:[{type:'text', text:'fake Harness result'}]}}),
].join('\n') + '\n')
console.log(JSON.stringify({type:'session', sessionId}))
const cases = [
  {id:'TC-1', title:'有效账号登录', module_path:['登录','主路径'], skill_priority:'P1', requirement_ids:['REQ-1'], preconditions:['存在有效账号'], steps:[{order:1, action:'输入正确账号密码', expected:'进入首页'}], expected:'进入首页', method:'EP', priority:'medium', labels:['Web'], traceability:[{requirement_id:'REQ-1', acceptance_criterion_id:'AC-1'}], quality_warnings:[]},
  {id:'TC-2', title:'登录请求中断', module_path:['风险点','网络异常'], skill_priority:'P2', requirement_ids:['REQ-1'], preconditions:['存在有效账号'], steps:[{order:1, action:'登录时中断网络', expected:'显示可恢复提示'}], expected:'不重复创建登录态', method:'EG', priority:'medium', labels:['Web'], traceability:[{requirement_id:'REQ-1', acceptance_criterion_id:'AC-1'}], quality_warnings:[]},
]
const result = taskType === 'skill-execution' ? {
  schema:'skill-execution.v1', result_id:'fake-skill', task_id:taskId, session_id:sessionId,
  skill_id:skillId, source_document_ids:[sourceId], generated_at:new Date().toISOString(), warnings:[],
  data:skillId === 'figma-to-prd'
    ? {output:'# 产品需求文档（PRD）：回环夹具设计证据\n\n待确认：真实 Figma 读稿未执行'}
    : {summary:'fake generic skill result', findings:[{title:'source inspected', source_document_id:sourceId}]}
} : analysisId ? {
  schema:'test-cases.v1', result_id:'fake-cases', task_id:taskId, session_id:sessionId,
  source_document_ids:[sourceId], analysis_run_id:analysisId, generated_at:new Date().toISOString(),
  warnings:[], data:{cases}
} : {
  schema:'requirement-analysis.v1', result_id:'fake-analysis', task_id:taskId, session_id:sessionId,
  source_document_ids:[sourceId], generated_at:new Date().toISOString(), warnings:[],
  report_markdown:'# 登录需求分析\n\n正确账号可以进入首页\n\n![外链图示](https://harness-asset-probe.invalid/image.png?fixture=report)\n',
  data:{requirements:[{id:'REQ-1', title:'登录', description:'正确账号可以进入首页', priority:'medium',
    source_refs:[{document_id:sourceId, locator:'body'}], acceptance_criteria:[{id:'AC-1', text:'进入首页', source_refs:[{document_id:sourceId, locator:'body'}]}]}],
    ambiguities:[], quality_warnings:[]}
}
console.log(JSON.stringify({type:'final', text:JSON.stringify(result)}))
""".strip()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, process: subprocess.Popen[str], timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process exited before ready: {url}")
        try:
            with urlopen(url, timeout=1):
                return
        except Exception:
            time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {url}")


def tail(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return ""


def stop_processes(processes: list[subprocess.Popen[str]]) -> None:
    failures = []
    for process in reversed(processes):
        try:
            stop_tree(process)
        except RuntimeError as exc:
            failures.append(str(exc))
    if failures:
        raise RuntimeError("; ".join(failures))
    processes.clear()


def verify_recovered_tasks(base_url: str, database: Path, source_data: Path, password: str,
                           source_database: Path) -> None:
    with sqlite3.connect(database) as connection:
        tasks = connection.execute(
            "SELECT id, project_id, task_type, skill_id FROM harness_tasks "
            "WHERE status = 'completed' AND task_type IN ('requirement-analysis', 'test-cases', 'skill-execution')"
        ).fetchall()
        documents = connection.execute("SELECT id, project_id FROM source_documents").fetchall()
    expected_schemas = {
        "requirement-analysis": "requirement-analysis.v1",
        "test-cases": "test-cases.v1",
        "skill-execution": "skill-execution.v1",
    }
    assert {task[2] for task in tasks} == set(expected_schemas), tasks
    assert documents, "no uploaded source document was archived"
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    with opener.open(f"{base_url}/api/v1/auth/csrf", timeout=5) as response:
        csrf = json.load(response)["token"]
    login = Request(
        f"{base_url}/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-csrf-token": csrf}, method="POST",
    )
    with opener.open(login, timeout=5) as response:
        assert response.status == 200
        csrf = json.load(response)["csrf"]
    for document_id, project_id in documents:
        original = source_data / "documents" / str(project_id) / f"{document_id}.bin"
        with opener.open(
            f"{base_url}/api/v1/projects/{project_id}/documents/{document_id}/download", timeout=5,
        ) as response:
            assert response.read() == original.read_bytes()
    reports_by_project: dict[int, dict[str, str]] = {}
    for task_id, project_id, task_type, skill_id in tasks:
        root = f"{base_url}/api/v1/projects/{project_id}/harness/tasks/{task_id}"
        with opener.open(root + "/result", timeout=5) as response:
            result = json.load(response)
        expected = expected_schemas[task_type]
        assert result["status"] == "completed" and result["result"]["schema"] == expected
        if task_type == "skill-execution":
            assert skill_id in {"api-parsing", "dev-doc-analysis", "figma-to-prd"}
            assert result["result"]["skill_id"] == skill_id
        if task_type == "requirement-analysis":
            report = result["result"].get("report_markdown")
            assert isinstance(report, str) and report.lstrip().startswith("# ")
            with opener.open(root + "/report.md", timeout=5) as response:
                assert response.read().decode("utf-8") == report
            reports_by_project.setdefault(project_id, {})[task_id] = report
        with opener.open(root + "/session", timeout=5) as response:
            replay = json.load(response)
        assert replay["session_id"] == result["result"]["session_id"]
        assert replay["events"], f"restored {task_type} Session is empty"
    for project_id, expected in reports_by_project.items():
        with opener.open(f"{base_url}/api/v1/projects/{project_id}/evidence/export", timeout=20) as response:
            content = response.read(32 * 1024 * 1024 + 1)
        assert len(content) <= 32 * 1024 * 1024, "disposable evidence exceeded the smoke budget"
        archive_path = source_data.parent / f"restored-evidence-{project_id}.zip"
        archive_path.write_bytes(content)
        assert verify_project_export(archive_path)["counts"]["analysis_report"] == len(expected)
        with zipfile.ZipFile(archive_path) as archive:
            for task_id, report in expected.items():
                assert archive.read(f"analysis_report/{task_id}.md").decode("utf-8") == report

    with sqlite3.connect(source_database) as original, sqlite3.connect(database) as restored:
        original_runs = original.execute(
            "SELECT id, project_id, status, title, suite_revision, started_at FROM execution_runs ORDER BY id"
        ).fetchall()
        restored_runs = restored.execute(
            "SELECT id, project_id, status, title, suite_revision, started_at FROM execution_runs ORDER BY id"
        ).fetchall()
        assert original_runs == restored_runs, "execution runs changed during stopped backup/restore"
        original_cases = original.execute(
            "SELECT id, run_id, result, note, version FROM run_cases ORDER BY id"
        ).fetchall()
        restored_cases = restored.execute(
            "SELECT id, run_id, result, note, version FROM run_cases ORDER BY id"
        ).fetchall()
        assert original_cases == restored_cases, "execution cases changed during stopped backup/restore"
        assert any(row[2] == "active" for row in original_runs), "browser did not leave an active run for recovery"
        assert any(row[2] == "completed" for row in original_runs), "browser did not freeze a completed report"
        original_reports = dict(original.execute("SELECT run_id, payload FROM execution_reports"))
        restored_reports = dict(restored.execute("SELECT run_id, payload FROM execution_reports"))
        assert original_reports == restored_reports, "frozen reports changed during stopped backup/restore"

    for run_id, project_id, status, title, suite_revision, started_at in original_runs:
        root = f"{base_url}/api/v1/projects/{project_id}/execution-runs/{run_id}"
        with opener.open(root, timeout=5) as response:
            run = json.load(response)
        assert (run["status"], run["title"], run["suite_revision"], run["started_at"]) == (
            status, title, suite_revision, started_at)
        expected_cases = [row for row in original_cases if row[1] == run_id]
        assert [(row["id"], run_id, row["result"], row["note"], row["version"]) for row in run["items"]] == expected_cases
        if status == "completed":
            with opener.open(root + "/report", timeout=5) as response:
                assert json.load(response) == json.loads(original_reports[run_id])
            continue
        assert run_id not in original_reports and len(expected_cases) == 1
        item = run["items"][0]
        assert item["result"] == "not_run"
        result = Request(
            root + f"/cases/{item['id']}",
            data=json.dumps({"version": item["version"], "result": "skipped", "actual_seconds": 0,
                             "note": "停写备份恢复后继续执行"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-csrf-token": csrf}, method="PUT",
        )
        with opener.open(result, timeout=5) as response:
            assert json.load(response)["summary"]["skipped"] == 1
        complete = Request(root + "/complete", data=b"", headers={"x-csrf-token": csrf}, method="POST")
        with opener.open(complete, timeout=5) as response:
            assert json.load(response)["status"] == "completed"
        with opener.open(root + "/report", timeout=5) as response:
            report = json.load(response)
        assert report["source"]["started_at"] == started_at
        assert report["summary"]["results"]["skipped"] == 1
        assert report["cases"][0]["note"] == "停写备份恢复后继续执行"
        with opener.open(f"{base_url}/api/v1/projects/{project_id}/evidence/export", timeout=20) as response:
            content = response.read(32 * 1024 * 1024 + 1)
        assert len(content) <= 32 * 1024 * 1024, "recovered execution evidence exceeded the smoke budget"
        archive_path = source_data.parent / f"restored-execution-{run_id}.zip"
        archive_path.write_bytes(content)
        assert verify_project_export(archive_path)["counts"]["execution_report"] >= 2


def restore_and_smoke(temp: Path, env: dict[str, str], host_env: dict[str, str],
                      node: str, password: str, recovery_port: int,
                      processes: list[subprocess.Popen[str]], handles: list) -> None:
    database = temp / "test.db"
    backups = temp / "backups"
    command = [sys.executable, "scripts/backup_data.py"]
    created = subprocess.run([
        *command, "backup", "--database-url", env["DATABASE_URL"],
        "--data-dir", str(temp / "data"), "--output-dir", str(backups),
        "--harness-task-dir", str(temp / "tasks"),
        "--harness-session-dir", str(temp / "sessions"), "--services-stopped",
    ], cwd=ROOT, check=False, capture_output=True, text=True)
    if created.returncode:
        raise RuntimeError(f"Harness backup failed: {created.stderr[-800:]}")
    archive = backups / json.loads(created.stdout)["archive"]
    subprocess.run([*command, "verify", str(archive)], cwd=ROOT, check=True,
                   capture_output=True, text=True)
    destination = temp / "recovered"
    restored_db = destination / "data" / database.name
    subprocess.run([
        *command, "restore", str(archive), "--database-path", str(restored_db),
        "--data-dir", str(destination / "data"),
        "--harness-task-dir", str(destination / "tasks"),
        "--harness-session-dir", str(destination / "sessions"), "--services-stopped",
    ], cwd=ROOT, check=True, capture_output=True, text=True)

    with sqlite3.connect(restored_db) as connection:
        identifiers = [row[0] for row in connection.execute("SELECT id FROM harness_tasks")]
    assert identifiers, "no Harness task was archived"
    for task_id in identifiers:
        event_name = f"{task_id}.events.json"
        assert (destination / "tasks" / event_name).read_bytes() == (temp / "tasks" / event_name).read_bytes()

    with sqlite3.connect(restored_db) as connection:
        project_ids = [row[0] for row in connection.execute("SELECT DISTINCT project_id FROM harness_tasks")]
    for project_id in project_ids:
        inventory = subprocess.run([
            sys.executable, "scripts/audit_harness_project_storage.py",
            "--project-id", str(project_id), "--database-url", f"sqlite:///{restored_db}",
            "--task-root", str(destination / "tasks"), "--session-root", str(destination / "sessions"),
        ], cwd=ROOT, check=False, capture_output=True, text=True)
        assert inventory.returncode == 0, f"Recovered Harness storage differs from the database: {inventory.stdout or inventory.stderr}"
        assert json.loads(inventory.stdout)["legacy_layout_sessions"] == 0, "New Host task did not own its Session directory"

    recovered_env = {**env, "DATABASE_URL": f"sqlite:///{restored_db}",
                     "AI_STUDIO_DATA_DIR": str(destination / "data")}
    recovered_host_env = {**host_env, "HARNESS_TASK_ROOT": str(destination / "tasks"),
                          "HARNESS_SESSION_ROOT": str(destination / "sessions")}
    handles.append((temp / "recovered-api.log").open("w", encoding="utf-8"))
    api = spawn_isolated([
        sys.executable, "-m", "api.uvicorn_entrypoint", "--host", "127.0.0.1", "--port", str(recovery_port),
    ], cwd=ROOT, env=recovered_env, stdout=handles[-1], stderr=subprocess.STDOUT, text=True)
    processes.append(api)
    handles.append((temp / "recovered-host.log").open("w", encoding="utf-8"))
    host = spawn_isolated([node, "host/server.mjs"], cwd=EXTENSIONS, env=recovered_host_env,
                            stdout=handles[-1], stderr=subprocess.STDOUT, text=True)
    processes.append(host)
    base_url = f"http://127.0.0.1:{recovery_port}"
    wait_http(base_url + "/api/v1/health/ready", api)
    wait_http(f"http://127.0.0.1:{host_env['TEST_ATELIER_HARNESS_PORT']}/health", host)
    verify_recovered_tasks(base_url, restored_db, temp / "data", password, database)
    verify_recovered_analysis_deletion(base_url, restored_db, destination, password,
                                       host, recovered_host_env, node, temp, processes, handles)
    print("Stopped backup/restore smoke: Harness Sessions, evidence, frozen reports and active execution recovery verified")


def verify_recovered_analysis_deletion(base_url: str, database: Path, storage: Path, password: str,
                                       host: subprocess.Popen[str], host_env: dict[str, str], node: str,
                                       temp: Path, processes: list[subprocess.Popen[str]], handles: list) -> None:
    """Recover a deletion after a real Host outage without losing its business rows."""
    with sqlite3.connect(database) as connection:
        candidate = connection.execute("""
            SELECT a.id, a.project_id FROM analysis_runs a
            JOIN harness_tasks t ON t.analysis_run_id = a.id
            WHERE a.provider = 'harness' AND a.status = 'ready'
            GROUP BY a.id, a.project_id
            HAVING SUM(CASE WHEN t.task_type = 'test-cases' THEN 1 ELSE 0 END) >= 1
               AND SUM(CASE WHEN t.status NOT IN ('completed', 'failed', 'cancelled') THEN 1 ELSE 0 END) = 0
            ORDER BY a.id LIMIT 1
        """).fetchone()
        assert candidate, "recovered project has no completed analysis with case tasks"
        analysis_id, project_id = candidate
        task_ids = [row[0] for row in connection.execute(
            "SELECT id FROM harness_tasks WHERE analysis_run_id = ? ORDER BY id", (analysis_id,))]
        cases_before = connection.execute("SELECT COUNT(*) FROM cases WHERE project_id = ?", (project_id,)).fetchone()[0]
    assert len(task_ids) >= 2
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    with opener.open(f"{base_url}/api/v1/auth/csrf", timeout=5) as response:
        csrf = json.load(response)["token"]
    login = Request(f"{base_url}/api/v1/auth/login",
                    data=json.dumps({"username": "admin", "password": password}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "x-csrf-token": csrf}, method="POST")
    with opener.open(login, timeout=5) as response:
        csrf = json.load(response)["csrf"]
    deletion = Request(f"{base_url}/api/v1/projects/{project_id}/analyses/{analysis_id}",
                       headers={"x-csrf-token": csrf}, method="DELETE")
    stop_tree(host)
    processes.remove(host)
    try:
        with opener.open(deletion, timeout=10):
            raise AssertionError("analysis deletion succeeded while the Host was stopped")
    except HTTPError as exc:
        try:
            assert exc.code == 503, f"unexpected analysis deletion failure: {exc.code}"
        finally:
            exc.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM analysis_runs WHERE id = ?", (analysis_id,)).fetchone() == ("deleting",)
        assert [row[0] for row in connection.execute(
            "SELECT id FROM harness_tasks WHERE analysis_run_id = ? ORDER BY id", (analysis_id,))] == task_ids
    for task_id in task_ids:
        assert (storage / "tasks" / f"{task_id}.json").is_file()
        assert (storage / "sessions" / task_id).is_dir()

    handles.append((temp / "recovered-host-restarted.log").open("w", encoding="utf-8"))
    restarted = spawn_isolated([node, "host/server.mjs"], cwd=EXTENSIONS, env=host_env,
                                stdout=handles[-1], stderr=subprocess.STDOUT, text=True)
    processes.append(restarted)
    wait_http(f"http://127.0.0.1:{host_env['TEST_ATELIER_HARNESS_PORT']}/health", restarted)
    with opener.open(deletion, timeout=60) as response:
        assert json.load(response) == {"id": analysis_id, "deleted": 1}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT 1 FROM analysis_runs WHERE id = ?", (analysis_id,)).fetchone() is None
        assert connection.execute("SELECT 1 FROM harness_tasks WHERE analysis_run_id = ?", (analysis_id,)).fetchone() is None
        assert connection.execute("SELECT COUNT(*) FROM cases WHERE project_id = ?", (project_id,)).fetchone()[0] == cases_before
    for task_id in task_ids:
        assert not (storage / "tasks" / f"{task_id}.json").exists()
        assert not (storage / "tasks" / f"{task_id}.events.json").exists()
        assert not (storage / "sessions" / task_id).exists()
    print("Recovered Harness analysis deletion: Host outage retained intent, restart removed tasks and Sessions")


def verify_detached_projection(base_url: str, database: Path, password: str) -> None:
    """Submit after Playwright exits; only the independent Projector may finish it."""
    with sqlite3.connect(database) as connection:
        document_id, project_id = connection.execute(
            "SELECT id, project_id FROM source_documents ORDER BY created_at LIMIT 1"
        ).fetchone()
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    with opener.open(f"{base_url}/api/v1/auth/csrf", timeout=5) as response:
        csrf = json.load(response)["token"]
    login = Request(
        f"{base_url}/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-csrf-token": csrf}, method="POST",
    )
    with opener.open(login, timeout=5) as response:
        csrf = json.load(response)["csrf"]
    task = Request(
        f"{base_url}/api/v1/projects/{project_id}/harness/tasks",
        data=json.dumps({
            "task_type": "requirement-analysis", "task_version": "v1",
            "source_document_ids": [document_id], "skill_id": "requirement-analysis",
            "input": {"language": "zh-CN", "output_mode": "structured"},
            "idempotency_key": f"detached-{secrets.token_hex(12)}",
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-csrf-token": csrf}, method="POST",
    )
    with opener.open(task, timeout=10) as response:
        task_id = json.load(response)["task_id"]

    deadline = time.monotonic() + 60
    confirmed = False
    ambiguity_decided = False
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as connection:
            state = connection.execute(
                "SELECT t.status, a.status, t.session_id FROM harness_tasks t "
                "JOIN analysis_runs a ON a.id = t.analysis_run_id WHERE t.id = ?", (task_id,),
            ).fetchone()
        if state and state[0] == "completed":
            if os.getenv("TA_LOCAL_SMOKE_CHECKPOINT") == "1":
                assert confirmed and ambiguity_decided, "Detached analysis bypassed Phase 2.5 or Phase 2.6 confirmation"
            assert state[1] == "ready" and state[2] and state[2] != f"ta-{task_id}", state
            print("Harness detached projector: analysis completed without browser polling")
            return
        if state and state[0] in {"failed", "cancelled"}:
            raise AssertionError(f"Detached Harness task ended as {state[0]}")
        if state and state[0] == "waiting_confirmation" and os.getenv("TA_LOCAL_SMOKE_CHECKPOINT") == "1":
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT payload FROM harness_task_events WHERE task_id = ? AND event = 'task.confirmation_required' "
                    "ORDER BY sequence DESC LIMIT 1", (task_id,),
                ).fetchone()
            confirmation = json.loads(row[0])["confirmation"] if row else {}
            kind = confirmation.get("kind")
            if kind == "skill_checkpoint":
                assert not confirmed, "Repeated Phase 2.5 checkpoint"
                payload = {"confirmation_id": confirmation["confirmation_id"], "decision": "approve",
                           "comment": "仅 Web 适用"}
            else:
                assert kind == "ambiguity_checkpoint" and confirmed and not ambiguity_decided, confirmation
                assert confirmation.get("source_text") == "连续失败五次后锁定账号", confirmation
                payload = {"confirmation_id": confirmation["confirmation_id"], "decision": "approve",
                           "ambiguity_choice": "mark_pending"}
            decision = Request(
                f"{base_url}/api/v1/projects/{project_id}/harness/tasks/{task_id}/confirm",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-csrf-token": csrf}, method="POST",
            )
            with opener.open(decision, timeout=10) as response:
                assert json.load(response)["status"] == "running"
            if kind == "skill_checkpoint":
                confirmed = True
            else:
                ambiguity_decided = True
        time.sleep(0.25)
    raise TimeoutError("Detached Harness task was not projected without a browser")


def main() -> int:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise SystemExit("node and npm are required")
    if not (EXTENSIONS / "host/server.mjs").is_file():
        raise SystemExit(f"Harness extensions checkout not found: {EXTENSIONS}")
    real_dsh = os.getenv("TEST_ATELIER_HARNESS_BROWSER_REAL_DSH") == "1"
    protocol = os.getenv("HARNESS_DSH_PROTOCOL", "headless").strip().lower()
    if protocol not in {"headless", "acp"} or (protocol == "acp" and not real_dsh):
        raise SystemExit("ACP browser smoke requires the real dsh loopback-model mode")
    if real_dsh:
        if not is_loopback_mock_model(os.environ):
            raise SystemExit("Real dsh browser smoke requires the loopback mock model")
        harness_root = Path(os.getenv("HARNESS_ROOT", str(sibling_checkout("deepseek-harness"))))
        if not (harness_root / "package.json").is_file() or not shutil.which("corepack"):
            raise SystemExit("The local Harness checkout and corepack are required")

    secret = secrets.token_urlsafe(32)
    password = f"browser-{secrets.token_urlsafe(12)}"
    ports = {"api": free_port(), "host": free_port(), "web": free_port(), "recovery": free_port()}
    with tempfile.TemporaryDirectory(prefix="test-atelier-harness-browser-") as raw:
        temp = Path(raw)
        scope_file = temp / "scope-secret"
        scope_file.write_bytes(secrets.token_urlsafe(40).encode("ascii") + b"\r\n")
        scope_file.chmod(0o600)
        host_token_file = temp / "host-api-token"
        host_token_file.write_bytes(secrets.token_urlsafe(32).encode("ascii") + b"\r\n")
        host_token_file.chmod(0o600)
        fake = temp / "fake-dsh.mjs"
        if not real_dsh:
            fake.write_text(FAKE_DSH, encoding="utf-8")
        logs = {name: temp / f"{name}.log" for name in ("api", "host", "vite")}
        base_env = {
            **browser_smoke_environment(os.environ),
            "AI_STUDIO_ENV": "development",
            "AI_STUDIO_REQUIRE_MIGRATIONS": "1",
            "AI_STUDIO_SESSION_SECRET": secret,
            "TEST_ATELIER_SCOPE_SECRET": "",
            "TEST_ATELIER_SCOPE_SECRET_FILE": str(scope_file),
            "TEST_ATELIER_HARNESS_TOKEN": "",
            "TEST_ATELIER_HARNESS_TOKEN_FILE": str(host_token_file),
            "TEST_ATELIER_HARNESS_ONLY": "1",
            "AI_STUDIO_ADMIN_PASSWORD": password,
            "DATABASE_URL": f"sqlite:///{temp / 'test.db'}",
            "AI_STUDIO_DATA_DIR": str(temp / "data"),
            "TEST_ATELIER_SKILL_ROOT": str(SKILL_ROOT),
            "TEST_ATELIER_HARNESS_URL": f"http://127.0.0.1:{ports['host']}",
            "TEST_ATELIER_HARNESS_PROJECTOR_ENABLED": "1",
            "TEST_ATELIER_HARNESS_PROJECTOR_HEARTBEAT": str(temp / "projector-heartbeat"),
        }
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=base_env,
                       check=True, capture_output=True)
        processes: list[subprocess.Popen[str]] = []
        handles = []
        try:
            handles.append(logs["api"].open("w", encoding="utf-8"))
            api = spawn_isolated(
                [sys.executable, "-m", "api.uvicorn_entrypoint", "--host", "127.0.0.1", "--port", str(ports["api"])],
                cwd=ROOT, env=base_env, stdout=handles[-1], stderr=subprocess.STDOUT, text=True,
            )
            processes.append(api)

            host_env = {**base_env, "TEST_ATELIER_HARNESS_PORT": str(ports["host"]),
                        "HARNESS_DSH_COMMAND": node,
                        "HARNESS_DSH_ARGS_JSON": json.dumps([str(fake)]), "HARNESS_TASK_ROOT": str(temp / "tasks"),
                        "HARNESS_SESSION_ROOT": str(temp / "sessions")}
            if real_dsh:
                host_env.update(
                    DSH_HOME=str(temp / "dsh"), HARNESS_ROOT=str(harness_root),
                    HARNESS_DSH_COMMAND=node if sys.platform == "win32" else "corepack", HARNESS_DSH_INPUT_MODE="stdin",
                    HARNESS_DSH_PROTOCOL=protocol,
                    HARNESS_DSH_ARGS_JSON=json.dumps(real_dsh_args(harness_root, protocol)),
                    OPENAI_API_BASE=os.environ["OPENAI_API_BASE"],
                    OPENAI_API_KEY="local-mock-model-only", OPENAI_MODEL="ugreen-ai-model",
                )
                subprocess.run([
                    sys.executable, str(EXTENSIONS / "scripts/install_profile.py"),
                    "--harness-root", str(harness_root), "--dsh-home", host_env["DSH_HOME"],
                ], cwd=EXTENSIONS, env=host_env, check=True, capture_output=True)
                subprocess.run([
                    sys.executable, str(EXTENSIONS / "scripts/preflight_host.py"), "--production",
                ], cwd=EXTENSIONS, env=host_env, check=True, capture_output=True)
            handles.append(logs["host"].open("w", encoding="utf-8"))
            host = spawn_isolated([node, "host/server.mjs"], cwd=EXTENSIONS, env=host_env,
                                    stdout=handles[-1], stderr=subprocess.STDOUT, text=True)
            processes.append(host)

            logs["projector"] = temp / "projector.log"
            handles.append(logs["projector"].open("w", encoding="utf-8"))
            projector = spawn_isolated([sys.executable, "-m", "api.harness_projector"], cwd=ROOT,
                                         env=base_env, stdout=handles[-1], stderr=subprocess.STDOUT, text=True)
            processes.append(projector)
            heartbeat = Path(base_env["TEST_ATELIER_HARNESS_PROJECTOR_HEARTBEAT"])
            deadline = time.monotonic() + 20
            while not heartbeat.is_file() and time.monotonic() < deadline:
                if projector.poll() is not None:
                    raise RuntimeError(f"Harness Projector exited on startup: {tail(logs['projector'])}")
                time.sleep(0.2)
            if not heartbeat.is_file():
                raise TimeoutError(f"Harness Projector did not start: {tail(logs['projector'])}")

            web_env = {**base_env, "AI_STUDIO_API_TARGET": f"http://127.0.0.1:{ports['api']}"}
            handles.append(logs["vite"].open("w", encoding="utf-8"))
            vite = spawn_isolated([npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(ports["web"]), "--strictPort"],
                                    cwd=WEB, env=web_env, stdout=handles[-1], stderr=subprocess.STDOUT, text=True)
            processes.append(vite)

            try:
                wait_http(f"http://127.0.0.1:{ports['api']}/api/v1/health/ready", api)
                wait_http(f"http://127.0.0.1:{ports['web']}/", vite)
            except (TimeoutError, RuntimeError) as exc:
                raise RuntimeError(f"Harness browser services did not become ready; Vite log: {tail(logs['vite'])}") from exc
            test_env = {**browser_smoke_environment(os.environ), "AI_STUDIO_E2E_URL": f"http://127.0.0.1:{ports['web']}",
                        "AI_STUDIO_E2E_PASSWORD": password, "TEST_ATELIER_HARNESS_E2E": "1",
                        "TEST_ATELIER_HARNESS_E2E_DB": str(temp / "test.db"),
                        "TEST_ATELIER_HARNESS_CHECKPOINT_E2E": os.getenv("TA_LOCAL_SMOKE_CHECKPOINT", "0"),
                        "TEST_ATELIER_HARNESS_CASE_CHECKPOINT_E2E": os.getenv("TA_LOCAL_SMOKE_CASE_CHECKPOINT", "0"),
                        "TEST_ATELIER_HARNESS_CASE_AMBIGUITY_E2E": os.getenv("TA_LOCAL_SMOKE_CASE_CHECKPOINT", "0"),
                        "TEST_ATELIER_HARNESS_CASE_EXCEPTION_E2E": os.getenv("TA_LOCAL_SMOKE_CASE_EXCEPTION", "0"),
                        "TEST_ATELIER_HARNESS_CASE_USER_EXCEPTION_E2E": os.getenv("TA_LOCAL_SMOKE_CASE_USER_EXCEPTION", "0"),
                        "TEST_ATELIER_HARNESS_REAL_DSH_E2E": "1" if real_dsh else "0"}
            result = subprocess.run([npm, "run", "test:e2e", "--", "harness.spec.ts", "historical-boundary.spec.ts", "skills-concurrency.spec.ts", "smoke.spec.ts"],
                                    cwd=WEB, env=test_env, check=False)
            if result.returncode:
                print("Harness browser smoke failed; process logs:")
                for name, path in logs.items():
                    print(f"--- {name} ---\n{tail(path)}")
                return result.returncode
            verify_detached_projection(f"http://127.0.0.1:{ports['api']}", temp / "test.db", password)
            stop_processes(processes)
            restore_and_smoke(temp, base_env, host_env, node, password, ports["recovery"], processes, handles)
            return 0
        finally:
            stop_processes(processes)
            for handle in handles:
                handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
