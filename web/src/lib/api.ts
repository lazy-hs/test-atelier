export type User = { id: number; username: string; role: 'admin' | 'reviewer' | 'reader' }
export type Project = { id: number; name: string; role: 'admin' | 'manager' | 'reviewer' | 'reader'; created_at?: string; archived?: boolean; archived_at?: string | null; deletion_pending_at?: string | null }
export type WorkItemKind = 'requirement' | 'tested_requirement' | 'task' | 'test_plan' | 'other'
export type WorkItemStatus = 'todo' | 'in_progress' | 'done'
export type WorkItem = {
  id: number; user_id: number; project_id: number | null; project_name: string | null
  kind: WorkItemKind; title: string; description: string; status: WorkItemStatus
  start_at: string | null; due_at: string | null; completed_at: string | null; created_at: string; updated_at: string
}
export type WorkboardSummary = {
  total: number; active: number; completed: number; overdue: number
  followed_requirements: number; tested_requirements: number; test_plans: number
  test_case_outputs: number; project_count: number
}
export type WorkboardResponse = { summary: WorkboardSummary; items: WorkItem[] }
export type ProjectSkill = { skill_id: string; name: string; label: string; source_path: string; description: string; enabled: boolean; updated_at: string }
export type AiConfig = {
  local_url: string; local_model: string; local_enabled?: boolean; remote_enabled: boolean; remote_url: string; remote_model: string
  /** Server-enforced requirement document upload limit in bytes. */
  max_document_bytes?: number
  remote_api_key_set: boolean; remote_api_key_mask: string
  codex_enabled: boolean; codex_command: string; codex_binary?: string; codex_model: string
  claude_enabled: boolean; claude_command: string; claude_model: string
  /** Legacy mixed catalog retained for older API workers. Prefer models/tools. */
  providers?: { id: string; kind: 'local' | 'remote' | 'cli'; name: string; endpoint: string; model: string; enabled: boolean; detail: string; protocol?: 'openai' | 'responses' | 'anthropic' | 'gemini' | 'system-one'; api_key_set?: boolean }[]
  /** Model connections. kind is the explicit local/remote deployment scope. */
  models?: { id: string; kind: 'local' | 'remote'; name: string; endpoint: string; model: string; enabled: boolean; detail: string; protocol?: 'openai' | 'responses' | 'anthropic' | 'gemini' | 'system-one'; api_key_set?: boolean }[]
  /** Installed AI tools are kept separate from model connections. */
  tools?: { id: 'codex_cli' | 'claude_code' | string; name: string; enabled: boolean; command: string; model: string; detail?: string }[]
}
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '未知'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value.replace('T', ' ').slice(0, 19)
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`
}

export type Case = {
  id: number; original_id: string; title: string; directory: string; level: string; smoke: string
  original: (string | null)[]; final_level: string | null; final_smoke: string | null
  review_status: string; version: number; reviewer: string | null; reviewed_at: string | null
  assignee_id: number | null; assignee: string | null
  final_categories: Record<DimensionKey, string | null> | Record<string, never>
  final_taxonomy_version_id: number | null
  governance_status: 'active' | 'retired' | string
  merged_into: number | null
  pending_suggestion_count: number; created_at: string; updated_at: string
}
export type CaseDetail = Case & { headers: string[] }
export type CaseActivity = {
  events: { id: number; action: string; actor: string; before: Record<string, string | number | null>;
    after: Record<string, string | number | null>; reason: string; created_at: string }[]
  comments: { id: number; actor: string; text: string; created_at: string }[]
}
export type BatchReviewResult = { results: { id: number; status: 'saved' | 'unchanged' | 'conflict' | 'forbidden' | 'invalid' | 'not_found'; detail?: string }[]; saved: number; conflicts: number }
export type DimensionKey = 'business_module' | 'business_scenario' | 'test_dimension' | 'endpoint_type'
export type TaxonomyEntry = { id?: string; name: string; description: string }
export type TaxonomyDefinitions = Record<DimensionKey, TaxonomyEntry[]>
export type TaxonomyVersion = { id: number; number: number; status: 'draft' | 'published'; revision: number; definitions: TaxonomyDefinitions; created_at: string; published_at: string | null }
export type TaxonomyState = { published: TaxonomyVersion | null; draft: TaxonomyVersion | null; versions: { id: number; number: number; status: string; created_at: string; published_at: string | null }[] }
export type CaseSuggestion = { id: number; case_id: number; source: 'rule' | 'model'; status: 'pending' | 'accepted' | 'rejected'; case_version: number; stale: boolean;
  taxonomy_version_id: number | null; categories: Record<DimensionKey, string | null>; category_labels: Record<DimensionKey, string | null>;
  level: string | null; smoke: string | null; rationale: string; questions: string[]; created_at: string; decided_at: string | null; decision_reason: string | null }
export type SuggestionJob = { id: string; project_id: number; provider: string; status: 'queued' | 'running' | 'done' | 'failed' | 'partial'; total: number; completed: number;
  errors: { case_id: number | null; message: string }[]; taxonomy_version_id: number | null; case_ids: number[]; created_at: string }
export type Preview = { id: number; filename: string; headers: string[]; count: number; sample: (string | null)[][]; warnings: string[] }
export type ImportRecord = { id: number; filename: string; count: number; status: string; created_at: string }
export type SourceDocument = { id: string; filename: string; warnings: string[]; created_at: string; text_length: number }
export type Requirement = { id: string; title: string; summary: string; source_ids: string[]; acceptance: string[]; methods: string[] }
export type Ambiguity = { id: string; description: string; critical: boolean; decision: string }
export type Analysis = { id: string; source_ids: string[]; source_filenames?: string[]; title?: string; status: string; provider: string; result: {
  requirements?: Requirement[]; ambiguities?: Ambiguity[]; risks?: string[]; report_markdown?: string; harness_result?: Record<string, unknown>
}; error: string; version: number; created_at: string; updated_at: string }
export type DraftPayload = { module: string[]; title: string; priority: string; precondition: string;
  steps: { action: string; expected: string }[]; requirement_ids: string[]; method: string; label: string; remark: string; smoke: string }
export type Draft = { id: string; analysis_id: string; payload: DraftPayload; status: string; version: number; case_id: number | null }
export type AiJob = { id: string; kind: string; status: string; error: string; attempts: number; analysis_id: string;
  request_id?: string; prompt_calls?: number; prompt_length?: number; prompt_available?: boolean }
export type AiPromptLog = { index: number; provider: string; text: string; length: number }
export type HarnessTask = {
  task_id: string; project_id: number; task_type: 'requirement-analysis' | 'test-cases' | 'skill-execution'; skill_id?: string; task_version: 'v1'
  status: 'queued' | 'starting' | 'running' | 'waiting_confirmation' | 'completed' | 'failed' | 'cancelled'
  session_id: string | null; session_ids: string[]; result_schema: 'requirement-analysis.v1' | 'test-cases.v1' | 'skill-execution.v1'
  source_document_ids: string[]; retry_of: string | null; analysis_run_id: string | null
  sequence: number; created_at: string; updated_at: string
}
export type HarnessTaskResult = { task_id: string; project_id: number; analysis_run_id: string | null; source_document_ids: string[]; status: HarnessTask['status']; result_schema: HarnessTask['result_schema']; result: Record<string, unknown>; error: Record<string, unknown> }
export type HarnessTaskEvent = { task_id: string; sequence: number; [key: string]: unknown }
export type HarnessSessionReplay = {
  session_id: string
  header: Record<string, unknown>
  events: Record<string, unknown>[]
  event_count: number
  truncated: boolean
  format: string
}

/** Keep persisted errors from older workers readable after provider renames. */
export function formatAiError(message: string | null | undefined): string {
  return (message || 'AI 任务失败，请检查模型输出与服务配置后重试')
    .replace(/^codex_cli CLI/, 'Codex CLI')
    .replace(/^claude_code CLI/, 'Claude Code')
}
export type SmokeItem = { id: number; case_id: number; case_version: number; original_id: string; title: string;
  level: string | null; smoke: string | null; stale: boolean; chain_name: string;
  estimated_seconds: number; confirmed_at: string }
export type SmokeSuite = { id: number; project_id: number; title: string; target_version: string;
  environment: string; required_chains: string[]; revision: number; budget_seconds: number;
  estimated_seconds: number; over_budget: boolean; items: SmokeItem[]; covered_chains: string[];
  uncovered_chains: string[]; covered_count: number; required_count: number; created_at: string; updated_at: string }
export type SmokeCandidate = { id: number; original_id: string; title: string; directory: string;
  level: string; version: number }
export type RunResult = 'not_run' | 'passed' | 'failed' | 'blocked' | 'skipped'
export type RunCase = { id: number; case_id: number; case_version: number; original_id: string;
  title: string; level: string; smoke: string; chain_name: string; estimated_seconds: number;
  result: RunResult; actual_seconds: number | null; note: string; executed_by: string | null;
  executed_at: string | null; version: number }
export type ExecutionEvent = { id: number; run_case_id: number; actor: string;
  before: { result: RunResult; actual_seconds: number | null; note: string };
  after: { result: RunResult; actual_seconds: number | null; note: string }; created_at: string }
export type ExecutionRun = { id: number; project_id: number; suite_id: number; suite_revision: number;
  title: string; target_version: string; environment: string; required_chains: string[];
  status: 'active' | 'completed'; started_by: string; started_at: string;
  completed_by: string | null; completed_at: string | null; budget_seconds: number;
  estimated_seconds: number; over_budget: boolean; actual_seconds: number; item_count: number;
  summary: Record<RunResult, number>; items: RunCase[]; events: ExecutionEvent[];
  covered_chains: string[]; uncovered_chains: string[]; covered_count: number; required_count: number }
export type ExecutionStatistics = { project_id: number;
  filters: { suite_id: number | null; target_version: string | null; environment: string | null };
  scope: string; run_count: number; unavailable_runs: number; case_executions: number; results: Record<RunResult, number>;
  pass_rate: number | null; estimated_seconds: number; actual_seconds: number;
  recent_runs: { run_id: number; suite_id: number; suite_revision: number; title: string;
    target_version: string; environment: string; completed_at: string; results: Record<RunResult, number> }[] }
export type ExecutionDefect = { id: number; project_id: number; run_id: number; run_case_id: number;
  source_original_id: string; source_title: string; source_result: 'failed' | 'blocked'; source_case_version: number;
  report_completeness: 'verified' | 'legacy_unverified'; title: string; description: string;
  severity: 'low' | 'medium' | 'high' | 'critical'; status: 'open' | 'in_progress' | 'resolved' | 'closed';
  resolution: string; version: number; created_by_id: number; updated_by_id: number; created_at: string; updated_at: string }
export type DefectCandidate = { run_case_id: number; original_id: string; title: string;
  result: 'failed' | 'blocked'; case_version: number }
export type DefectCandidates = { report_completeness: 'verified' | 'legacy_unverified'; items: DefectCandidate[] }
export type DefectEvent = { id: number; actor_id: number; before: Partial<ExecutionDefect>;
  after: Partial<ExecutionDefect>; created_at: string }
export type KnowledgeSourceType = 'document' | 'analysis' | 'harness_task' | 'case' | 'conversion' | 'execution_report' | 'defect'
export type KnowledgeEntry = { id: number; project_id: number; source_type: KnowledgeSourceType; source_id: string;
  source_snapshot: { title: string; version: number | string; source_integrity?: 'recorded' | 'unrecorded';
    source_sha256?: string; source_size?: number; output_integrity?: 'recorded' | 'unrecorded';
    output_sha256?: string; output_size?: number; result_id?: string; result_snapshot_sha256?: string;
    provider?: string; harness_task_id?: string; analysis_result_snapshot_sha256?: string;
    analysis_run_id?: string; source_analysis_snapshot_sha256?: string;
    [key: string]: unknown }; title: string; body: string;
  status: 'active' | 'archived'; version: number; created_by_id: number; updated_by_id: number;
  created_at: string; updated_at: string }
export type KnowledgeRevision = { version: number; actor_id: number; title: string; body: string;
  status: 'active' | 'archived'; created_at: string }
export type KnowledgeList = { items: KnowledgeEntry[]; next_before_id: number | null }
export type KnowledgeHistory = { items: KnowledgeRevision[]; next_before_version: number | null }
export type ArtifactKind = KnowledgeSourceType | 'import' | 'draft' | 'smoke_suite' | 'smoke_item' |
  'execution_run' | 'run_case' | 'report_case' | 'conversion_input' | 'knowledge' |
  'taxonomy' | 'suggestion_job' | 'suggestion' | 'governance_directory' | 'governance_change' |
  'duplicate_group' | 'duplicate_member' | 'governance_validation' | 'governance_metric' | 'governance_export_job'
export type ArtifactNode = { type: ArtifactKind; id: string;
  title: string; version: number | string; missing?: boolean; sha256?: string;
  frozen?: boolean; original_id?: string; result?: string }
export type ArtifactRelations = { center: ArtifactNode; nodes: ArtifactNode[];
  edges: { from: { type: ArtifactNode['type']; id: string };
    to: { type: ArtifactNode['type']; id: string }; relation: string }[];
  next_offset: number | null; truncated: boolean; scope: 'direct'; page_checkpoint: string; revision?: string }
export type ArtifactCatalog = { items: ArtifactNode[]; next_before_id: string | null; revision?: string }
export type ProjectGraphSnapshot = { project_id: number; scope: 'single-read-transaction'; revision: string;
  node_count: number; edge_count: number; nodes: ArtifactNode[]; edges: ArtifactRelations['edges'] }
export type ProjectTemplate = { id: number; project_id: number; name: string; schema_version: string;
  snapshot_sha256: string; skill_count: number; enabled_skill_count: number; has_taxonomy: boolean;
  has_conversion_settings: boolean; created_by_id: number; created_at: string }
export type ProjectTemplateDetail = ProjectTemplate & { snapshot: {
  schema_version: string; source_project_id: number;
  skills: { skill_id: string; enabled: boolean }[];
  conversion: { default_case_type: string; default_case_status: string; history_retention_days: number } | null;
  taxonomy: { source_number: number; definitions: Record<string, { id: string; name: string; description: string }[]> } | null;
} }

let csrfToken = ''

export async function rawApi(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (init.method && init.method !== 'GET') headers.set('x-csrf-token', csrfToken)
  const response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: 'same-origin' })
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    const message = typeof data.detail === 'string'
      ? data.detail
      : typeof data.detail?.message === 'string'
        ? data.detail.message
        : `请求失败 (${response.status})`
    const requestId = response.headers.get('x-request-id')
    throw Object.assign(new Error(requestId ? `${message}（请求编号 ${requestId}）` : message), { status: response.status })
  }
  return response
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  return (await rawApi(path, init)).json()
}

export type StreamEvent = { event: string; data: unknown; id?: number }

/** Read a same-origin Server-Sent Event response using the existing CSRF path. */
export async function streamApi(path: string, init: RequestInit, onEvent: (event: StreamEvent) => void) {
  const response = await rawApi(path, init)
  if (!response.body) throw new Error('流式响应不可用')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const consume = (block: string) => {
    let event = 'message'; let data = ''; let id: number | undefined
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data += line.slice(5).trim()
      else if (line.startsWith('id:')) {
        const value = line.slice(3).trim()
        if (/^\d+$/.test(value)) id = Number(value)
      }
    }
    if (!data) return
    let parsed: unknown = data
    try { parsed = JSON.parse(data) } catch { /* pass unstructured events through */ }
    onEvent({ event, data: parsed, id })
  }
  while (true) {
    const chunk = await reader.read()
    buffer += decoder.decode(chunk.value ?? new Uint8Array(), { stream: !chunk.done })
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) { consume(buffer.slice(0, boundary)); buffer = buffer.slice(boundary + 2); boundary = buffer.indexOf('\n\n') }
    if (chunk.done) { if (buffer.trim()) consume(buffer); break }
  }
}

/** Resume a task from its durable local SSE cursor after a transport closes. */
export async function followHarnessTask(path: string, signal: AbortSignal, onEvent: (event: StreamEvent) => void) {
  let cursor = 0
  let failures = 0
  while (!signal.aborted) {
    let terminal = false
    const receive = (event: StreamEvent) => {
      if (event.id !== undefined && Number.isSafeInteger(event.id)) cursor = Math.max(cursor, event.id)
      onEvent(event)
      if (['task.completed', 'task.failed', 'task.cancelled'].includes(event.event)) terminal = true
    }
    try {
      await streamApi(`${path}/events?after=${cursor}`, { signal }, receive)
      if (terminal || signal.aborted) return
      // A detail read performs a bounded catch-up when a stream closed before
      // the Host's terminal event was projected by the background worker.
      const task = await api<HarnessTask>(path, { signal })
      if (['completed', 'failed', 'cancelled'].includes(task.status)) {
        await streamApi(`${path}/events?after=${cursor}`, { signal }, receive)
        return
      }
      failures = 0
    } catch (cause) {
      if (signal.aborted || (cause as Error).name === 'AbortError') return
      const status = (cause as Error & { status?: number }).status
      if (status === 401 || status === 403 || status === 404) throw cause
      failures += 1
    }
    const delay = Math.min(5000, 500 * 2 ** Math.min(failures, 4))
    await new Promise<void>(resolve => {
      const timer = window.setTimeout(done, delay)
      function done() { window.clearTimeout(timer); signal.removeEventListener('abort', done); resolve() }
      signal.addEventListener('abort', done, { once: true })
      if (signal.aborted) done()
    })
  }
}

export async function refreshCsrf() {
  const response = await api<{ token: string }>('/auth/csrf')
  csrfToken = response.token
}

export async function login(username: string, password: string) {
  const result = await api<User & { csrf: string }>('/auth/login', {
    method: 'POST', body: JSON.stringify({ username, password }),
  })
  csrfToken = result.csrf
  return result
}

export async function upload(projectId: number, file: File) {
  const form = new FormData()
  form.append('file', file)
  return api<Preview>(`/projects/${projectId}/imports/preview`, { method: 'POST', body: form })
}
