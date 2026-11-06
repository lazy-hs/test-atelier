import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { AlertCircle, ChevronLeft, ChevronRight, Download, Eye, ListFilter, Plus, RefreshCw, Save, Search, Send, Square, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { api, followHarnessTask, formatAiError, rawApi, type AiJob, type Analysis, type Draft, type DraftPayload, type HarnessTask, type HarnessTaskResult } from '@/lib/api'
import { PromptViewerDialog } from '@/components/PromptViewerDialog'
import { HarnessSessionDialog } from '@/components/HarnessSessionDialog'
import { downloadHarnessResult } from '@/lib/harnessResultExport'
import { validateHarnessResultEnvelope } from '@/lib/harnessResult'

function download(response: Response, blob: Blob) {
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const match = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  const filename = match ? decodeURIComponent(match[1]) : '用例草稿'
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a'); link.href = url; link.download = filename
  document.body.appendChild(link); link.click(); link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

type AmbiguityChoice = 'accept_suggestion' | 'provide_explanation' | 'skip' | 'mark_pending'
type HarnessConfirmation = { id: string; prompt: string; kind?: string; summary?: string; warnings?: string[]; applicable_platforms?: string; default_platforms?: string; case_id?: string; title?: string; candidates?: { case_id: string; title: string }[]; total_case_count?: number; requirement_id?: string; source_document_id?: string; source_text?: string; ambiguity_type?: string; interpretation?: string }

export function AuthoringPage({ projectId, readonly, initialAnalysisId = '', mode = 'harness', onOpenCases, onOpenReview, onOpenAuthoring }: { projectId: number; readonly: boolean; initialAnalysisId?: string; mode?: 'harness' | 'review'; onOpenCases: () => void; onOpenReview?: (id: string) => void; onOpenAuthoring?: (id: string) => void }) {
  const base = `/projects/${projectId}`
  const [analyses, setAnalyses] = useState<Analysis[]>([])
  const [harnessEnabled, setHarnessEnabled] = useState(false)
  const [harnessTask, setHarnessTask] = useState<HarnessTask | null>(null)
  const [harnessOutput, setHarnessOutput] = useState('')
  const [analysisId, setAnalysisId] = useState('')
  const [drafts, setDrafts] = useState<Draft[]>([])
  const [selected, setSelected] = useState('')
  const [draftQuery, setDraftQuery] = useState('')
  const [form, setForm] = useState<DraftPayload | null>(null)
  const [job, setJob] = useState<AiJob | null>(null)
  const [quality, setQuality] = useState<{ coverage: number; errors: string[]; warnings: string[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [promptOpen, setPromptOpen] = useState(false)
  const [sessionOpen, setSessionOpen] = useState(false)
  const [confirmation, setConfirmation] = useState<HarnessConfirmation | null>(null)
  const [confirmationComment, setConfirmationComment] = useState('')
  const [exceptionCaseId, setExceptionCaseId] = useState('')
  const [exceptionPlatforms, setExceptionPlatforms] = useState('')
  const harnessRequestKey = useRef('')
  const harnessTaskHistory = useRef<HarnessTask[]>([])
  const harnessTaskRef = useRef<HarnessTask | null>(null)
  const analysisRef = useRef<Analysis | null>(null)

  const refresh = useCallback(async () => {
    const [runs, harnessConfig, harnessTasks] = await Promise.all([
      api<Analysis[]>(`${base}/analyses`),
      api<{ enabled: boolean; required?: boolean }>(`${base}/harness/config`),
      mode === 'harness' ? api<HarnessTask[]>(`${base}/harness/tasks?task_type=test-cases&limit=20`) : Promise.resolve([]),
    ])
    const readyRuns = runs.filter(run => run.status === 'ready' && (mode === 'review' || run.provider === 'harness'))
    setAnalyses(readyRuns)
    setHarnessEnabled(harnessConfig.enabled)
    harnessTaskHistory.current = harnessTasks
    setHarnessTask(previous => previous?.analysis_run_id === analysisId
      ? previous : harnessTasks.find(task => task.analysis_run_id === analysisId) ?? null)
    if (!analysisId && readyRuns.length) {
      const next = readyRuns.find(run => run.id === initialAnalysisId) ?? readyRuns[0]
      setAnalysisId(next.id)
    }
  }, [base, analysisId, initialAnalysisId, mode])

  useEffect(() => {
    const recent = harnessTaskHistory.current.find(task => task.analysis_run_id === analysisId) ?? null
    let active = true
    setHarnessTask(recent)
    setHarnessOutput('')
    setConfirmation(null)
    if (mode === 'harness' && analysisId && !recent) {
      void api<HarnessTask[]>(`${base}/harness/tasks?task_type=test-cases&analysis_run_id=${encodeURIComponent(analysisId)}&limit=1`)
        .then(tasks => {
          if (active) setHarnessTask(previous => previous?.analysis_run_id === analysisId ? previous : tasks[0] ?? null)
        })
        .catch(cause => { if (active) setError((cause as Error).message) })
    }
    return () => { active = false }
  }, [analysisId, base, mode])

  const loadDrafts = useCallback(async () => {
    if (!analysisId) { setDrafts([]); return }
    const [result, tasks, summary] = await Promise.all([
      api<Draft[]>(`${base}/drafts?analysis_id=${analysisId}`), api<AiJob[]>(`${base}/ai/jobs?analysis_id=${analysisId}`),
      api<{ coverage: number; errors: string[]; warnings: string[] }>(`${base}/analyses/${analysisId}/quality`),
    ])
    setDrafts(result)
    // Select the first result as soon as generation finishes so the main pane
    // is useful immediately, while preserving the reader's current selection.
    setSelected(previous => previous && result.some(item => item.id === previous) ? previous : result[0]?.id ?? '')
    setJob(tasks.find(item => item.kind === 'cases') ?? null); setQuality(summary)
  }, [base, analysisId])

  // Reconnect to the durable local event projection after a page refresh.
  const harnessTaskId = harnessTask?.task_id
  const analysis = analyses.find(item => item.id === analysisId)
  useEffect(() => { analysisRef.current = analysis ?? null }, [analysis])
  useEffect(() => { harnessTaskRef.current = harnessTask }, [harnessTask])
  useEffect(() => {
    if (mode !== 'harness' || !harnessTaskId) return
    const controller = new AbortController()
    void followHarnessTask(`${base}/harness/tasks/${harnessTaskId}`, controller.signal, event => {
      if (controller.signal.aborted) return
      const data = event.data as { result?: unknown; kind?: string; status?: HarnessTask['status']; session_id?: string; sequence?: number; confirmation_id?: string; prompt?: string; error?: { code?: string; message?: string }; confirmation?: Omit<HarnessConfirmation, 'id' | 'prompt'> & { confirmation_id?: string; id?: string; prompt?: string; message?: string; question?: string } }
      if (event.event === 'task.transport_error') {
        setError(data.error?.message ?? 'Harness 连接中断，正在等待恢复')
        return
      }
      if (['task.progress', 'task.result', 'task.completed'].includes(event.event)) {
        setError(previous => previous === 'Harness 连接中断，正在等待恢复' ? '' : previous)
      }
      const nextStatus: HarnessTask['status'] | null = event.event === 'task.started' ? 'running'
        : event.event === 'task.confirmation_required' ? 'waiting_confirmation'
          : event.event === 'task.progress' && (data.kind === 'confirmation_submitted' || data.status === 'running') ? 'running'
          : event.event === 'task.completed' ? 'completed'
            : event.event === 'task.failed' ? 'failed'
              : event.event === 'task.cancelled' ? 'cancelled' : null
      const bindsSession = event.event === 'task.started' || event.event === 'task.result' || (event.event === 'task.progress' && data.kind === 'session_bound')
      const realSession = bindsSession && data.session_id && data.session_id !== `ta-${harnessTaskId}` ? data.session_id : null
      if (nextStatus || realSession) {
        setHarnessTask(previous => {
          if (!previous || previous.task_id !== harnessTaskId) return previous
          const session_ids = realSession ? Array.from(new Set([...(previous.session_ids ?? []).filter(id => id !== `ta-${harnessTaskId}`), realSession])) : previous.session_ids
          return { ...previous, ...(nextStatus ? { status: nextStatus } : {}), ...(realSession ? { session_id: realSession, session_ids } : {}) }
        })
      }
      if (event.event === 'task.confirmation_required') {
        const request: Omit<HarnessConfirmation, 'id' | 'prompt'> & { confirmation_id?: string; id?: string; prompt?: string; message?: string; question?: string } = data.confirmation ?? { confirmation_id: data.confirmation_id, prompt: data.prompt }
        const id = request.confirmation_id ?? request.id ?? (data.sequence ? `sequence-${data.sequence}` : '')
        if (id) {
          setConfirmation({ id, prompt: request.prompt ?? request.message ?? request.question ?? 'Harness 请求确认后继续执行',
            kind: request.kind, summary: request.summary, warnings: request.warnings, applicable_platforms: request.applicable_platforms,
            default_platforms: request.default_platforms, case_id: request.case_id, title: request.title,
            candidates: request.candidates, total_case_count: request.total_case_count,
            requirement_id: request.requirement_id, source_document_id: request.source_document_id,
            source_text: request.source_text, ambiguity_type: request.ambiguity_type, interpretation: request.interpretation })
          setConfirmationComment('')
          setExceptionCaseId('')
          setExceptionPlatforms('')
        }
      }
      if (event.event === 'task.progress' && data.kind === 'confirmation_submitted') setConfirmation(null)
      if (event.event === 'task.failed' || event.event === 'task.cancelled' || event.event === 'task.completed') {
        setConfirmation(null)
        if (event.event !== 'task.completed') setHarnessOutput('任务未完成，没有可用结果')
        if (event.event === 'task.failed' && data.error) {
          setError(data.error.message ?? data.error.code ?? 'DeepSeek Harness 任务失败')
        }
      }
      if (event.event === 'task.completed') {
        void api<HarnessTaskResult>(`${base}/harness/tasks/${harnessTaskId}/result`)
          .then(response => {
            if (controller.signal.aborted) return
            const taskContext = harnessTaskRef.current
            const analysisContext = analysisRef.current
            const result = validateHarnessResultEnvelope(response, {
              taskId: harnessTaskId,
              projectId,
              expectedSchema: 'test-cases.v1',
              expectedAnalysisRunId: taskContext?.analysis_run_id,
              expectedSourceDocumentIds: analysisContext?.source_ids,
              authorizedSessionIds: [taskContext?.session_id ?? '', ...(taskContext?.session_ids ?? [])].filter(Boolean),
            })
            setHarnessOutput(JSON.stringify(result, null, 2))
          }).catch(cause => { if (!controller.signal.aborted) setError((cause as Error).message) })
      }
    }).catch(cause => { if (!controller.signal.aborted) setError((cause as Error).message) })
    return () => controller.abort()
  }, [base, harnessTaskId, mode, projectId])
  useEffect(() => { void refresh().catch(cause => setError(cause.message)) }, [refresh])
  useEffect(() => { setSelected(''); setForm(null); if (mode === 'review') void loadDrafts().catch(cause => setError(cause.message)) }, [loadDrafts, mode])
  useEffect(() => {
    if (mode !== 'review' || (job?.status !== 'queued' && job?.status !== 'running')) return
    const interval = window.setInterval(() => { void loadDrafts().catch(cause => setError(cause.message)) }, 2500)
    return () => window.clearInterval(interval)
  }, [job?.status, loadDrafts, mode])
  useEffect(() => {
    const draft = drafts.find(item => item.id === selected)
    if (draft) setForm(structuredClone(draft.payload))
  }, [drafts, selected])
  const current = drafts.find(item => item.id === selected)
  const filteredDrafts = useMemo(() => {
    const query = draftQuery.trim().toLocaleLowerCase()
    if (!query) return drafts
    return drafts.filter(item => [item.payload.title, item.payload.module.join(' / '), item.payload.requirement_ids.join(' '), item.payload.method, item.payload.priority]
      .some(value => value.toLocaleLowerCase().includes(query)))
  }, [draftQuery, drafts])
  const currentIndex = filteredDrafts.findIndex(item => item.id === selected)
  const publishedCount = drafts.filter(item => item.status === 'published').length
  const unresolved = analysis?.result.ambiguities?.some(item => item.critical && !item.decision)
  async function generate() {
    if (!analysisId) return
    if (!harnessEnabled) {
      setError('DeepSeek Harness 已被设为唯一执行引擎，但当前 Host 尚未配置')
      return
    }
    setBusy(true); setError('')
    try {
      if (harnessTask && ['completed', 'failed', 'cancelled'].includes(harnessTask.status)) {
        harnessRequestKey.current = ''
      }
      if (!harnessRequestKey.current.startsWith(`${analysisId}:`)) {
        harnessRequestKey.current = `${analysisId}:${crypto.randomUUID()}`
      }
      const task = await api<HarnessTask>(`${base}/harness/tasks`, {
        method: 'POST',
        body: JSON.stringify({
          task_type: 'test-cases',
          task_version: 'v1',
          analysis_run_id: analysisId,
          skill_id: 'test-cases',
          input: { language: 'zh-CN', output_mode: 'structured' },
          idempotency_key: harnessRequestKey.current,
        }),
      })
      harnessTaskHistory.current = [task, ...harnessTaskHistory.current.filter(item => item.task_id !== task.task_id)]
      setHarnessTask(task)
      setHarnessOutput('')
      setNotice('DeepSeek Harness 用例任务已启动，正在接收流式结果')
    } catch (cause) {
      if (cause instanceof Error && (cause.message.startsWith('queue_full') || cause.message.startsWith('input_timeout'))) {
        harnessRequestKey.current = ''
        setError(cause.message.startsWith('input_timeout') ? 'Harness 分段上传超时，请重新发起任务' : 'Harness 当前并发容量已满，请稍后重试')
      } else setError((cause as Error).message)
    } finally { setBusy(false) }
  }
  async function retry() {
    if (!job) return
    setBusy(true); setError('')
    try { await api(`${base}/ai/jobs/${job.id}/retry`, { method: 'POST' }); await loadDrafts(); setNotice('任务已重新排队') }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function cancelHarnessTask() {
    if (!harnessTask || !['queued', 'starting', 'running', 'waiting_confirmation'].includes(harnessTask.status)) return
    setBusy(true); setError('')
    try {
      const task = await api<HarnessTask>(`${base}/harness/tasks/${harnessTask.task_id}/cancel`, { method: 'POST' })
      setHarnessTask(task); setNotice('DeepSeek Harness 任务已取消')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function retryHarnessTask() {
    if (!harnessTask || !['failed', 'cancelled'].includes(harnessTask.status)) return
    setBusy(true); setError('')
    try {
      const task = await api<HarnessTask>(`${base}/harness/tasks/${harnessTask.task_id}/retry`, { method: 'POST' })
      harnessTaskHistory.current = [task, ...harnessTaskHistory.current.filter(item => item.task_id !== task.task_id)]
      setHarnessTask(task); setHarnessOutput(''); setConfirmation(null)
      setNotice('DeepSeek Harness 用例任务已重新启动')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function confirmHarnessTask(decision: 'approve' | 'reject', ambiguityChoice?: AmbiguityChoice) {
    if (!harnessTask || !confirmation) return
    if (ambiguityChoice === 'provide_explanation' && !confirmationComment.trim()) { setError('请先填写你的解释'); return }
    if (decision === 'approve' && confirmation.kind === 'case_tag_exception'
      && confirmationComment.trim() !== confirmation.applicable_platforms) {
      setError('请准确输入申请的适用端后再批准该单条例外')
      return
    }
    if (confirmation.kind === 'case_tag_selection' && decision === 'approve') {
      const tag = exceptionPlatforms.trim()
      const parts = tag.split(', ')
      if (Boolean(exceptionCaseId.trim()) !== Boolean(tag)) { setError('请同时填写用例 ID 和例外适用端，或均留空直接继续'); return }
      if (tag && (parts.some(part => !['Pc', 'Web', 'Android', 'IOS'].includes(part))
        || new Set(parts).size !== parts.length || parts.join(', ') !== tag || tag === confirmation.default_platforms)) {
        setError('例外适用端必须与本次适用端不同，格式为 Pc、Web、Android、IOS 或其英文逗号加空格组合')
        return
      }
    }
    if (decision === 'approve' && confirmation.kind === 'case_skill_checkpoint' && !confirmation.applicable_platforms) {
      const choice = confirmationComment.trim()
      const selected = /^[1-6]$/.test(choice) ? ['Pc', 'Web', 'Android', 'IOS', 'Android, IOS', 'Pc, Web, Android, IOS'][Number(choice) - 1] : choice
      const parts = selected.split(', ')
      if (!selected || parts.some(part => !['Pc', 'Web', 'Android', 'IOS'].includes(part)) || new Set(parts).size !== parts.length || parts.join(', ') !== selected) {
        setError('适用端请选择 1-6，或填写 Pc、Web、Android、IOS 及其英文逗号加空格组合')
        return
      }
    }
    const submittedId = confirmation.id
    setBusy(true); setError('')
    try {
      const task = await api<HarnessTask>(`${base}/harness/tasks/${harnessTask.task_id}/confirm`, {
        method: 'POST', body: JSON.stringify({ confirmation_id: confirmation.id, decision,
          ...(ambiguityChoice ? { ambiguity_choice: ambiguityChoice } : {}),
          ...(['case_skill_checkpoint', 'ambiguity_checkpoint', 'case_tag_exception', 'case_tag_selection'].includes(confirmation.kind ?? '') ? {
            comment: confirmation.kind === 'case_tag_selection' && exceptionCaseId.trim()
              ? JSON.stringify({ case_id: exceptionCaseId.trim(), applicable_platforms: exceptionPlatforms.trim() })
              : confirmation.kind === 'case_tag_selection' ? '' : confirmationComment.trim(),
          } : {}) }),
      })
      setHarnessTask(previous => previous?.task_id === task.task_id && previous.sequence > task.sequence ? previous : task)
      setConfirmation(previous => previous?.id === submittedId ? null : previous)
      setNotice(ambiguityChoice ? '已记录这条歧义的决定' : decision === 'approve' ? '已允许 Harness 继续' : '已拒绝 Harness 请求')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function save() {
    if (!form || !current) return
    setBusy(true); setError('')
    try {
      const updated = await api<Draft>(`${base}/drafts/${current.id}`, { method: 'PATCH', body: JSON.stringify({ version: current.version, payload: form }) })
      setDrafts(previous => previous.map(item => item.id === updated.id ? updated : item)); setNotice('草稿已保存'); await loadDrafts()
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function publish() {
    if (!current) return
    if (JSON.stringify(form) !== JSON.stringify(current.payload)) { setError('请先保存草稿修改，再发布'); return }
    if (!window.confirm('发布后用例会进入项目用例库，确定继续吗？')) return
    setBusy(true); setError('')
    try { await api(`${base}/drafts/${current.id}/publish`, { method: 'POST' }); await loadDrafts(); setNotice('已发布到用例库') }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function exportFile(format: 'xlsx' | 'xmind') {
    if (!analysisId) return
    setBusy(true); setError('')
    try {
      const response = await rawApi(`${base}/drafts/export?analysis_id=${analysisId}&format=${format}`)
      download(response, await response.blob()); setNotice('文件已生成')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function exportHarnessXmind() {
    if (!harnessTask || harnessTask.status !== 'completed') return
    setBusy(true); setError('')
    try {
      const response = await rawApi(`${base}/harness/tasks/${harnessTask.task_id}/xmind`)
      download(response, await response.blob()); setNotice('Harness 用例 XMind 已生成')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function exportHarnessResult() {
    if (!harnessTask || harnessTask.status !== 'completed') return
    setBusy(true); setError('')
    try {
      await downloadHarnessResult(projectId, harnessTask.task_id, 'test-cases.v1')
      setNotice('Harness 原始结果已下载')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  function change(patch: Partial<DraftPayload>) { setForm(previous => previous ? { ...previous, ...patch } : previous) }
  return <div className={`ai-workspace${mode === 'harness' ? ' harness-authoring-workspace' : ''}`}>
    {error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
    {mode === 'harness' && !harnessEnabled && <div className="ai-alert warning" role="status"><AlertCircle size={16} />用例编写仅由 DeepSeek Harness 执行，请先配置 Host 和作用域密钥</div>}
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    <div className="authoring-toolbar"><label htmlFor="analysis-select">分析报告</label><select id="analysis-select" value={analysisId} onChange={event => { setAnalysisId(event.target.value); setDraftQuery('') }}>
      {analyses.length ? analyses.map(item => <option value={item.id} key={item.id}>{item.title || new Date(item.created_at).toLocaleString('zh-CN')} · {item.result.requirements?.length ?? 0} 条需求</option>) : <option value="">暂无已完成的分析报告</option>}
    </select>{mode === 'harness' ? <>
      <div className={`harness-runtime-badge${!harnessEnabled ? ' is-unavailable' : ''}`}><span className="harness-runtime-dot" />{harnessEnabled ? 'DeepSeek Harness 执行' : 'DeepSeek Harness 未配置'}</div>
      <Button onClick={() => void generate()} disabled={readonly || busy || !harnessEnabled || !analysisId || Boolean(unresolved) || ['queued', 'starting', 'running', 'waiting_confirmation'].includes(harnessTask?.status ?? '')}><Plus data-icon="inline-start" />运行 Harness</Button>
      {analysisId && onOpenReview && <Button variant="outline" onClick={() => onOpenReview(analysisId)}>草稿审核</Button>}
    </> : <>
      {analysisId && onOpenAuthoring && <Button variant="outline" onClick={() => onOpenAuthoring(analysisId)}>查看 Harness 结果</Button>}
      {job && <Button variant="outline" onClick={() => setPromptOpen(true)} disabled={busy}><Eye data-icon="inline-start" />查看历史任务提示词</Button>}
      {drafts.length > 0 && <><Button variant="outline" onClick={() => void exportFile('xlsx')} disabled={busy}><Download data-icon="inline-start" />Excel</Button><Button variant="outline" onClick={() => void exportFile('xmind')} disabled={busy}><Download data-icon="inline-start" />XMind</Button></>}
    </>}</div>
    {mode === 'harness' && unresolved && <div className="ai-alert warning"><AlertCircle size={16} />分析报告仍有关键歧义待确认</div>}
    {mode === 'review' && drafts.length > 0 && quality && <div className={`ai-alert ${quality.errors.length ? 'error' : quality.warnings.length ? 'warning' : ''}`} role="status">需求覆盖 {quality.coverage}% · {drafts.length} 条草稿{quality.errors.map(item => ` · ${item}`)}{quality.warnings.map(item => ` · ${item}`)}</div>}
    {mode === 'review' && job?.status === 'failed' && <div className="ai-alert error"><AlertCircle size={16} />{formatAiError(job.error)}{!readonly && <Button variant="outline" size="sm" onClick={() => void retry()} disabled={busy}><RefreshCw data-icon="inline-start" />重试</Button>}</div>}
    {mode === 'review' && (job?.status === 'queued' || job?.status === 'running') && <div className="ai-processing" role="status">正在生成用例草稿</div>}
    {mode === 'harness' && !harnessTask && <motion.div className="ai-empty main" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}><h2>尚无 Harness 用例任务</h2><p>选择已确认的分析报告，运行后在这里查看原始结果</p></motion.div>}
    {mode === 'harness' && harnessTask && <motion.div className="harness-stream-panel" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}><div className="ai-section-heading"><div><h2>DeepSeek Harness 输出</h2><p>原始结构化结果只读展示 · 任务状态：{harnessTask.status}</p></div><div className="ai-heading-actions"><Badge variant="secondary">{harnessTask.result_schema}</Badge><AnimatePresence mode="popLayout">{harnessTask.status === 'completed' && <motion.div key="harness-xmind" initial={{ opacity: 0, scale: .96 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: .96 }} whileHover={{ scale: 1.02 }} whileTap={{ scale: .97 }} transition={{ duration: .18 }}><Button variant="outline" size="sm" onClick={() => void exportHarnessXmind()} disabled={busy}><Download data-icon="inline-start" />Harness 结果导出 XMind</Button></motion.div>}</AnimatePresence>{harnessTask.status === 'completed' && <motion.div key="harness-json" initial={{ opacity: 0, scale: .96 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: .96 }} whileHover={{ scale: 1.02 }} whileTap={{ scale: .97 }} transition={{ duration: .18 }}><Button variant="outline" size="sm" onClick={() => void exportHarnessResult()} disabled={busy}><Download data-icon="inline-start" />下载原始 JSON</Button></motion.div>}{harnessTask.session_id && <Button variant="ghost" size="sm" onClick={() => setSessionOpen(true)}><Eye data-icon="inline-start" />查看 Session</Button>}{['failed', 'cancelled'].includes(harnessTask.status) && !readonly && <Button variant="outline" size="sm" onClick={() => void retryHarnessTask()} disabled={busy}><RefreshCw data-icon="inline-start" />重试</Button>}{['queued', 'starting', 'running', 'waiting_confirmation'].includes(harnessTask.status) && <Button variant="outline" size="sm" onClick={() => void cancelHarnessTask()} disabled={busy}><Square data-icon="inline-start" />停止</Button>}</div></div>{confirmation && <div className="harness-confirmation" role="dialog" aria-label={confirmation.kind === 'ambiguity_checkpoint' ? '用例关键歧义逐项决定' : confirmation.kind === 'case_tag_exception' ? '用例适用端例外确认' : confirmation.kind === 'case_tag_selection' ? '指定单条用例适用端' : 'Harness 用例确认'}><strong>{confirmation.kind === 'case_skill_checkpoint' ? 'Phase 2.5 用例解析确认' : confirmation.kind === 'ambiguity_checkpoint' ? 'Phase 2.6 用例关键歧义' : confirmation.kind === 'case_tag_exception' ? '单条例外待确认' : confirmation.kind === 'case_tag_selection' ? '用例适用端逐条指定' : '需要你的确认'}</strong><p>{confirmation.prompt}</p>{confirmation.kind === 'case_skill_checkpoint' && <><pre className="harness-checkpoint-summary">{confirmation.summary}</pre>{Boolean(confirmation.warnings?.length) && <ul>{confirmation.warnings?.map((warning, index) => <li key={`${index}-${warning}`}>{warning}</li>)}</ul>}<p>适用端：{confirmation.applicable_platforms || '待选择'}</p><Textarea aria-label="用例适用端或解析澄清" placeholder="适用端可填 1-6 或 Pc、Web、Android、IOS 的组合" value={confirmationComment} maxLength={2000} onChange={event => setConfirmationComment(event.target.value)} disabled={readonly || busy} /></>}{confirmation.kind === 'case_tag_selection' && <motion.div className="harness-case-selection" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }}><p>本次统一适用端：{confirmation.default_platforms} · 共 {confirmation.total_case_count} 条用例</p><label>单条用例 ID<Input aria-label="指定例外用例 ID" list="harness-case-candidates" placeholder="留空则不指定例外" value={exceptionCaseId} maxLength={80} onChange={event => setExceptionCaseId(event.target.value)} disabled={readonly || busy} /></label><datalist id="harness-case-candidates">{confirmation.candidates?.map(item => <option key={item.case_id} value={item.case_id}>{item.title}</option>)}</datalist><label>该条适用端<Input aria-label="指定例外适用端" placeholder="例如 Pc" value={exceptionPlatforms} maxLength={60} onChange={event => setExceptionPlatforms(event.target.value)} disabled={readonly || busy} /></label><small>只展示前 {confirmation.candidates?.length ?? 0} 条候选；其余用例可直接填写 ID。模型会在同一 Session 重发原始结果，之后仍需逐条批准。</small></motion.div>}{confirmation.kind === 'case_tag_exception' && <motion.dl className="harness-ambiguity-fields" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: .2 }}><dt>用例 ID</dt><dd>{confirmation.case_id}</dd><dt>用例标题</dt><dd>{confirmation.title}</dd><dt>本次适用端</dt><dd>{confirmation.default_platforms}</dd><dt>例外适用端</dt><dd>{confirmation.applicable_platforms}</dd></motion.dl>}{confirmation.kind === 'case_tag_exception' && <Textarea aria-label="确认单条例外适用端" placeholder="准确输入申请的适用端以批准" value={confirmationComment} maxLength={2000} onChange={event => setConfirmationComment(event.target.value)} disabled={readonly || busy} />}{confirmation.kind === 'ambiguity_checkpoint' && <><dl className="harness-ambiguity-fields"><dt>需求 ID</dt><dd>{confirmation.requirement_id}</dd><dt>来源 ID</dt><dd>{confirmation.source_document_id}</dd><dt>原文或已确认需求</dt><dd>{confirmation.source_text}</dd><dt>类型</dt><dd>{confirmation.ambiguity_type}</dd><dt>建议理解</dt><dd>{confirmation.interpretation}</dd></dl><Textarea aria-label="用例歧义解释" placeholder="选择提供解释时填写" value={confirmationComment} maxLength={2000} onChange={event => setConfirmationComment(event.target.value)} disabled={readonly || busy} /><p>跳过或待确认会阻止生成 P0/P1 用例，不会自动视为已解决</p></>}<div>{confirmation.kind === 'ambiguity_checkpoint' ? <><Button size="sm" onClick={() => void confirmHarnessTask('approve', 'accept_suggestion')} disabled={readonly || busy}>接受建议</Button><Button size="sm" variant="outline" onClick={() => void confirmHarnessTask('approve', 'provide_explanation')} disabled={readonly || busy}>提供解释</Button><Button size="sm" variant="outline" onClick={() => void confirmHarnessTask('approve', 'skip')} disabled={readonly || busy}>跳过</Button><Button size="sm" variant="outline" onClick={() => void confirmHarnessTask('approve', 'mark_pending')} disabled={readonly || busy}>标记待确认</Button></> : confirmation.kind === 'case_tag_selection' ? <Button size="sm" onClick={() => void confirmHarnessTask('approve')} disabled={readonly || busy}>{exceptionCaseId.trim() ? '提交单条例外' : '无额外例外 继续'}</Button> : confirmation.kind === 'case_tag_exception' ? <><Button size="sm" onClick={() => void confirmHarnessTask('approve')} disabled={readonly || busy}>批准单条例外</Button><Button size="sm" variant="outline" onClick={() => void confirmHarnessTask('reject')} disabled={readonly || busy}>拒绝例外</Button></> : <><Button size="sm" onClick={() => void confirmHarnessTask('approve')} disabled={readonly || busy}>{confirmation.kind === 'case_skill_checkpoint' ? '确认用例解析后继续' : '允许继续'}</Button><Button size="sm" variant="outline" onClick={() => void confirmHarnessTask('reject')} disabled={readonly || busy}>拒绝</Button></>}</div></div>}<pre className="harness-stream-output" data-testid="harness-cases-result">{harnessOutput || '任务完成后显示 Harness 原始结果'}</pre></motion.div>}
    {mode === 'review' && <div className="ai-columns authoring-columns"><aside className="ai-rail"><div className="ai-rail-heading"><div><h2>Test Atelier 草稿审核</h2><p className="draft-list-summary">{drafts.length} 条 · 已发布 {publishedCount} 条 · 非 Harness 原始结果</p></div><Badge variant="secondary">{drafts.length}</Badge></div>
      {drafts.length > 0 && <label className="draft-search"><Search size={14} /><input value={draftQuery} onChange={event => setDraftQuery(event.target.value)} placeholder="搜索标题、模块或需求 ID" aria-label="搜索用例草稿" /></label>}
      {filteredDrafts.length ? filteredDrafts.map((item, index) => <button className={`draft-row ${item.id === selected ? 'is-active' : ''}`} key={item.id} onClick={() => setSelected(item.id)}><span className="draft-row-index">{String(index + 1).padStart(2, '0')}</span><span className="draft-row-copy"><strong>{item.payload.title}</strong><span>{item.payload.module.join(' / ') || '未分类'} · {item.payload.priority} · {item.status === 'published' ? '已发布' : '草稿'}</span></span></button>) : drafts.length ? <div className="draft-filter-empty"><ListFilter size={18} /><span>没有匹配的用例</span></div> : <div className="ai-empty">从已确认的分析报告生成草稿</div>}
    </aside><section className="ai-main"><AnimatePresence mode="wait">{current && form ? <motion.div key={current.id} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} className="draft-editor">
      <div className="ai-section-heading"><div><div className="draft-title-line"><h2>编写用例</h2><Badge variant="secondary">{currentIndex >= 0 ? `${currentIndex + 1} / ${filteredDrafts.length}` : '已选'}</Badge></div><p>来源需求 {form.requirement_ids.join(', ')}</p></div><div className="draft-navigation"><Button variant="ghost" size="icon-sm" aria-label="上一个用例" onClick={() => currentIndex > 0 && setSelected(filteredDrafts[currentIndex - 1].id)} disabled={currentIndex <= 0}><ChevronLeft /></Button><Button variant="ghost" size="icon-sm" aria-label="下一个用例" onClick={() => currentIndex >= 0 && currentIndex < filteredDrafts.length - 1 && setSelected(filteredDrafts[currentIndex + 1].id)} disabled={currentIndex < 0 || currentIndex >= filteredDrafts.length - 1}><ChevronRight /></Button>{harnessTask?.session_id && <Button variant="ghost" size="sm" onClick={() => setSessionOpen(true)}><Eye data-icon="inline-start" />查看 Session</Button>}{current.status === 'published' && <Button variant="outline" size="sm" onClick={onOpenCases}>查看用例库</Button>}</div></div>
      <div className="draft-fields"><label>标题<Input value={form.title} onChange={event => change({ title: event.target.value })} disabled={readonly || current.status === 'published'} /></label><label>模块路径<Input value={form.module.join(' / ')} onChange={event => change({ module: event.target.value.split('/').map(text => text.trim()).filter(Boolean) })} disabled={readonly || current.status === 'published'} /></label>
        <label>需求 ID<Input value={form.requirement_ids.join(', ')} onChange={event => change({ requirement_ids: event.target.value.split(',').map(text => text.trim()).filter(Boolean) })} disabled={readonly || current.status === 'published'} /></label>
        <label>优先级<select value={form.priority} onChange={event => change({ priority: event.target.value })} disabled={readonly || current.status === 'published'}>{['P0', 'P1', 'P2', 'P3'].map(value => <option key={value}>{value}</option>)}</select></label>
        <label>设计方法<select value={form.method} onChange={event => change({ method: event.target.value })} disabled={readonly || current.status === 'published'}>{['EP', 'BVA', 'ST', 'EG'].map(value => <option key={value}>{value}</option>)}</select></label>
        <label>冒烟<select value={form.smoke} onChange={event => change({ smoke: event.target.value })} disabled={readonly || current.status === 'published'}><option value="">待判定</option><option value="是">是</option><option value="否">否</option></select></label>
        <label className="full">前置条件<Textarea value={form.precondition} onChange={event => change({ precondition: event.target.value })} disabled={readonly || current.status === 'published'} /></label></div>
      <div className="ai-section-heading"><h3>步骤与预期</h3><Button variant="outline" size="sm" onClick={() => change({ steps: [...form.steps, { action: '', expected: '' }] })} disabled={readonly || current.status === 'published'}><Plus data-icon="inline-start" />步骤</Button></div>
      {form.steps.map((step, index) => <div className="draft-step" key={index}><span>{index + 1}</span><Input aria-label={`步骤 ${index + 1}`} placeholder="操作" value={step.action} onChange={event => change({ steps: form.steps.map((item, i) => i === index ? { ...item, action: event.target.value } : item) })} disabled={readonly || current.status === 'published'} /><Input aria-label={`预期 ${index + 1}`} placeholder="预期结果" value={step.expected} onChange={event => change({ steps: form.steps.map((item, i) => i === index ? { ...item, expected: event.target.value } : item) })} disabled={readonly || current.status === 'published'} /><Button variant="ghost" size="icon-sm" aria-label={`删除步骤 ${index + 1}`} onClick={() => change({ steps: form.steps.filter((_, i) => i !== index) })} disabled={readonly || current.status === 'published' || form.steps.length === 1}><Trash2 /></Button></div>)}
      <div className="draft-fields draft-extra"><label>适用端<Input value={form.label} onChange={event => change({ label: event.target.value })} disabled={readonly || current.status === 'published'} /></label><label>备注<Input value={form.remark} onChange={event => change({ remark: event.target.value })} disabled={readonly || current.status === 'published'} /></label></div>
      {current.status === 'draft' && !readonly && <div className="draft-actions"><Button variant="outline" onClick={() => void save()} disabled={busy}><Save data-icon="inline-start" />保存草稿</Button><Button onClick={() => void publish()} disabled={busy}><Send data-icon="inline-start" />发布到用例库</Button></div>}
    </motion.div> : <motion.div key="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="ai-empty main"><h2>选择一条用例草稿</h2></motion.div>}</AnimatePresence></section></div>}
    <PromptViewerDialog projectId={projectId} jobId={job?.id ?? null} open={promptOpen} onOpenChange={setPromptOpen} />
    <HarnessSessionDialog projectId={projectId} taskId={harnessTask?.task_id ?? null} sessionId={harnessTask?.session_id ?? null} sessionIds={harnessTask?.session_ids ?? []} open={sessionOpen} onOpenChange={setSessionOpen} />
  </div>
}
