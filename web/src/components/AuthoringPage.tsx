import { useCallback, useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { AlertCircle, Download, Eye, Plus, RefreshCw, Save, Send, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { api, formatAiError, rawApi, type AiJob, type Analysis, type Draft, type DraftPayload } from '@/lib/api'
import { PromptViewerDialog } from '@/components/PromptViewerDialog'

function download(response: Response, blob: Blob) {
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const match = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  const filename = match ? decodeURIComponent(match[1]) : '用例草稿'
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a'); link.href = url; link.download = filename
  document.body.appendChild(link); link.click(); link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function AuthoringPage({ projectId, readonly, onOpenCases }: { projectId: number; readonly: boolean; onOpenCases: () => void }) {
  const base = `/projects/${projectId}`
  const [analyses, setAnalyses] = useState<Analysis[]>([])
  const [analysisId, setAnalysisId] = useState('')
  const [drafts, setDrafts] = useState<Draft[]>([])
  const [selected, setSelected] = useState('')
  const [form, setForm] = useState<DraftPayload | null>(null)
  const [job, setJob] = useState<AiJob | null>(null)
  const [quality, setQuality] = useState<{ coverage: number; errors: string[]; warnings: string[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [promptOpen, setPromptOpen] = useState(false)

  const refresh = useCallback(async () => {
    const runs = await api<Analysis[]>(`${base}/analyses`)
    setAnalyses(runs.filter(run => run.status === 'ready'))
    if (!analysisId && runs.some(run => run.status === 'ready')) setAnalysisId(runs.find(run => run.status === 'ready')!.id)
  }, [base, analysisId])
  const loadDrafts = useCallback(async () => {
    if (!analysisId) { setDrafts([]); return }
    const [result, tasks, summary] = await Promise.all([
      api<Draft[]>(`${base}/drafts?analysis_id=${analysisId}`), api<AiJob[]>(`${base}/ai/jobs?analysis_id=${analysisId}`),
      api<{ coverage: number; errors: string[]; warnings: string[] }>(`${base}/analyses/${analysisId}/quality`),
    ])
    setDrafts(result); setJob(tasks.find(item => item.kind === 'cases') ?? null); setQuality(summary)
  }, [base, analysisId])
  useEffect(() => { void refresh().catch(cause => setError(cause.message)) }, [refresh])
  useEffect(() => { setSelected(''); setForm(null); void loadDrafts().catch(cause => setError(cause.message)) }, [loadDrafts])
  useEffect(() => {
    if (job?.status !== 'queued' && job?.status !== 'running') return
    const interval = window.setInterval(() => { void loadDrafts().catch(cause => setError(cause.message)) }, 2500)
    return () => window.clearInterval(interval)
  }, [job?.status, loadDrafts])
  useEffect(() => {
    const draft = drafts.find(item => item.id === selected)
    if (draft) setForm(structuredClone(draft.payload))
  }, [drafts, selected])

  const current = drafts.find(item => item.id === selected)
  const analysis = analyses.find(item => item.id === analysisId)
  const unresolved = analysis?.result.ambiguities?.some(item => item.critical && !item.decision)
  async function generate() {
    if (!analysisId) return
    setBusy(true); setError('')
    try {
      const result = await api<{ job_id: string }>(`${base}/analyses/${analysisId}/generate`, { method: 'POST' })
      setJob({ id: result.job_id, kind: 'cases', status: 'queued', attempts: 0, error: '', analysis_id: analysisId }); setNotice('生成任务已创建')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function retry() {
    if (!job) return
    setBusy(true); setError('')
    try { await api(`${base}/ai/jobs/${job.id}/retry`, { method: 'POST' }); await loadDrafts(); setNotice('任务已重新排队') }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
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
  function change(patch: Partial<DraftPayload>) { setForm(previous => previous ? { ...previous, ...patch } : previous) }
  return <div className="ai-workspace">
    {error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    <div className="authoring-toolbar"><label htmlFor="analysis-select">分析报告</label><select id="analysis-select" value={analysisId} onChange={event => setAnalysisId(event.target.value)}>
      {analyses.length ? analyses.map(item => <option value={item.id} key={item.id}>{new Date(item.created_at).toLocaleString('zh-CN')} · {item.result.requirements?.length ?? 0} 条需求</option>) : <option value="">暂无已完成的分析报告</option>}
    </select>{job && <Button variant="outline" onClick={() => setPromptOpen(true)} disabled={busy}><Eye data-icon="inline-start" />查看提示词</Button>}<Button onClick={() => void generate()} disabled={readonly || busy || !analysisId || Boolean(unresolved) || drafts.length > 0 || job?.status === 'running' || job?.status === 'queued'}><Plus data-icon="inline-start" />生成草稿</Button>
      {drafts.length > 0 && <><Button variant="outline" onClick={() => void exportFile('xlsx')} disabled={busy}><Download data-icon="inline-start" />Excel</Button><Button variant="outline" onClick={() => void exportFile('xmind')} disabled={busy}><Download data-icon="inline-start" />XMind</Button></>}</div>
    {unresolved && <div className="ai-alert warning"><AlertCircle size={16} />分析报告仍有关键歧义待确认</div>}
    {drafts.length > 0 && quality && <div className={`ai-alert ${quality.errors.length ? 'error' : quality.warnings.length ? 'warning' : ''}`} role="status">需求覆盖 {quality.coverage}% · {drafts.length} 条草稿{quality.errors.map(item => ` · ${item}`)}{quality.warnings.map(item => ` · ${item}`)}</div>}
    {job?.status === 'failed' && <div className="ai-alert error"><AlertCircle size={16} />{formatAiError(job.error)}{!readonly && <Button variant="outline" size="sm" onClick={() => void retry()} disabled={busy}><RefreshCw data-icon="inline-start" />重试</Button>}</div>}
    {(job?.status === 'queued' || job?.status === 'running') && <div className="ai-processing" role="status">正在生成用例草稿</div>}
    <div className="ai-columns authoring-columns"><aside className="ai-rail"><div className="ai-rail-heading"><h2>用例草稿</h2><Badge variant="secondary">{drafts.length}</Badge></div>
      {drafts.length ? drafts.map(item => <button className={`draft-row ${item.id === selected ? 'is-active' : ''}`} key={item.id} onClick={() => setSelected(item.id)}><strong>{item.payload.title}</strong><span>{item.payload.requirement_ids.join(', ')} · {item.payload.priority} · {item.status === 'published' ? '已发布' : '草稿'}</span></button>) : <div className="ai-empty">从已确认的分析报告生成草稿</div>}
    </aside><section className="ai-main"><AnimatePresence mode="wait">{current && form ? <motion.div key={current.id} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} className="draft-editor">
      <div className="ai-section-heading"><div><h2>编写用例</h2><p>来源需求 {form.requirement_ids.join(', ')}</p></div>{current.status === 'published' && <Button variant="outline" size="sm" onClick={onOpenCases}>查看用例库</Button>}</div>
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
    </motion.div> : <motion.div key="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="ai-empty main"><h2>选择一条用例草稿</h2></motion.div>}</AnimatePresence></section></div>
    <PromptViewerDialog projectId={projectId} jobId={job?.id ?? null} open={promptOpen} onOpenChange={setPromptOpen} />
  </div>
}
