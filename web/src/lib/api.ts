export type User = { id: number; username: string; role: 'admin' | 'reviewer' | 'reader' }
export type Project = { id: number; name: string; role: 'admin' | 'manager' | 'reviewer' | 'reader'; created_at?: string; archived?: boolean; archived_at?: string | null }
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
  requirements?: Requirement[]; ambiguities?: Ambiguity[]; risks?: string[]; report_markdown?: string
}; error: string; version: number; created_at: string; updated_at: string }
export type DraftPayload = { module: string[]; title: string; priority: string; precondition: string;
  steps: { action: string; expected: string }[]; requirement_ids: string[]; method: string; label: string; remark: string; smoke: string }
export type Draft = { id: string; analysis_id: string; payload: DraftPayload; status: string; version: number; case_id: number | null }
export type AiJob = { id: string; kind: string; status: string; error: string; attempts: number; analysis_id: string;
  request_id?: string; prompt_calls?: number; prompt_length?: number; prompt_available?: boolean }
export type AiPromptLog = { index: number; provider: string; text: string; length: number }

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
    throw new Error(requestId ? `${message}（请求编号 ${requestId}）` : message)
  }
  return response
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  return (await rawApi(path, init)).json()
}

export type StreamEvent = { event: string; data: unknown }

/** Read a same-origin Server-Sent Event response using the existing CSRF path. */
export async function streamApi(path: string, init: RequestInit, onEvent: (event: StreamEvent) => void) {
  const response = await rawApi(path, init)
  if (!response.body) throw new Error('流式响应不可用')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const consume = (block: string) => {
    let event = 'message'; let data = ''
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data += line.slice(5).trim()
    }
    if (!data) return
    try { onEvent({ event, data: JSON.parse(data) }) } catch { onEvent({ event, data }) }
  }
  while (true) {
    const chunk = await reader.read()
    buffer += decoder.decode(chunk.value ?? new Uint8Array(), { stream: !chunk.done })
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) { consume(buffer.slice(0, boundary)); buffer = buffer.slice(boundary + 2); boundary = buffer.indexOf('\n\n') }
    if (chunk.done) { if (buffer.trim()) consume(buffer); break }
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
