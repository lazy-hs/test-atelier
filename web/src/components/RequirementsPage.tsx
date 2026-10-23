import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { AlertCircle, ArrowRight, Check, Eye, FileText, Loader2, PencilLine, Plus, RefreshCw, Save, Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { api, formatAiError, type AiConfig, type AiJob, type Analysis, type SourceDocument } from '@/lib/api'
import { PromptViewerDialog } from '@/components/PromptViewerDialog'

type UploadItem = { id: string; name: string; size: number; status: 'queued' | 'uploading' | 'done' | 'rejected' | 'failed' }

export function RequirementsPage({ projectId, readonly, onOpenAuthoring }: { projectId: number; readonly: boolean; onOpenAuthoring: () => void }) {
  const [documents, setDocuments] = useState<SourceDocument[]>([])
  const [analyses, setAnalyses] = useState<Analysis[]>([])
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [documentId, setDocumentId] = useState('')
  const [documentText, setDocumentText] = useState('')
  const [analysisId, setAnalysisId] = useState('')
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [jobs, setJobs] = useState<AiJob[]>([])
  const [model, setModel] = useState<Pick<AiConfig, 'remote_enabled' | 'codex_enabled' | 'claude_enabled' | 'max_document_bytes'> | null>(null)
  const [provider, setProvider] = useState('remote')
  const [reportText, setReportText] = useState('')
  const [decisions, setDecisions] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [promptOpen, setPromptOpen] = useState(false)
  const [reportMode, setReportMode] = useState<'preview' | 'edit'>('preview')
  const [uploadItems, setUploadItems] = useState<UploadItem[]>([])
  const reportRevision = useRef('')
  const input = useRef<HTMLInputElement>(null)
  const base = `/projects/${projectId}`

  const refresh = useCallback(async () => {
    const [docs, runs, config] = await Promise.all([
      api<SourceDocument[]>(`${base}/documents`), api<Analysis[]>(`${base}/analyses`),
      api<Pick<AiConfig, 'remote_enabled' | 'codex_enabled' | 'claude_enabled' | 'max_document_bytes'>>(`${base}/ai/config`),
    ])
    setDocuments(docs); setAnalyses(runs); setModel(config)
    setAnalysisId(previous => previous || runs[0]?.id || '')
  }, [base])

  useEffect(() => { void refresh().catch(cause => setError(cause.message)) }, [refresh])
  // Keep the controlled select aligned with the provider catalog after the
  // async config request.
  useEffect(() => {
    if (!model) return
    const enabled = [
      model.remote_enabled ? 'remote' : '',
      model.codex_enabled ? 'codex' : '',
      model.claude_enabled ? 'claude' : '',
    ].filter(Boolean)
    if (!enabled.includes(provider)) setProvider(enabled[0] ?? '')
  }, [model, provider])
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
      if (run.status === 'ready' || run.status === 'failed') window.clearInterval(interval)
    }
    void load().catch(cause => setError(cause.message))
    const interval = window.setInterval(() => {
      if (active) void load().catch(cause => setError(cause.message))
    }, 3000)
    return () => { active = false; window.clearInterval(interval) }
  }, [analysisId, base])

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
  const requirementCount = analysis?.result.requirements?.length ?? 0
  const ambiguityCount = analysis?.result.ambiguities?.length ?? 0
  const unresolvedCritical = analysis?.result.ambiguities?.filter(item => item.critical && !item.decision?.trim()).length ?? 0
  const mappedSourceCount = analysis ? new Set((analysis.result.requirements ?? []).flatMap(item => item.source_ids)).size : 0
  const providerLabel = analysis?.provider === 'codex' || analysis?.provider === 'codex_cli'
    ? 'Codex CLI 本机'
    : analysis?.provider === 'claude' || analysis?.provider === 'claude_code'
      ? 'Claude Code 本机'
      : analysis?.provider === 'remote' ? '云端模型' : 'AI 工具'
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
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    <div className="ai-columns">
      <aside className="ai-rail">
        <div className="ai-rail-heading"><div><h2>需求材料</h2><p className="ai-upload-limit-note">单文件最大 {uploadLimitLabel} · Codex 将分块读取大文档</p></div><Button variant="outline" size="sm" onClick={() => input.current?.click()} disabled={readonly || busy}><Plus data-icon="inline-start" />上传</Button></div>
        <input ref={input} type="file" multiple hidden accept=".md,.markdown,.txt,.png,.jpg,.jpeg,.webp,.pdf,.doc,.docx" onChange={event => void uploadFiles(event.target.files)} />
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
          <input type="checkbox" aria-label={`选择 ${doc.filename}`} checked={selectedIds.includes(doc.id)} onChange={event => setSelectedIds(previous => event.target.checked ? [...previous, doc.id] : previous.filter(id => id !== doc.id))} />
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
            return <motion.button key={run.id} className={`ai-rail-row ${analysisId === run.id ? 'is-active' : ''}`} onClick={() => setAnalysisId(run.id)}
              initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 6 }}
              transition={{ duration: .2, delay: Math.min(index * .035, .21) }} title={title}>
              <span className="ai-rail-row-copy"><strong>{title}</strong><small>{new Date(run.created_at).toLocaleString('zh-CN')}</small></span>
              <Badge variant="secondary">{run.status === 'ready' ? '已完成' : run.status === 'failed' ? '失败' : '处理中'}</Badge>
            </motion.button>
          })}
        </AnimatePresence>
        <div className="ai-rail-footer"><label htmlFor="model-provider">分析模型</label><select id="model-provider" value={provider} onChange={event => setProvider(event.target.value)} disabled={readonly || !model}>
          {model?.remote_enabled && <option value="remote">远程模型（发送正文）</option>}{model?.codex_enabled && <option value="codex">Codex CLI（本机）</option>}{model?.claude_enabled && <option value="claude">Claude Code（本机）</option>}
        </select><Button onClick={() => void start()} disabled={readonly || busy || !selectedIds.length || !model || !provider}>开始分析</Button></div>
      </aside>
      <section className="ai-main">
        <AnimatePresence mode="wait">{analysis ? <motion.div key={analysis.id} initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -7 }} className="ai-report">
          <div className="ai-section-heading"><div><h2>分析报告</h2><p>{analysis.source_ids.length} 份材料 · {analysis.result.requirements?.length ?? 0} 条需求</p></div>
            <div className="ai-heading-actions">{currentJob && <Button variant="outline" size="sm" onClick={() => setPromptOpen(true)}><Eye data-icon="inline-start" />查看提示词</Button>}{analysis.status === 'ready' && <Button variant="outline" size="sm" onClick={onOpenAuthoring}>进入用例编写<ArrowRight data-icon="inline-end" /></Button>}</div></div>
          <motion.div className="analysis-flow" aria-label="需求分析流程" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }} transition={{ delay: .06, duration: .22 }}>
            <div className="analysis-flow-head"><span>{providerLabel === 'Codex CLI 本机' ? 'Codex 需求分析流程' : '需求分析流程'}</span><Badge variant="secondary">{providerLabel}</Badge></div>
            <div className="analysis-flow-steps">{workflow.map((step, index) => <motion.div className={`analysis-flow-step ${step.done ? 'is-done' : ''} ${analysis.status !== 'ready' && index === 1 ? 'is-active' : ''}`} key={step.label} initial={{ opacity: 0, x: -5 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -5 }} transition={{ delay: .08 + index * .035, duration: .18 }}><span className="analysis-flow-dot" aria-hidden="true">{step.done ? <Check size={11} strokeWidth={2.5} /> : index + 1}</span><span>{step.label}</span>{index < workflow.length - 1 && <span className="analysis-flow-line" aria-hidden="true" />}</motion.div>)}</div>
          </motion.div>
          {analysis.status === 'ready' && <motion.div className="analysis-metrics" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }} transition={{ delay: .12, duration: .22 }}>
            <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }} transition={{ delay: .14, duration: .18 }}><strong>{requirementCount}</strong><span>条需求</span></motion.div><motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }} transition={{ delay: .17, duration: .18 }}><strong>{mappedSourceCount}/{analysis.source_ids.length}</strong><span>来源已映射</span></motion.div><motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }} transition={{ delay: .2, duration: .18 }}><strong>{ambiguityCount}</strong><span>待澄清</span></motion.div><motion.div className={unresolvedCritical ? 'is-warning' : ''} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }} transition={{ delay: .23, duration: .18 }}><strong>{unresolvedCritical}</strong><span>关键未确认</span></motion.div>
          </motion.div>}
          {analysis.status === 'failed' ? <div className="ai-alert error"><AlertCircle size={16} />{formatAiError(analysis.error)}{currentJob && !readonly && <Button variant="outline" size="sm" onClick={() => void retry(currentJob.id)} disabled={busy}><RefreshCw data-icon="inline-start" />重试</Button>}</div> : null}
          {analysis.status === 'queued' || analysis.status === 'running' ? <div className="ai-processing" role="status">正在处理需求材料，请稍候</div> : null}
          {analysis.status === 'ready' && <><div className="ai-section-heading"><h3>待澄清</h3><span>{analysis.result.ambiguities?.length ?? 0} 项</span></div>
            {(analysis.result.ambiguities ?? []).map(item => <div className="ambiguity-row" key={item.id}><div><strong>{item.id} {item.description}</strong>{item.critical && <Badge variant="destructive">关键</Badge>}</div>
              <div><Input value={decisions[item.id] ?? ''} placeholder="填写确认结论" onChange={event => setDecisions(previous => ({ ...previous, [item.id]: event.target.value }))} disabled={readonly} /><Button size="sm" variant="outline" onClick={() => void saveDecision(item.id)} disabled={readonly || busy || !(decisions[item.id] ?? '').trim()}>确认</Button></div></div>)}
            <div className="ai-section-heading report-edit-heading"><div><h3>报告正文</h3><p>支持 Markdown 预览与编辑</p></div><div className="report-heading-actions"><div className="report-mode-switch" role="tablist" aria-label="报告显示模式">
              <Button type="button" size="sm" variant={reportMode === 'preview' ? 'secondary' : 'ghost'} role="tab" aria-selected={reportMode === 'preview'} onClick={() => setReportMode('preview')}><Eye data-icon="inline-start" />预览</Button>
              <Button type="button" size="sm" variant={reportMode === 'edit' ? 'secondary' : 'ghost'} role="tab" aria-selected={reportMode === 'edit'} onClick={() => setReportMode('edit')}><PencilLine data-icon="inline-start" />编辑 Markdown</Button>
            </div><Button size="sm" onClick={() => void saveReport()} disabled={readonly || busy || !reportText.trim() || reportMode !== 'edit'}><Save data-icon="inline-start" />保存</Button></div></div>
            <AnimatePresence mode="wait" initial={false}>{reportMode === 'preview' ? <motion.article key="report-preview" className="report-markdown" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }} transition={{ duration: .16 }}>
              {reportText.trim() ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{reportText}</ReactMarkdown> : <p className="report-markdown-empty">暂无报告正文</p>}
            </motion.article> : <motion.div key="report-editor" className="report-editor-frame" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }} transition={{ duration: .16 }}><Textarea className="report-editor" value={reportText} onChange={event => setReportText(event.target.value)} readOnly={readonly} aria-label="分析报告正文 Markdown" /></motion.div>}</AnimatePresence></>}
        </motion.div> : <motion.div key={documentId || 'empty'} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="ai-report">
          {currentDoc ? <><div className="ai-section-heading"><div><h2>{currentDoc.filename}</h2><p>提取内容预览 · {currentDoc.text_length} 字符</p></div></div>
            {currentDoc.warnings.map(warning => <div className="ai-alert warning" key={warning}><AlertCircle size={16} />{warning}</div>)}
            <pre className="document-preview">{documentText}</pre></> : <div className="ai-empty main"><FileText size={26} /><h2>选择或上传需求材料</h2></div>}
        </motion.div>}</AnimatePresence>
      </section>
    </div>
    <PromptViewerDialog projectId={projectId} jobId={currentJob?.id ?? null} open={promptOpen} onOpenChange={setPromptOpen} />
  </div>
}
