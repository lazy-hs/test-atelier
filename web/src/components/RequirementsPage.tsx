import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { AlertCircle, ArrowRight, Check, Copy, Download, Eye, FileText, Loader2, PencilLine, Plus, RefreshCw, Save, Square, Trash2, Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { api, followHarnessTask, formatAiError, rawApi, type AiConfig, type AiJob, type Analysis, type HarnessTask, type HarnessTaskResult, type SourceDocument } from '@/lib/api'
import { PromptViewerDialog } from '@/components/PromptViewerDialog'
import { HarnessSessionDialog } from '@/components/HarnessSessionDialog'
import { downloadHarnessResult } from '@/lib/harnessResultExport'
import { validateHarnessResultEnvelope } from '@/lib/harnessResult'

type UploadItem = { id: string; name: string; size: number; status: 'queued' | 'uploading' | 'done' | 'rejected' | 'failed' }
type AmbiguityChoice = 'accept_suggestion' | 'provide_explanation' | 'skip' | 'mark_pending'
type HarnessConfirmationRequest = { id: string; prompt: string; kind?: string; summary?: string; warnings?: string[]; ambiguity_id?: string; requirement_id?: string; source_document_id?: string; source_text?: string; ambiguity_type?: string; interpretation?: string }

const safeMarkdownComponents: Components = {
  img: ({ alt }) => <span className="report-remote-asset">图片未加载：{alt || '无描述'}</span>,
  a: ({ children, href, title }) => <a href={href} title={title} referrerPolicy="no-referrer" rel="noopener noreferrer">{children}</a>,
}

export function RequirementsPage({ projectId, readonly, mode = 'harness', onOpenAuthoring }: { projectId: number; readonly: boolean; mode?: 'harness' | 'archive'; onOpenAuthoring: (analysisId: string) => void }) {
  const [documents, setDocuments] = useState<SourceDocument[]>([])
  const [analyses, setAnalyses] = useState<Analysis[]>([])
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [documentId, setDocumentId] = useState('')
  const [documentText, setDocumentText] = useState('')
  const [analysisId, setAnalysisId] = useState('')
  const [loadedAnalysis, setAnalysis] = useState<Analysis | null>(null)
  const [jobs, setJobs] = useState<AiJob[]>([])
  const [model, setModel] = useState<Pick<AiConfig, 'remote_enabled' | 'codex_enabled' | 'claude_enabled' | 'max_document_bytes'> | null>(null)
  const [harnessEnabled, setHarnessEnabled] = useState(false)
  const [harnessTask, setHarnessTask] = useState<HarnessTask | null>(null)
  const [harnessOutput, setHarnessOutput] = useState('')
  const [harnessProgress, setHarnessProgress] = useState('')
  const [reportText, setReportText] = useState('')
  const [decisions, setDecisions] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [promptOpen, setPromptOpen] = useState(false)
  const [sessionOpen, setSessionOpen] = useState(false)
  const [confirmation, setConfirmation] = useState<HarnessConfirmationRequest | null>(null)
  const [confirmationComment, setConfirmationComment] = useState('')
  const [clarificationsCopied, setClarificationsCopied] = useState(false)
  const [reportMode, setReportMode] = useState<'preview' | 'edit'>('preview')
  const [uploadItems, setUploadItems] = useState<UploadItem[]>([])
  const analysis = loadedAnalysis?.id === analysisId ? loadedAnalysis : null
  const reportRevision = useRef('')
  const harnessRequestKey = useRef('')
  const harnessTaskHistory = useRef<HarnessTask[]>([])
  const harnessTaskRef = useRef<HarnessTask | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const base = `/projects/${projectId}`

  const clarificationItems = analysis?.result.ambiguities ?? []
  const clarificationText = clarificationItems.map((item, index) => [
    `${index + 1}、${item.id}${item.critical ? '（关键）' : ''} ${item.description}`,
  ].join('\n')).join('\n\n')

  const copyClarifications = async () => {
    if (!clarificationText) return
    try {
      await navigator.clipboard.writeText(clarificationText)
    } catch {
      const fallback = document.createElement('textarea')
      fallback.value = clarificationText
      fallback.setAttribute('readonly', '')
      fallback.style.position = 'fixed'
      fallback.style.opacity = '0'
      document.body.appendChild(fallback)
      fallback.select()
      document.execCommand('copy')
      fallback.remove()
    }
    setClarificationsCopied(true)
    window.setTimeout(() => setClarificationsCopied(false), 1800)
  }

  const refresh = useCallback(async () => {
    const [docs, runs, config, harnessConfig, harnessTasks] = await Promise.all([
      api<SourceDocument[]>(`${base}/documents`), api<Analysis[]>(`${base}/analyses`),
      api<Pick<AiConfig, 'remote_enabled' | 'codex_enabled' | 'claude_enabled' | 'max_document_bytes'>>(`${base}/ai/config`),
      api<{ enabled: boolean }>(`${base}/harness/config`),
      mode === 'harness' ? api<HarnessTask[]>(`${base}/harness/tasks?task_type=requirement-analysis&limit=20`) : Promise.resolve([]),
    ])
    const visible = runs.filter(run => (run.provider === 'harness') === (mode === 'harness'))
    setDocuments(docs); setAnalyses(visible); setModel(config); setHarnessEnabled(harnessConfig.enabled)
    harnessTaskHistory.current = harnessTasks
    setAnalysisId(previous => previous && visible.some(run => run.id === previous) ? previous : visible[0]?.id ?? '')
  }, [base, mode])

  useEffect(() => { void refresh().catch(cause => setError(cause.message)) }, [refresh])
  useEffect(() => {
    const recent = harnessTaskHistory.current.find(task => task.analysis_run_id === analysisId) ?? null
    let active = true
    setHarnessTask(recent)
    setHarnessOutput('')
    setHarnessProgress('')
    setConfirmation(null)
    if (mode === 'harness' && analysisId && !recent) {
      void api<HarnessTask[]>(`${base}/harness/tasks?task_type=requirement-analysis&analysis_run_id=${encodeURIComponent(analysisId)}&limit=1`)
        .then(tasks => {
          if (active) setHarnessTask(previous => previous?.analysis_run_id === analysisId ? previous : tasks[0] ?? null)
        })
        .catch(cause => { if (active) setError((cause as Error).message) })
    }
    return () => { active = false }
  }, [analysisId, base, mode])
  useEffect(() => {
    if (!analysisId) return
    let active = true
    const load = async () => {
      const [run, currentJobs] = await Promise.all([
        api<Analysis>(`${base}/analyses/${analysisId}`), api<AiJob[]>(`${base}/ai/jobs?analysis_id=${analysisId}`),
      ])
      if (!active) return
      setAnalysis(run); setJobs(currentJobs)
      setAnalyses(previous => previous.map(item => item.id === run.id ? run : item))
      // Polling refreshes job status, but must not overwrite unsaved editor
      // changes until the server reports a newer analysis revision.
      const revision = `${run.id}:${run.version}`
      if (reportRevision.current !== revision) {
        reportRevision.current = revision
        setReportText(run.result.report_markdown ?? '')
        setDecisions(Object.fromEntries((run.result.ambiguities ?? []).map(item => [item.id, item.decision ?? ''])))
      }
      if (run.status === 'ready' || run.status === 'failed' || run.status === 'deleting') window.clearInterval(interval)
    }
    void load().catch(cause => setError(cause.message))
    const interval = window.setInterval(() => {
      if (active) void load().catch(cause => setError(cause.message))
    }, 3000)
    return () => { active = false; window.clearInterval(interval) }
  }, [analysisId, base])

  // A refreshed page has no in-memory SSE cursor. Replaying the local
  // projection restores the latest Harness task without starting a duplicate.
  const harnessTaskId = harnessTask?.task_id
  useEffect(() => { harnessTaskRef.current = harnessTask }, [harnessTask])
  useEffect(() => {
    if (mode !== 'harness' || !harnessTaskId) return
    const controller = new AbortController()
    void followHarnessTask(`${base}/harness/tasks/${harnessTaskId}`, controller.signal, event => {
      if (controller.signal.aborted) return
      const data = event.data as { result?: unknown; kind?: string; chunk_index?: number; chunk_count?: number; round?: number; batch_index?: number; batch_count?: number; input_count?: number; final?: boolean; execution_mode?: string; session_id?: string; sequence?: number; confirmation_id?: string; prompt?: string; error?: { code?: string; message?: string }; confirmation?: Omit<HarnessConfirmationRequest, 'id'> & { confirmation_id?: string; id?: string; message?: string; question?: string } }
      if (event.event === 'task.transport_error') {
        setError(data.error?.message ?? 'Harness 连接中断，正在等待恢复')
        return
      }
      if (['task.progress', 'task.result', 'task.completed'].includes(event.event)) {
        setError(previous => previous === 'Harness 连接中断，正在等待恢复' ? '' : previous)
      }
      const nextStatus: HarnessTask['status'] | null = event.event === 'task.started' ? 'running'
        : event.event === 'task.confirmation_required' ? 'waiting_confirmation'
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
        const request: Omit<HarnessConfirmationRequest, 'id' | 'prompt'> & { confirmation_id?: string; id?: string; prompt?: string; message?: string; question?: string } = data.confirmation ?? { confirmation_id: data.confirmation_id, prompt: data.prompt }
        const id = request.confirmation_id ?? request.id ?? (data.sequence ? `sequence-${data.sequence}` : '')
        if (id) {
          setConfirmation({ id, prompt: request.prompt ?? request.message ?? request.question ?? 'Harness 请求确认后继续执行', kind: request.kind, summary: request.summary, warnings: request.warnings,
            ambiguity_id: request.ambiguity_id, requirement_id: request.requirement_id, source_document_id: request.source_document_id, source_text: request.source_text,
            ambiguity_type: request.ambiguity_type, interpretation: request.interpretation })
          setConfirmationComment('')
        }
      }
      if (event.event === 'task.failed' || event.event === 'task.cancelled' || event.event === 'task.completed') {
        setConfirmation(null)
        if (event.event !== 'task.completed') setHarnessOutput('任务未完成，没有可用结果')
        if (event.event === 'task.failed' && data.error) {
          setError(data.error.message ?? data.error.code ?? 'DeepSeek Harness 任务失败')
        }
      }
      if (event.event === 'task.progress') {
        if (data.kind === 'chunk_started' || data.kind === 'chunk_completed') {
          const index = typeof data.chunk_index === 'number' ? data.chunk_index + 1 : '?'
          const total = typeof data.chunk_count === 'number' ? data.chunk_count : '?'
          const action = data.kind === 'chunk_started' ? '开始分析' : '完成分析'
          setHarnessProgress(previous => `${previous}${previous ? '\n' : ''}[${action}窗口 ${index}/${total}]`)
        } else if (data.kind === 'reduce_started') {
          const round = typeof data.round === 'number' ? data.round + 1 : '?'
          const batches = typeof data.batch_count === 'number' ? data.batch_count : '?'
          const inputs = typeof data.input_count === 'number' ? data.input_count : data.chunk_count ?? '?'
          setHarnessProgress(previous => `${previous}${previous ? '\n' : ''}[开始第 ${round} 轮聚合 ${inputs} 个结果 分 ${batches} 批]`)
        } else if (data.kind === 'reduce_completed') {
          const round = typeof data.round === 'number' ? data.round + 1 : '?'
          const batch = typeof data.batch_index === 'number' && typeof data.batch_count === 'number' ? `${data.batch_index + 1}/${data.batch_count}` : '?'
          setHarnessProgress(previous => `${previous}${previous ? '\n' : ''}[完成第 ${round} 轮聚合 ${batch}${data.final ? ' 最终' : ''}]`)
        }
      }
      if (event.event === 'task.completed') {
        void api<HarnessTaskResult>(`${base}/harness/tasks/${harnessTaskId}/result`)
          .then(response => {
            if (controller.signal.aborted) return
            const taskContext = harnessTaskRef.current
            const result = validateHarnessResultEnvelope(response, {
              taskId: harnessTaskId,
              projectId,
              expectedSchema: 'requirement-analysis.v1',
              expectedAnalysisRunId: taskContext?.analysis_run_id,
              expectedSourceDocumentIds: taskContext?.source_document_ids,
              authorizedSessionIds: [taskContext?.session_id ?? '', ...(taskContext?.session_ids ?? [])].filter(Boolean),
            })
            setHarnessOutput(JSON.stringify(result, null, 2))
          }).catch(cause => { if (!controller.signal.aborted) setError((cause as Error).message) })
        void refresh().catch(cause => { if (!controller.signal.aborted) setError((cause as Error).message) })
      }
    }).catch(cause => { if (!controller.signal.aborted) setError((cause as Error).message) })
    return () => controller.abort()
  }, [base, harnessTaskId, mode, projectId, refresh])

  const updateUploadItem = (id: string, status: UploadItem['status']) => {
    setUploadItems(previous => previous.map(item => item.id === id ? { ...item, status } : item))
  }

  async function uploadFiles(files: FileList | null) {
    if (!files?.length) return
    setBusy(true); setError(''); setNotice('')
    const selected = Array.from(files)
    const maxDocumentBytes = model?.max_document_bytes ?? 100 * 1024 * 1024
    const maxDocumentLabel = `${Math.round(maxDocumentBytes / 1024 / 1024)} MB`
    const items: UploadItem[] = selected.map((file, index) => ({ id: `${Date.now()}-${index}`, name: file.name, size: file.size, status: file.size > maxDocumentBytes ? 'rejected' : 'queued' }))
    setUploadItems(items)
    const oversized = selected.filter(file => file.size > maxDocumentBytes)
    const valid = selected.filter(file => file.size <= maxDocumentBytes)
    if (oversized.length) setError(`${oversized.map(file => file.name).join('、')} 超过 ${maxDocumentLabel} 限制，已跳过上传`)
    if (!valid.length) { setBusy(false); if (input.current) input.current.value = ''; return }
    try {
      const ids: string[] = []
      const failed: string[] = []
      for (const [index, file] of selected.entries()) {
        if (file.size > maxDocumentBytes) continue
        const item = items[index]
        if (item) updateUploadItem(item.id, 'uploading')
        const form = new FormData(); form.append('file', file)
        try {
          const result = await api<{ id: string }>(`${base}/documents`, { method: 'POST', body: form })
          ids.push(result.id)
          if (item) updateUploadItem(item.id, 'done')
        } catch (cause) {
          failed.push(`${file.name}：${(cause as Error).message}`)
          if (item) updateUploadItem(item.id, 'failed')
        }
      }
      if (ids.length) {
        await refresh(); setSelectedIds(ids); setDocumentId(ids.at(-1) ?? ''); setAnalysisId('')
        setNotice(`已上传 ${ids.length} 份文档，请核对提取内容`)
      }
      if (failed.length) setError(previous => [previous, ...failed].filter(Boolean).join('；'))
    } catch (cause) {
      setError(previous => [previous, (cause as Error).message].filter(Boolean).join('；'))
    } finally { setBusy(false); if (input.current) input.current.value = '' }
  }

  const formatUploadSize = (size: number) => size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`
  const uploadLimitLabel = `${Math.round((model?.max_document_bytes ?? 100 * 1024 * 1024) / 1024 / 1024)} MB`
  useEffect(() => {
    if (!documentId) return
    let active = true
    api<{ text: string }>(`${base}/documents/${documentId}`).then(result => { if (active) setDocumentText(result.text) }).catch(cause => setError(cause.message))
    return () => { active = false }
  }, [base, documentId])

  async function start() {
    if (!selectedIds.length) return
    if (!harnessEnabled) {
      setError('DeepSeek Harness 已被设为唯一执行引擎，但当前 Host 尚未配置')
      return
    }
    setBusy(true); setError('')
    try {
      if (harnessTask && ['completed', 'failed', 'cancelled'].includes(harnessTask.status)) {
        harnessRequestKey.current = ''
      }
      const inputFingerprint = selectedIds.join(',')
      if (!harnessRequestKey.current.startsWith(`${inputFingerprint}:`)) {
        harnessRequestKey.current = `${inputFingerprint}:${crypto.randomUUID()}`
      }
      const task = await api<HarnessTask>(`${base}/harness/tasks`, {
        method: 'POST',
        body: JSON.stringify({
          task_type: 'requirement-analysis',
          task_version: 'v1',
          source_document_ids: selectedIds,
          skill_id: 'requirement-analysis',
          input: { language: 'zh-CN', output_mode: 'structured' },
          idempotency_key: harnessRequestKey.current,
        }),
      })
      harnessTaskHistory.current = [task, ...harnessTaskHistory.current.filter(item => item.task_id !== task.task_id)]
      setHarnessTask(task); setHarnessOutput(''); setHarnessProgress(''); setAnalysisId(task.analysis_run_id ?? '')
      setNotice('DeepSeek Harness 任务已启动，正在接收流式结果')
    } catch (cause) {
      if (cause instanceof Error && (cause.message.startsWith('queue_full') || cause.message.startsWith('input_timeout') || cause.message.startsWith('window_policy_mismatch'))) {
        harnessRequestKey.current = ''
        setError(cause.message.startsWith('window_policy_mismatch') ? 'Harness 与 Test Atelier 的分窗配置不一致，请联系管理员核对'
          : cause.message.startsWith('input_timeout') ? 'Harness 分段上传超时，请重新发起任务' : 'Harness 当前并发容量已满，请稍后重试')
      } else setError((cause as Error).message)
    } finally { setBusy(false) }
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
      setHarnessTask(task); setHarnessOutput(''); setHarnessProgress(''); setConfirmation(null)
      setNotice('DeepSeek Harness 任务已重新启动')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function confirmHarnessTask(decision: 'approve' | 'reject', ambiguityChoice?: AmbiguityChoice) {
    if (!harnessTask || !confirmation) return
    const submittedId = confirmation.id
    if (ambiguityChoice === 'provide_explanation' && !confirmationComment.trim()) {
      setError('请先填写你对这条歧义的解释')
      return
    }
    setBusy(true); setError('')
    try {
      const task = await api<HarnessTask>(`${base}/harness/tasks/${harnessTask.task_id}/confirm`, {
        method: 'POST', body: JSON.stringify({ confirmation_id: confirmation.id, decision,
          ...(ambiguityChoice ? { ambiguity_choice: ambiguityChoice } : {}),
          ...(['skill_checkpoint', 'ambiguity_checkpoint'].includes(confirmation.kind ?? '') ? { comment: confirmationComment.trim() } : {}) }),
      })
      setHarnessTask(previous => previous?.task_id === task.task_id && previous.sequence > task.sequence ? previous : task)
      setConfirmation(previous => previous?.id === submittedId ? null : previous)
      setNotice(ambiguityChoice ? '已记录这条歧义的决定，继续核对下一条' : decision === 'approve' ? '已允许 Harness 继续' : '已拒绝 Harness 请求')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function retry(jobId: string) {
    setBusy(true); setError('')
    try { await api(`${base}/ai/jobs/${jobId}/retry`, { method: 'POST' }); setNotice('任务已重新排队') }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function removeAnalysis(run: Analysis) {
    if (run.status === 'queued' || run.status === 'running') {
      setError('分析任务正在处理中，请完成或停止任务后再删除')
      return
    }
    const title = run.title?.trim() || '这条分析记录'
    const prompt = run.status === 'deleting'
      ? `继续清理${title}的 Harness 任务、Session 和分析记录吗？Host 不可用时仍可稍后重试`
      : `确认删除${title}吗？关联的 Harness 任务、Session、分析报告和用例草稿都会删除`
    if (!window.confirm(prompt)) return
    setBusy(true); setError('')
    try {
      await api(`${base}/analyses/${run.id}`, { method: 'DELETE' })
      const remaining = analyses.filter(item => item.id !== run.id)
      setAnalyses(remaining)
      if (analysisId === run.id) {
        setAnalysisId(remaining[0]?.id ?? '')
        setAnalysis(null); setJobs([])
      }
      setNotice('分析记录已删除')
    } catch (cause) {
      setError((cause as Error).message)
      void Promise.all([refresh(), api<Analysis>(`${base}/analyses/${run.id}`).then(updated => {
        if (analysisId === updated.id) setAnalysis(updated)
      })]).catch(() => {})
    } finally { setBusy(false) }
  }
  async function saveReport() {
    if (!analysis) return
    setBusy(true); setError('')
    try {
      const run = await api<Analysis>(`${base}/analyses/${analysis.id}/report`, { method: 'PATCH', body: JSON.stringify({ version: analysis.version, report_markdown: reportText }) })
      setAnalysis(run); setNotice('分析报告已保存')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function saveDecision(id: string) {
    if (!analysis) return
    setBusy(true); setError('')
    try {
      const run = await api<Analysis>(`${base}/analyses/${analysis.id}/ambiguities/${id}`, { method: 'PATCH', body: JSON.stringify({ version: analysis.version, decision: decisions[id] }) })
      setAnalysis(run); setReportText(run.result.report_markdown ?? ''); setNotice('澄清结论已记录')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  const currentDoc = documents.find(doc => doc.id === documentId)
  const currentJob = analysis ? jobs.find(job => job.kind === 'analysis' && job.analysis_id === analysis.id) : undefined
  const requirementCount = analysis?.result.requirements?.length ?? 0
  const ambiguityCount = analysis?.result.ambiguities?.length ?? 0
  const unresolvedCritical = analysis?.result.ambiguities?.filter(item => item.critical && !item.decision?.trim()).length ?? 0
  const mappedSourceCount = analysis ? new Set((analysis.result.requirements ?? []).flatMap(item => item.source_ids)).size : 0
  const isHarnessAnalysis = analysis?.provider === 'harness'
  const harnessResult = analysis?.result.harness_result
  const validatedHarnessSnapshot = (() => {
    if (!analysis || !isHarnessAnalysis || analysis.status !== 'ready' || !harnessResult
      || typeof harnessResult.task_id !== 'string') return null
    try {
      return validateHarnessResultEnvelope({
        task_id: harnessResult.task_id,
        project_id: projectId,
        analysis_run_id: analysis.id,
        source_document_ids: analysis.source_ids,
        status: 'completed',
        result_schema: 'requirement-analysis.v1',
        result: harnessResult,
      }, { taskId: harnessResult.task_id, projectId, expectedSchema: 'requirement-analysis.v1', expectedSourceDocumentIds: analysis.source_ids })
    } catch {
      return null
    }
  })()
  const harnessReport = typeof validatedHarnessSnapshot?.report_markdown === 'string' ? validatedHarnessSnapshot.report_markdown : ''
  const reportTaskId = typeof validatedHarnessSnapshot?.task_id === 'string' ? validatedHarnessSnapshot.task_id : ''

  async function downloadHarnessReport() {
    if (!reportTaskId || !harnessReport) return
    setError('')
    try {
      const response = await rawApi(`${base}/harness/tasks/${encodeURIComponent(reportTaskId)}/report.md`)
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = url
      link.download = `Harness-analysis-${reportTaskId}.md`
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (cause) { setError((cause as Error).message) }
  }
  async function exportHarnessResult(taskId: string) {
    setError('')
    try { await downloadHarnessResult(projectId, taskId, 'requirement-analysis.v1') }
    catch (cause) { setError((cause as Error).message) }
  }
  const providerLabel = analysis?.provider === 'codex' || analysis?.provider === 'codex_cli'
    ? 'Codex CLI 本机'
    : analysis?.provider === 'claude' || analysis?.provider === 'claude_code'
      ? 'Claude Code 本机'
      : analysis?.provider === 'remote' ? '云端模型' : isHarnessAnalysis ? 'DeepSeek Harness' : 'AI 工具'
  // Keep the checkpoint language aligned with the requirement-analysis skill.
  // These are UI checkpoints only; the worker remains the source of truth.
  const workflow = [
    { label: '读取材料', done: documents.length > 0 },
    { label: 'Phase 2 解析', done: Boolean(analysis) },
    { label: 'Phase 2.5 解析确认', done: analysis?.status === 'ready' },
    { label: 'Phase 2.6 歧义处理', done: analysis?.status === 'ready' && unresolvedCritical === 0 },
    { label: 'Phase 2.8 分析预审', done: analysis?.status === 'ready' },
  ]
  return <div className="ai-workspace">
    {error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
    {mode === 'harness' && !harnessEnabled && <div className="ai-alert warning" role="status"><AlertCircle size={16} />需求分析仅由 DeepSeek Harness 执行，请先配置 Host 和作用域密钥</div>}
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    <div className="ai-columns">
      <aside className="ai-rail">
        <div className="ai-rail-heading"><div><h2>需求材料</h2><p className="ai-upload-limit-note">{mode === 'archive' ? '历史需求分析与报告，仅用于业务追溯' : `单文件最大 ${uploadLimitLabel} · Harness 分批传输并分窗分析，超出任务文本预算仍需拆分`}</p></div>{mode === 'harness' && <Button variant="outline" size="sm" onClick={() => input.current?.click()} disabled={readonly || busy}><Plus data-icon="inline-start" />上传</Button>}</div>
        <input ref={input} type="file" multiple hidden accept=".md,.markdown,.txt,.json,.yaml,.yml,.png,.jpg,.jpeg,.webp,.pdf,.doc,.docx" onChange={event => void uploadFiles(event.target.files)} />
        <AnimatePresence initial={false}>
          {uploadItems.length > 0 && <motion.div className="ai-upload-queue" role="status" aria-live="polite" initial={{ opacity: 0, height: 0, y: -4 }} animate={{ opacity: 1, height: 'auto', y: 0 }} exit={{ opacity: 0, height: 0, y: -4 }}>
            <div className="ai-upload-queue-head"><span>上传队列</span><button type="button" onClick={() => setUploadItems([])} disabled={busy}>清除</button></div>
            {uploadItems.map(item => <motion.div className={`ai-upload-item is-${item.status}`} key={item.id} initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 8 }}>
              <span className="ai-upload-item-name" title={item.name}>{item.name}</span><small>{formatUploadSize(item.size)}</small>
              <span className="ai-upload-item-status">{item.status === 'uploading' ? <><Loader2 size={12} className="spin" />上传中</> : item.status === 'done' ? '已完成' : item.status === 'rejected' ? '超过大小限制' : item.status === 'failed' ? '失败' : '等待中'}</span>
            </motion.div>)}
          </motion.div>}
        </AnimatePresence>
        {documents.length ? documents.map(doc => <div className="ai-document" key={doc.id}>
          {mode === 'harness' && <input type="checkbox" aria-label={`选择 ${doc.filename}`} checked={selectedIds.includes(doc.id)} onChange={event => setSelectedIds(previous => event.target.checked ? [...previous, doc.id] : previous.filter(id => id !== doc.id))} />}
          <button className={documentId === doc.id && !analysisId ? 'is-active' : ''} onClick={() => { setDocumentId(doc.id); setAnalysisId('') }}><FileText size={15} /><span>{doc.filename}</span></button>
        </div>) : <div className="ai-empty"><Upload size={22} /><span>尚无需求材料</span></div>}
        <div className="ai-rail-heading ai-analysis-heading"><h2>分析记录</h2></div>
        <AnimatePresence initial={false}>
          {analyses.map((run, index) => {
            const filenames = run.source_filenames?.length ? run.source_filenames : run.source_ids
              .map(sourceId => documents.find(document => document.id === sourceId)?.filename)
              .filter((filename): filename is string => Boolean(filename))
            // Keep older records readable when their source document was removed.
            const title = run.title?.trim() || filenames.join('、') || `需求分析 · ${run.source_ids.length} 份材料`
            return <motion.div key={run.id} className={`ai-rail-row ${analysisId === run.id ? 'is-active' : ''}`}
              initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 6 }}
              transition={{ duration: .2, delay: Math.min(index * .035, .21) }} title={title}>
              <button type="button" className="ai-analysis-select" data-analysis-id={run.id} onClick={() => setAnalysisId(run.id)}>
                <span className="ai-rail-row-copy"><strong>{title}</strong><small>{new Date(run.created_at).toLocaleString('zh-CN')}</small></span>
                <Badge variant="secondary">{run.status === 'ready' ? '已完成' : run.status === 'failed' ? '失败' : run.status === 'queued' ? '排队中' : run.status === 'deleting' ? '清理待重试' : '处理中'}</Badge>
              </button>
              <Button type="button" variant="ghost" size="icon" className="ai-analysis-delete" aria-label={`${run.status === 'deleting' ? '继续清理' : '删除'} ${title}`} title={run.status === 'deleting' ? '继续清理 Harness 历史' : '删除分析记录'} onClick={() => void removeAnalysis(run)} disabled={readonly || busy || run.status === 'queued' || run.status === 'running'}>{run.status === 'deleting' ? <RefreshCw size={14} /> : <Trash2 size={14} />}</Button>
            </motion.div>
          })}
        </AnimatePresence>
        {mode === 'harness' && <div className="ai-rail-footer"><div className={`harness-runtime-badge${!harnessEnabled ? ' is-unavailable' : ''}`}><span className="harness-runtime-dot" />{harnessEnabled ? 'DeepSeek Harness 执行' : 'DeepSeek Harness 未配置'}</div><Button onClick={() => void start()} disabled={readonly || busy || !selectedIds.length || !harnessEnabled}>开始分析</Button></div>}
      </aside>
      <section className="ai-main">
        {mode === 'harness' && harnessTask && harnessTask.status !== 'completed' && <motion.div className="harness-stream-panel" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}><div className="ai-section-heading"><div><h2>DeepSeek Harness 输出</h2><p>{harnessTask.status === 'running' ? '正在执行项目技能' : '任务状态：' + harnessTask.status}</p></div><div className="ai-heading-actions"><Badge variant="secondary">{harnessTask.result_schema}</Badge>{harnessTask.session_id && <Button variant="ghost" size="sm" onClick={() => setSessionOpen(true)}><Eye data-icon="inline-start" />查看 Session</Button>}{['failed', 'cancelled'].includes(harnessTask.status) && !readonly && <Button variant="outline" size="sm" onClick={() => void retryHarnessTask()} disabled={busy}><RefreshCw data-icon="inline-start" />重试</Button>}{['queued', 'starting', 'running', 'waiting_confirmation'].includes(harnessTask.status) && <Button variant="outline" size="sm" onClick={() => void cancelHarnessTask()} disabled={busy}><Square data-icon="inline-start" />停止</Button>}</div></div>{confirmation && <div className="harness-confirmation" role="dialog" aria-label={confirmation.kind === 'ambiguity_checkpoint' ? '关键歧义逐项决定' : 'Harness 确认'}>
          <strong>{confirmation.kind === 'skill_checkpoint' ? 'Phase 2.5 解析确认' : confirmation.kind === 'ambiguity_checkpoint' ? 'Phase 2.6 关键歧义' : '需要你的确认'}</strong>
          <p>{confirmation.prompt}</p>
          {confirmation.kind === 'skill_checkpoint' && <>
            <article className="harness-checkpoint-summary"><ReactMarkdown remarkPlugins={[remarkGfm]} components={safeMarkdownComponents}>{confirmation.summary ?? ''}</ReactMarkdown></article>
            {Boolean(confirmation.warnings?.length) && <ul>{confirmation.warnings?.map((warning, index) => <li key={`${index}-${warning}`}>{warning}</li>)}</ul>}
            <Textarea aria-label="解析澄清或适用端" placeholder="可补充适用端或解析澄清；不确定时请在报告中保留待确认" value={confirmationComment} maxLength={2000} onChange={event => setConfirmationComment(event.target.value)} disabled={readonly || busy} />
          </>}
          {confirmation.kind === 'ambiguity_checkpoint' && <>
            <dl className="harness-ambiguity-fields">
              <dt>需求 ID</dt><dd>{confirmation.requirement_id}</dd>
              <dt>来源 ID</dt><dd>{confirmation.source_document_id}</dd>
              <dt>原文</dt><dd>{confirmation.source_text}</dd>
              <dt>类型</dt><dd>{confirmation.ambiguity_type}</dd>
              <dt>建议理解</dt><dd>{confirmation.interpretation}</dd>
            </dl>
            <Textarea aria-label="你对关键歧义的解释" placeholder="选择提供解释时填写；其他选择无需填写" value={confirmationComment} maxLength={2000} onChange={event => setConfirmationComment(event.target.value)} disabled={readonly || busy} />
            <p>此处决定会进入当前 Harness Session，但不会自动修改原需求或将阻断歧义标为已确认</p>
          </>}
          <div>
            {confirmation.kind === 'ambiguity_checkpoint' ? <>
              <Button size="sm" onClick={() => void confirmHarnessTask('approve', 'accept_suggestion')} disabled={readonly || busy}>接受建议</Button>
              <Button size="sm" variant="outline" onClick={() => void confirmHarnessTask('approve', 'provide_explanation')} disabled={readonly || busy}>提供解释</Button>
              <Button size="sm" variant="outline" onClick={() => void confirmHarnessTask('approve', 'skip')} disabled={readonly || busy}>跳过</Button>
              <Button size="sm" variant="outline" onClick={() => void confirmHarnessTask('approve', 'mark_pending')} disabled={readonly || busy}>标记待确认</Button>
            </> : <>
              <Button size="sm" onClick={() => void confirmHarnessTask('approve')} disabled={readonly || busy}>{confirmation.kind === 'skill_checkpoint' ? '确认解析后继续' : '允许继续'}</Button>
              <Button size="sm" variant="outline" onClick={() => void confirmHarnessTask('reject')} disabled={readonly || busy}>拒绝</Button>
            </>}
          </div>
        </div>}
        {harnessProgress && <pre className="harness-stream-output" aria-label="任务进度 非结果">{harnessProgress}</pre>}
        <pre className="harness-stream-output" data-testid="harness-analysis-result" aria-label="Harness 完成结果">{harnessOutput || '任务完成后显示 Harness 原始结果'}</pre></motion.div>}
        <AnimatePresence mode="wait">{analysis ? <motion.div key={analysis.id} initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -7 }} className="ai-report">
          <div className="ai-section-heading"><div><h2>分析报告</h2><p>{analysis.source_ids.length} 份材料 · {analysis.result.requirements?.length ?? 0} 条需求</p></div>
            <div className="ai-heading-actions">{currentJob && <Button variant="outline" size="sm" onClick={() => setPromptOpen(true)}><Eye data-icon="inline-start" />查看提示词</Button>}{harnessTask?.session_id && <Button variant="ghost" size="sm" onClick={() => setSessionOpen(true)}><Eye data-icon="inline-start" />查看 Session</Button>}{reportTaskId && isHarnessAnalysis && <Button variant="outline" size="sm" onClick={() => void exportHarnessResult(reportTaskId)}><Download data-icon="inline-start" />下载原始 JSON</Button>}{analysis.status === 'ready' && <Button variant="outline" size="sm" onClick={() => onOpenAuthoring(analysis.id)}>{mode === 'archive' ? '进入草稿审核' : '进入用例编写'}<ArrowRight data-icon="inline-end" /></Button>}</div></div>
          {!isHarnessAnalysis && <motion.div className="analysis-flow" aria-label="需求分析流程" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }} transition={{ delay: .06, duration: .22 }}>
            <div className="analysis-flow-head"><span>{providerLabel === 'Codex CLI 本机' ? 'Codex 需求分析流程' : '需求分析流程'}</span><Badge variant="secondary">{providerLabel}</Badge></div>
            <div className="analysis-flow-steps">{workflow.map((step, index) => <motion.div className={`analysis-flow-step ${step.done ? 'is-done' : ''} ${analysis.status !== 'ready' && index === 1 ? 'is-active' : ''}`} key={step.label} initial={{ opacity: 0, x: -5 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -5 }} transition={{ delay: .08 + index * .035, duration: .18 }}><span className="analysis-flow-dot" aria-hidden="true">{step.done ? <Check size={11} strokeWidth={2.5} /> : index + 1}</span><span>{step.label}</span>{index < workflow.length - 1 && <span className="analysis-flow-line" aria-hidden="true" />}</motion.div>)}</div>
          </motion.div>}
          {analysis.status === 'ready' && !isHarnessAnalysis && <motion.div className="analysis-metrics" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }} transition={{ delay: .12, duration: .22 }}>
            <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }} transition={{ delay: .14, duration: .18 }}><strong>{requirementCount}</strong><span>条需求</span></motion.div><motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }} transition={{ delay: .17, duration: .18 }}><strong>{mappedSourceCount}/{analysis.source_ids.length}</strong><span>来源已映射</span></motion.div><motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }} transition={{ delay: .2, duration: .18 }}><strong>{ambiguityCount}</strong><span>待澄清</span></motion.div><motion.div className={unresolvedCritical ? 'is-warning' : ''} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }} transition={{ delay: .23, duration: .18 }}><strong>{unresolvedCritical}</strong><span>关键未确认</span></motion.div>
          </motion.div>}
          {analysis.status === 'failed' ? <div className="ai-alert error"><AlertCircle size={16} />{formatAiError(analysis.error)}{currentJob && !readonly && <Button variant="outline" size="sm" onClick={() => void retry(currentJob.id)} disabled={busy}><RefreshCw data-icon="inline-start" />重试</Button>}</div> : null}
          {analysis.status === 'queued' ? <div className="ai-processing" role="status">任务已排队，等待前一项完成后自动开始</div> : null}
          {analysis.status === 'running' ? <div className="ai-processing" role="status">正在处理需求材料，请稍候</div> : null}
          {analysis.status === 'deleting' ? <div className="ai-alert warning" role="status">Harness 历史清理尚未完成，可在左侧点击继续清理；任务和 Session 在安全完成前仍保留</div> : null}
          {analysis.status === 'ready' && <>{isHarnessAnalysis && <motion.section className="harness-result-panel" initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -7 }}>
            <div className="ai-section-heading"><div><h3>需求分析正文</h3><p>Harness 已完成校验 · 结果只读展示</p></div><div className="ai-heading-actions"><Badge variant="secondary">requirement-analysis.v1</Badge>{reportTaskId && <Button size="sm" variant="outline" onClick={() => void downloadHarnessReport()}><Download data-icon="inline-start" />下载 Markdown</Button>}</div></div>
            {harnessReport ? <article className="report-markdown" data-testid="harness-analysis-report"><ReactMarkdown remarkPlugins={[remarkGfm]} components={safeMarkdownComponents}>{harnessReport}</ReactMarkdown></article> : <div className="ai-alert warning">该历史记录没有可展示的分析正文，请查看受保护的 Session</div>}
          </motion.section>}<div className="ai-section-heading"><div><h3>{isHarnessAnalysis ? 'Test Atelier 人工澄清' : '待澄清'}</h3><span>{clarificationItems.length} 项</span></div>{isHarnessAnalysis && clarificationItems.length > 0 && <Button type="button" variant="outline" size="sm" onClick={() => void copyClarifications()}><Copy data-icon="inline-start" />{clarificationsCopied ? '已复制' : '复制给产品经理'}</Button>}</div>
            {isHarnessAnalysis && clarificationItems.length > 0 && <div className="harness-clarification-copy" role="note"><p>按编号整理待澄清点，复制后可直接粘贴到沟通工具</p><pre>{clarificationText}</pre></div>}
            {(analysis.result.ambiguities ?? []).map(item => <div className="ambiguity-row" key={item.id}><div><strong>{item.id} {item.description}</strong>{item.critical && <Badge variant="destructive">关键</Badge>}</div>
              <div><Input value={decisions[item.id] ?? ''} placeholder="填写确认结论" onChange={event => setDecisions(previous => ({ ...previous, [item.id]: event.target.value }))} disabled={readonly} /><Button size="sm" variant="outline" onClick={() => void saveDecision(item.id)} disabled={readonly || busy || !(decisions[item.id] ?? '').trim()}>确认</Button></div></div>)}
            {!isHarnessAnalysis && <><div className="ai-section-heading report-edit-heading"><div><h3>报告正文</h3><p>支持 Markdown 预览与编辑</p></div><div className="report-heading-actions"><div className="report-mode-switch" role="tablist" aria-label="报告显示模式">
              <Button type="button" size="sm" variant={reportMode === 'preview' ? 'secondary' : 'ghost'} role="tab" aria-selected={reportMode === 'preview'} onClick={() => setReportMode('preview')}><Eye data-icon="inline-start" />预览</Button>
              <Button type="button" size="sm" variant={reportMode === 'edit' ? 'secondary' : 'ghost'} role="tab" aria-selected={reportMode === 'edit'} onClick={() => setReportMode('edit')}><PencilLine data-icon="inline-start" />编辑 Markdown</Button>
            </div><Button size="sm" onClick={() => void saveReport()} disabled={readonly || busy || !reportText.trim() || reportMode !== 'edit'}><Save data-icon="inline-start" />保存</Button></div></div>
            <AnimatePresence mode="wait" initial={false}>{reportMode === 'preview' ? <motion.article key="report-preview" className="report-markdown" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }} transition={{ duration: .16 }}>
              {reportText.trim() ? <ReactMarkdown remarkPlugins={[remarkGfm]} components={safeMarkdownComponents}>{reportText}</ReactMarkdown> : <p className="report-markdown-empty">暂无报告正文</p>}
            </motion.article> : <motion.div key="report-editor" className="report-editor-frame" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }} transition={{ duration: .16 }}><Textarea className="report-editor" value={reportText} onChange={event => setReportText(event.target.value)} readOnly={readonly} aria-label="分析报告正文 Markdown" /></motion.div>}</AnimatePresence></>}</>}
        </motion.div> : <motion.div key={documentId || 'empty'} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="ai-report">
          {currentDoc ? <><div className="ai-section-heading"><div><h2>{currentDoc.filename}</h2><p>提取内容预览 · {currentDoc.text_length} 字符</p></div></div>
            {currentDoc.warnings.map(warning => <div className="ai-alert warning" key={warning}><AlertCircle size={16} />{warning}</div>)}
            <pre className="document-preview">{documentText}</pre></> : <div className="ai-empty main"><FileText size={26} /><h2>选择或上传需求材料</h2></div>}
        </motion.div>}</AnimatePresence>
      </section>
    </div>
    <PromptViewerDialog projectId={projectId} jobId={currentJob?.id ?? null} open={promptOpen} onOpenChange={setPromptOpen} />
    <HarnessSessionDialog projectId={projectId} taskId={harnessTask?.task_id ?? null} sessionId={harnessTask?.session_id ?? null} sessionIds={harnessTask?.session_ids ?? []} open={sessionOpen} onOpenChange={setSessionOpen} />
  </div>
}
