import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { AlertCircle, ArrowRight, FileText, Plus, RefreshCw, Save, Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { api, type AiConfig, type AiJob, type Analysis, type SourceDocument } from '@/lib/api'

export function RequirementsPage({ projectId, readonly, onOpenAuthoring }: { projectId: number; readonly: boolean; onOpenAuthoring: () => void }) {
  const [documents, setDocuments] = useState<SourceDocument[]>([])
  const [analyses, setAnalyses] = useState<Analysis[]>([])
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [documentId, setDocumentId] = useState('')
  const [documentText, setDocumentText] = useState('')
  const [analysisId, setAnalysisId] = useState('')
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [jobs, setJobs] = useState<AiJob[]>([])
  const [model, setModel] = useState<Pick<AiConfig, 'local_model' | 'remote_enabled' | 'codex_enabled' | 'claude_enabled'> | null>(null)
  const [provider, setProvider] = useState('local')
  const [reportText, setReportText] = useState('')
  const [decisions, setDecisions] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const base = `/projects/${projectId}`

  const refresh = useCallback(async () => {
    const [docs, runs, config] = await Promise.all([
      api<SourceDocument[]>(`${base}/documents`), api<Analysis[]>(`${base}/analyses`),
      api<Pick<AiConfig, 'local_model' | 'remote_enabled' | 'codex_enabled' | 'claude_enabled'>>(`${base}/ai/config`),
    ])
    setDocuments(docs); setAnalyses(runs); setModel(config)
    setAnalysisId(previous => previous || runs[0]?.id || '')
  }, [base])

  useEffect(() => { void refresh().catch(cause => setError(cause.message)) }, [refresh])
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
      setReportText(run.result.report_markdown ?? '')
      setDecisions(Object.fromEntries((run.result.ambiguities ?? []).map(item => [item.id, item.decision ?? ''])))
      if (run.status === 'ready' || run.status === 'failed') window.clearInterval(interval)
    }
    void load().catch(cause => setError(cause.message))
    const interval = window.setInterval(() => {
      if (active) void load().catch(cause => setError(cause.message))
    }, 3000)
    return () => { active = false; window.clearInterval(interval) }
  }, [analysisId, base])

  async function uploadFiles(files: FileList | null) {
    if (!files?.length) return
    setBusy(true); setError(''); setNotice('')
    try {
      const ids: string[] = []
      for (const file of Array.from(files)) {
        const form = new FormData(); form.append('file', file)
        const result = await api<{ id: string }>(`${base}/documents`, { method: 'POST', body: form })
        ids.push(result.id)
      }
      await refresh(); setSelectedIds(ids); setDocumentId(ids.at(-1) ?? ''); setAnalysisId('')
      setNotice(`已上传 ${ids.length} 份文档，请核对提取内容`)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false); if (input.current) input.current.value = '' }
  }
  useEffect(() => {
    if (!documentId) return
    let active = true
    api<{ text: string }>(`${base}/documents/${documentId}`).then(result => { if (active) setDocumentText(result.text) }).catch(cause => setError(cause.message))
    return () => { active = false }
  }, [base, documentId])

  async function start() {
    if (!selectedIds.length) return
    if (provider === 'remote' && !window.confirm('远程模型会接收选中文档的正文。确认发送吗？')) return
    setBusy(true); setError('')
    try {
      const result = await api<{ analysis_id: string }>(`${base}/analyses`, { method: 'POST', body: JSON.stringify({ source_ids: selectedIds, provider }) })
      setAnalysisId(result.analysis_id); await refresh(); setNotice('分析任务已创建')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function retry(jobId: string) {
    setBusy(true); setError('')
    try { await api(`${base}/ai/jobs/${jobId}/retry`, { method: 'POST' }); setNotice('任务已重新排队') }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
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
  const currentJob = jobs.find(job => job.kind === 'analysis')
  return <div className="ai-workspace">
    {error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    <div className="ai-columns">
      <aside className="ai-rail">
        <div className="ai-rail-heading"><h2>需求材料</h2><Button variant="outline" size="sm" onClick={() => input.current?.click()} disabled={readonly || busy}><Plus data-icon="inline-start" />上传</Button></div>
        <input ref={input} type="file" multiple hidden accept=".md,.markdown,.txt,.png,.jpg,.jpeg,.webp,.pdf,.doc,.docx" onChange={event => void uploadFiles(event.target.files)} />
        {documents.length ? documents.map(doc => <div className="ai-document" key={doc.id}>
          <input type="checkbox" aria-label={`选择 ${doc.filename}`} checked={selectedIds.includes(doc.id)} onChange={event => setSelectedIds(previous => event.target.checked ? [...previous, doc.id] : previous.filter(id => id !== doc.id))} />
          <button className={documentId === doc.id && !analysisId ? 'is-active' : ''} onClick={() => { setDocumentId(doc.id); setAnalysisId('') }}><FileText size={15} /><span>{doc.filename}</span></button>
        </div>) : <div className="ai-empty"><Upload size={22} /><span>尚无需求材料</span></div>}
        <div className="ai-rail-heading ai-analysis-heading"><h2>分析记录</h2></div>
        {analyses.map(run => <button key={run.id} className={`ai-rail-row ${analysisId === run.id ? 'is-active' : ''}`} onClick={() => setAnalysisId(run.id)}>
          <span>{new Date(run.created_at).toLocaleString('zh-CN')}</span><Badge variant="secondary">{run.status === 'ready' ? '已完成' : run.status === 'failed' ? '失败' : '处理中'}</Badge>
        </button>)}
        <div className="ai-rail-footer"><label htmlFor="model-provider">分析模型</label><select id="model-provider" value={provider} onChange={event => setProvider(event.target.value)} disabled={readonly}>
          <option value="local">本地 {model?.local_model ?? 'Ollama'}</option>{model?.remote_enabled && <option value="remote">远程模型（发送正文）</option>}{model?.codex_enabled && <option value="codex">Codex CLI（本机）</option>}{model?.claude_enabled && <option value="claude">Claude Code（本机）</option>}
        </select><Button onClick={() => void start()} disabled={readonly || busy || !selectedIds.length}>开始分析</Button></div>
      </aside>
      <section className="ai-main">
        <AnimatePresence mode="wait">{analysis ? <motion.div key={analysis.id} initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -7 }} className="ai-report">
          <div className="ai-section-heading"><div><h2>分析报告</h2><p>{analysis.source_ids.length} 份材料 · {analysis.result.requirements?.length ?? 0} 条需求</p></div>
            {analysis.status === 'ready' && <Button variant="outline" size="sm" onClick={onOpenAuthoring}>进入用例编写<ArrowRight data-icon="inline-end" /></Button>}</div>
          {analysis.status === 'failed' ? <div className="ai-alert error"><AlertCircle size={16} />{analysis.error}{currentJob && !readonly && <Button variant="outline" size="sm" onClick={() => void retry(currentJob.id)} disabled={busy}><RefreshCw data-icon="inline-start" />重试</Button>}</div> : null}
          {analysis.status === 'queued' || analysis.status === 'running' ? <div className="ai-processing" role="status">正在处理需求材料，请稍候</div> : null}
          {analysis.status === 'ready' && <><div className="ai-section-heading"><h3>待澄清</h3><span>{analysis.result.ambiguities?.length ?? 0} 项</span></div>
            {(analysis.result.ambiguities ?? []).map(item => <div className="ambiguity-row" key={item.id}><div><strong>{item.id} {item.description}</strong>{item.critical && <Badge variant="destructive">关键</Badge>}</div>
              <div><Input value={decisions[item.id] ?? ''} placeholder="填写确认结论" onChange={event => setDecisions(previous => ({ ...previous, [item.id]: event.target.value }))} disabled={readonly} /><Button size="sm" variant="outline" onClick={() => void saveDecision(item.id)} disabled={readonly || busy || !(decisions[item.id] ?? '').trim()}>确认</Button></div></div>)}
            <div className="ai-section-heading report-edit-heading"><h3>报告正文</h3><Button size="sm" onClick={() => void saveReport()} disabled={readonly || busy || !reportText.trim()}><Save data-icon="inline-start" />保存</Button></div>
            <Textarea className="report-editor" value={reportText} onChange={event => setReportText(event.target.value)} readOnly={readonly} aria-label="分析报告正文" /></>}
        </motion.div> : <motion.div key={documentId || 'empty'} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="ai-report">
          {currentDoc ? <><div className="ai-section-heading"><div><h2>{currentDoc.filename}</h2><p>提取内容预览 · {currentDoc.text_length} 字符</p></div></div>
            {currentDoc.warnings.map(warning => <div className="ai-alert warning" key={warning}><AlertCircle size={16} />{warning}</div>)}
            <pre className="document-preview">{documentText}</pre></> : <div className="ai-empty main"><FileText size={26} /><h2>选择或上传需求材料</h2></div>}
        </motion.div>}</AnimatePresence>
      </section>
    </div>
  </div>
}
