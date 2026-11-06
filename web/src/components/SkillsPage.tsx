import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { BookOpen, Check, CircleOff, Clock3, Download, Eye, FileText, Play, RefreshCw, Square } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { api, followHarnessTask, type HarnessTaskResult, type ProjectSkill, type SourceDocument } from '@/lib/api'
import { HarnessSessionDialog } from '@/components/HarnessSessionDialog'
import { downloadHarnessResult } from '@/lib/harnessResultExport'
import { validateHarnessResultEnvelope } from '@/lib/harnessResult'
import type { HarnessTask } from '@/lib/api'

type HarnessSkillCapability = { skill_id: string; task_type: string; result_schema: string; confirmation: string }
type HarnessConfig = { enabled: boolean; task_types: string[]; result_schemas: string[]; skills?: HarnessSkillCapability[] }
type TaskConfirmation = { taskId: string; id: string; prompt: string }
const ACTIVE_STATUSES: HarnessTask['status'][] = ['queued', 'starting', 'running', 'waiting_confirmation']

export function SkillsPage({ projectId, readonly }: { projectId: number; readonly: boolean }) {
  const [skills, setSkills] = useState<ProjectSkill[]>([])
  const [harness, setHarness] = useState<HarnessConfig | null>(null)
  const [tasks, setTasks] = useState<HarnessTask[]>([])
  const [documents, setDocuments] = useState<SourceDocument[]>([])
  const [selectedDocuments, setSelectedDocuments] = useState<string[]>([])
  const [replayTask, setReplayTask] = useState<HarnessTask | null>(null)
  const [resultTask, setResultTask] = useState<HarnessTask | null>(null)
  const [resultText, setResultText] = useState('')
  const [confirmations, setConfirmations] = useState<Record<string, TaskConfirmation>>({})
  const subscriptions = useRef(new Map<string, AbortController>())
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const base = `/projects/${projectId}`
  const load = useCallback(async () => {
    setError('')
    try {
      const [projectSkills, harnessConfig, recentTasks, projectDocuments] = await Promise.all([
        api<ProjectSkill[]>(`${base}/skills`),
        api<HarnessConfig>(`${base}/harness/config`),
        api<HarnessTask[]>(`${base}/harness/tasks?limit=100`),
        api<SourceDocument[]>(`${base}/documents`),
      ])
      setSkills(projectSkills)
      setHarness(harnessConfig)
      setTasks(recentTasks)
      setDocuments(projectDocuments)
    }
    catch (cause) { setError((cause as Error).message) }
  }, [base])
  useEffect(() => { void load() }, [load])
  const activeTaskIds = tasks.filter(task => ACTIVE_STATUSES.includes(task.status)).map(task => task.task_id).join(',')
  useEffect(() => {
    const active = new Set(activeTaskIds ? activeTaskIds.split(',') : [])
    for (const [taskId, controller] of subscriptions.current) {
      if (active.has(taskId)) continue
      controller.abort()
      subscriptions.current.delete(taskId)
    }
    for (const taskId of active) {
      if (subscriptions.current.has(taskId)) continue
      const controller = new AbortController()
      subscriptions.current.set(taskId, controller)
      void followHarnessTask(`${base}/harness/tasks/${taskId}`, controller.signal, event => {
        if (controller.signal.aborted) return
        const data = event.data as { status?: HarnessTask['status']; kind?: string; error?: { message?: string }; confirmation?: { confirmation_id?: string; id?: string; prompt?: string; message?: string; question?: string }; confirmation_id?: string; prompt?: string }
        if (event.event === 'task.transport_error') {
          setError(data.error?.message ?? 'Harness 连接中断，正在等待恢复')
          return
        }
        if (['task.progress', 'task.result', 'task.completed'].includes(event.event)) {
          setError(previous => previous === 'Harness 连接中断，正在等待恢复' ? '' : previous)
        }
        const status = event.event === 'task.started' ? 'running' : event.event === 'task.completed' ? 'completed' : event.event === 'task.failed' ? 'failed' : event.event === 'task.cancelled' ? 'cancelled' : event.event === 'task.confirmation_required' ? 'waiting_confirmation' : data.status
        if (status) setTasks(previous => previous.map(task => task.task_id === taskId ? { ...task, status } : task))
        if (event.event === 'task.confirmation_required') {
          const request = data.confirmation ?? {}
          const id = request.confirmation_id ?? request.id ?? data.confirmation_id
          if (id) setConfirmations(previous => ({ ...previous, [taskId]: { taskId, id, prompt: request.prompt ?? request.message ?? request.question ?? 'Harness 请求确认后继续执行' } }))
        }
        if (data.kind === 'confirmation_submitted' || ['task.completed', 'task.failed', 'task.cancelled'].includes(event.event)) {
          setConfirmations(previous => {
            if (!previous[taskId]) return previous
            const next = { ...previous }
            delete next[taskId]
            return next
          })
        }
        if (event.event === 'task.failed' && data.error?.message) setError(data.error.message)
      }).catch(cause => { if (!controller.signal.aborted) setError((cause as Error).message) })
    }
  }, [base, activeTaskIds])
  useEffect(() => () => {
    for (const controller of subscriptions.current.values()) controller.abort()
    subscriptions.current.clear()
  }, [base])
  async function toggle(skill: ProjectSkill) {
    setBusy(skill.skill_id); setError('')
    try {
      const updated = await api<{ skill_id: string; enabled: boolean }>(`${base}/skills/${skill.skill_id}`, {
        method: 'PATCH', body: JSON.stringify({ enabled: !skill.enabled }),
      })
      setSkills(previous => previous.map(item => item.skill_id === skill.skill_id ? { ...item, enabled: updated.enabled } : item))
    } catch (cause) { setError((cause as Error).message) } finally { setBusy('') }
  }
  const harnessSkillIds = new Set((harness?.skills ?? []).map(item => item.skill_id))
  const executableSkillIds = new Set((harness?.skills ?? []).filter(item => item.task_type === 'skill-execution').map(item => item.skill_id))
  const taskLabel = (task: HarnessTask) => task.task_type === 'requirement-analysis' ? '需求分析' : task.task_type === 'test-cases' ? '用例编写' : task.skill_id || '项目技能'
  const taskStatus = (status: HarnessTask['status']) => ({ queued: '排队中', starting: '启动中', running: '执行中', waiting_confirmation: '等待确认', completed: '已完成', failed: '失败', cancelled: '已取消' })[status]
  async function executeSkill(skill: ProjectSkill) {
    if (!harness?.enabled || !executableSkillIds.has(skill.skill_id) || !selectedDocuments.length) return
    setBusy(skill.skill_id); setError('')
    try {
      const task = await api<HarnessTask>(`${base}/harness/tasks`, {
        method: 'POST',
        body: JSON.stringify({ task_type: 'skill-execution', skill_id: skill.skill_id, source_document_ids: selectedDocuments, idempotency_key: `skill-${skill.skill_id}-${Date.now()}` }),
      })
      setTasks(previous => [task, ...previous.filter(item => item.task_id !== task.task_id)])
    } catch (cause) {
      const message = (cause as Error).message
      setError(message.startsWith('window_policy_mismatch') ? 'Harness 与 Test Atelier 的分窗配置不一致，请联系管理员核对' : message)
    } finally { setBusy('') }
  }
  async function showResult(task: HarnessTask) {
    setBusy(`result:${task.task_id}`); setError('')
    try {
      const result = await api<HarnessTaskResult>(`${base}/harness/tasks/${task.task_id}/result`)
      const payload = validateHarnessResultEnvelope(result, {
        taskId: task.task_id,
        projectId,
        expectedSchema: 'skill-execution.v1',
        expectedSourceDocumentIds: task.source_document_ids,
        authorizedSessionIds: [task.session_id ?? '', ...(task.session_ids ?? [])].filter(Boolean),
      })
      setResultTask(task)
      setResultText(JSON.stringify(payload, null, 2))
    } catch (cause) { setError((cause as Error).message) } finally { setBusy('') }
  }
  async function exportResult(task: HarnessTask) {
    setBusy(`export:${task.task_id}`); setError('')
    try { await downloadHarnessResult(projectId, task.task_id, 'skill-execution.v1') }
    catch (cause) { setError((cause as Error).message) }
    finally { setBusy('') }
  }
  async function cancelTask(task: HarnessTask) {
    if (!['queued', 'starting', 'running', 'waiting_confirmation'].includes(task.status)) return
    setBusy(`cancel:${task.task_id}`); setError('')
    try {
      const updated = await api<HarnessTask>(`${base}/harness/tasks/${task.task_id}/cancel`, { method: 'POST' })
      setTasks(previous => previous.map(item => item.task_id === task.task_id ? updated : item))
      setConfirmations(previous => {
        if (!previous[task.task_id]) return previous
        const next = { ...previous }
        delete next[task.task_id]
        return next
      })
    } catch (cause) { setError((cause as Error).message) } finally { setBusy('') }
  }
  async function retryTask(task: HarnessTask) {
    if (!['failed', 'cancelled'].includes(task.status)) return
    setBusy(`retry:${task.task_id}`); setError('')
    try {
      const updated = await api<HarnessTask>(`${base}/harness/tasks/${task.task_id}/retry`, { method: 'POST' })
      // Keep the failed Attempt visible for audit/replay; the new task owns
      // its own Session and is linked through ``retry_of``.
      setTasks(previous => previous.some(item => item.task_id === updated.task_id) ? previous : [updated, ...previous])
    } catch (cause) { setError((cause as Error).message) } finally { setBusy('') }
  }
  async function confirmTask(confirmation: TaskConfirmation, decision: 'approve' | 'reject') {
    setBusy(`confirm:${confirmation.taskId}`); setError('')
    try {
      const updated = await api<HarnessTask>(`${base}/harness/tasks/${confirmation.taskId}/confirm`, {
        method: 'POST', body: JSON.stringify({ confirmation_id: confirmation.id, decision }),
      })
      setTasks(previous => previous.map(item => item.task_id === confirmation.taskId ? updated : item))
      setConfirmations(previous => {
        if (previous[confirmation.taskId]?.id !== confirmation.id) return previous
        const next = { ...previous }
        delete next[confirmation.taskId]
        return next
      })
    } catch (cause) { setError((cause as Error).message) } finally { setBusy('') }
  }
  const pendingConfirmations = Object.values(confirmations)
  const confirmation = pendingConfirmations[0]
  const visibleTasks = [...tasks.filter(task => ACTIVE_STATUSES.includes(task.status)),
    ...tasks.filter(task => !ACTIVE_STATUSES.includes(task.status)).slice(0, 8)]
  return <div className="skills-page">
    <div className="skills-toolbar"><div><strong>项目技能</strong><span>技能规范来自 qa-doc-skills，执行由 DeepSeek Harness 承担</span></div><div className="skills-toolbar-actions">{harness && <Badge variant={harness.enabled ? 'secondary' : 'outline'}>{harness.enabled ? 'Harness 已配置' : 'Harness 未配置'}</Badge>}<Button variant="outline" size="sm" onClick={() => void load()} disabled={Boolean(busy)}><RefreshCw data-icon="inline-start" />刷新</Button></div></div>
    {error && <div className="ai-alert error" role="alert">{error}</div>}
    <div className="skills-grid"><AnimatePresence initial={false}>
      {skills.map((skill, index) => <motion.article key={skill.skill_id} className={`skill-card ${skill.enabled ? '' : 'is-disabled'}`} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={{ delay: index * .04 }}>
        <div className="skill-card-icon">{skill.enabled ? <Check size={18} /> : <CircleOff size={18} />}</div>
        <div className="skill-card-body"><div className="skill-card-heading"><h2>{skill.label}</h2><div className="skill-card-badges"><Badge variant={skill.enabled ? 'secondary' : 'outline'}>{skill.enabled ? '已启用' : '已停用'}</Badge>{skill.enabled && harness?.enabled && harnessSkillIds.has(skill.skill_id) && <Badge variant="outline">Harness 执行</Badge>}</div></div><p>{skill.description || '来自 qa-doc-skills 的项目技能'}</p>{skill.skill_id === 'figma-to-prd' && <motion.p className="skill-source-note" initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: .2 }}>只读取已上传的设计导出文档，不会打开 Figma 链接，原稿来源需人工核对</motion.p>}<code>{skill.source_path}</code></div>
        {!readonly && <div className="skill-card-actions"><Button variant="outline" size="sm" onClick={() => void toggle(skill)} disabled={busy === skill.skill_id}>{busy === skill.skill_id ? '保存中' : skill.enabled ? '停用' : '启用'}</Button>{executableSkillIds.has(skill.skill_id) && skill.enabled && harness?.enabled && <Button variant="secondary" size="sm" onClick={() => void executeSkill(skill)} disabled={Boolean(busy) || !selectedDocuments.length}><Play data-icon="inline-start" />执行</Button>}</div>}
      </motion.article>)}
    </AnimatePresence></div>
    {harness?.enabled && <motion.section className="harness-skill-input" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: .1 }}><div><strong>技能输入材料</strong><span>先选择授权文档，再运行已开放的 Harness 项目技能</span></div><div className="harness-skill-docs">{documents.length ? documents.map(document => <label key={document.id} className="harness-skill-doc"><input type="checkbox" checked={selectedDocuments.includes(document.id)} onChange={event => setSelectedDocuments(previous => event.target.checked ? [...previous, document.id] : previous.filter(id => id !== document.id))} /><FileText size={14} /><span>{document.filename}</span></label>) : <small>暂无已上传文档</small>}</div></motion.section>}
    {harness?.enabled && <motion.section className="harness-skill-runs" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: .16 }}>
      <div className="skills-toolbar"><div><strong>Harness 执行记录</strong><span>断开页面后任务仍由 Projector Worker 持续投影</span></div><Clock3 size={17} aria-hidden="true" /></div>
      {tasks.length ? <div className="harness-skill-runs-list">{visibleTasks.map((task, index) => <motion.div key={task.task_id} className="harness-skill-run" initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 8 }} transition={{ delay: .2 + index * .035 }}>
        <div className="harness-skill-run-copy"><strong>{taskLabel(task)}{task.retry_of ? ' · 重试任务' : ''}</strong><span>{taskStatus(task.status)} · {task.result_schema}{task.retry_of ? ` · 基于 ${task.retry_of.slice(0, 8)}` : ''}</span></div>
        <div className="harness-skill-run-actions">{task.status === 'completed' && <Button variant="ghost" size="sm" onClick={() => void showResult(task)} disabled={busy === `result:${task.task_id}`}><Eye data-icon="inline-start" />查看结果</Button>}{task.session_id && <Button variant="ghost" size="sm" onClick={() => setReplayTask(task)}><Eye data-icon="inline-start" />查看 Session</Button>}{['failed', 'cancelled'].includes(task.status) && !readonly && <Button variant="ghost" size="sm" onClick={() => void retryTask(task)} disabled={busy === `retry:${task.task_id}`}><RefreshCw data-icon="inline-start" />重试</Button>}{['queued', 'starting', 'running', 'waiting_confirmation'].includes(task.status) && !readonly && <Button variant="ghost" size="sm" onClick={() => void cancelTask(task)} disabled={busy === `cancel:${task.task_id}`}><Square data-icon="inline-start" />停止</Button>}</div>
      </motion.div>)}</div> : <div className="harness-skill-runs-empty">暂无 Harness 执行记录</div>}
    </motion.section>}
    {!skills.length && !error && <div className="ai-empty main"><BookOpen size={26} /><h2>正在读取项目技能</h2></div>}
    <HarnessSessionDialog projectId={projectId} taskId={replayTask?.task_id ?? null} sessionId={replayTask?.session_id ?? null} sessionIds={replayTask?.session_ids ?? []} open={Boolean(replayTask)} onOpenChange={open => { if (!open) setReplayTask(null) }} />
    <AnimatePresence>{confirmation && <motion.div className="harness-confirmation-overlay" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}><motion.div key={confirmation.taskId} className="harness-confirmation-card" role="dialog" aria-label="Harness 请求确认" initial={{ opacity: 0, y: 10, scale: .98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: .98 }}><strong>需要你的确认{pendingConfirmations.length > 1 ? ` · 待处理 ${pendingConfirmations.length} 项` : ''}</strong><small>{tasks.find(task => task.task_id === confirmation.taskId)?.skill_id ?? '项目技能'} · {confirmation.taskId.slice(0, 8)}</small><p>{confirmation.prompt}</p><div><Button size="sm" onClick={() => void confirmTask(confirmation, 'approve')} disabled={Boolean(busy)}>允许继续</Button><Button size="sm" variant="outline" onClick={() => void confirmTask(confirmation, 'reject')} disabled={Boolean(busy)}>拒绝</Button></div></motion.div></motion.div>}</AnimatePresence>
    <AnimatePresence>{resultTask && <motion.div className="harness-result-dialog" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} role="dialog" aria-label="Harness 结果"><motion.div className="harness-result-dialog-card" initial={{ opacity: 0, y: 12, scale: .98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: .98 }}><div className="ai-section-heading"><div><h2>Harness 返回结果</h2><p>{resultTask.result_schema}</p></div><div className="ai-heading-actions"><motion.div initial={{ opacity: 0, scale: .96 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: .96 }} whileHover={{ scale: 1.02 }} whileTap={{ scale: .97 }}><Button variant="outline" size="sm" onClick={() => void exportResult(resultTask)} disabled={busy === `export:${resultTask.task_id}`}><Download data-icon="inline-start" />下载原始 JSON</Button></motion.div><Button variant="ghost" size="sm" onClick={() => setResultTask(null)}>关闭</Button></div></div><pre className="harness-stream-output">{resultText}</pre></motion.div></motion.div>}</AnimatePresence>
  </div>
}
