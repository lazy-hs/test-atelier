import { useEffect, useState, type FormEvent } from 'react'
import { AlertCircle, FileWarning, Plus, Save } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { api, type DefectCandidates, type DefectEvent, type ExecutionDefect } from '@/lib/api'

const severityLabels = { low: '低', medium: '中', high: '高', critical: '严重' } as const
const statusLabels = { open: '待处理', in_progress: '处理中', resolved: '已解决', closed: '已关闭' } as const
type Severity = ExecutionDefect['severity']
type Status = ExecutionDefect['status']

export function ExecutionDefectsDialog({ projectId, runId, readonly, onClose }: {
  projectId: number; runId: number; readonly: boolean; onClose: () => void
}) {
  const base = `/projects/${projectId}/execution-defects`
  const [candidates, setCandidates] = useState<DefectCandidates | null>(null)
  const [items, setItems] = useState<ExecutionDefect[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [events, setEvents] = useState<DefectEvent[]>([])
  const [caseId, setCaseId] = useState('')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [severity, setSeverity] = useState<Severity>('medium')
  const [status, setStatus] = useState<Status>('open')
  const [resolution, setResolution] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const selected = items.find(item => item.id === selectedId) ?? null

  useEffect(() => {
    let active = true
    void Promise.all([
      api<DefectCandidates>(`${base}/candidates?run_id=${runId}`),
      api<ExecutionDefect[]>(`${base}?run_id=${runId}`),
    ]).then(([source, defects]) => { if (active) {
      setCandidates(source); setItems(defects); setCaseId(String(source.items[0]?.run_case_id ?? ''))
    } }).catch(cause => { if (active) setError(cause.message) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [base, runId])
  useEffect(() => {
    if (!selectedId) return
    let active = true
    void api<DefectEvent[]>(`${base}/${selectedId}/events`).then(value => { if (active) setEvents(value) })
      .catch(cause => { if (active) setError(cause.message) })
    return () => { active = false }
  }, [base, selectedId, selected?.version])

  function choose(item: ExecutionDefect | null) {
    setSelectedId(item?.id ?? null); setEvents([]); setError(''); setNotice('')
    setCaseId(String(item?.run_case_id ?? candidates?.items[0]?.run_case_id ?? ''))
    setTitle(item?.title ?? ''); setDescription(item?.description ?? '')
    setSeverity(item?.severity ?? 'medium'); setStatus(item?.status ?? 'open')
    setResolution(item?.resolution ?? '')
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (readonly || (!selected && !caseId)) return
    setBusy(true); setError(''); setNotice('')
    try {
      const next = selected
        ? await api<ExecutionDefect>(`${base}/${selected.id}`, { method: 'PATCH', body: JSON.stringify({
            version: selected.version, title: title.trim(), description: description.trim(), severity, status,
            resolution: resolution.trim(),
          }) })
        : await api<ExecutionDefect>(base, { method: 'POST', body: JSON.stringify({
            run_id: runId, run_case_id: Number(caseId), title: title.trim(), description: description.trim(), severity,
          }) })
      setItems(previous => selected ? previous.map(item => item.id === next.id ? next : item) : [next, ...previous])
      choose(next); setNotice(selected ? '缺陷已更新' : '缺陷已关联冻结的执行用例')
    } catch (cause) {
      setError((cause as Error).message)
      if (selected && (cause as Error & { status?: number }).status === 409) {
        try {
          const fresh = await api<ExecutionDefect[]>(`${base}?run_id=${runId}`)
          setItems(fresh)
          choose(fresh.find(item => item.id === selected?.id) ?? null)
          setError('缺陷已被其他成员修改，已载入最新记录，请核对后重新编辑')
        } catch { /* Preserve the conflict message until the next retry. */ }
      }
    } finally { setBusy(false) }
  }

  return <Dialog open onOpenChange={open => { if (!open && !busy) onClose() }}><DialogContent className="execution-defects-dialog"><DialogHeader><DialogTitle>执行缺陷 · #{runId}</DialogTitle><DialogDescription>缺陷独立于执行报告 不会改写已冻结结果</DialogDescription></DialogHeader>
    {error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    {candidates?.report_completeness === 'legacy_unverified' && <div className="ai-alert warning"><AlertCircle size={16} />旧报告未能证明执行用例总数 已关联的单条记录仍可核对</div>}
    {loading ? <p className="execution-defects-empty" role="status">正在核对执行快照</p> : candidates === null ? <p className="execution-defects-empty">无法读取报告 请核对执行 ID 后重试</p> : <div className="execution-defects-body"><div className="execution-defects-list"><div className="execution-defects-list-heading"><strong>关联记录 · {items.length}</strong>{!readonly && candidates.items.length ? <Button size="icon-sm" variant="ghost" aria-label="新建缺陷" title="新建缺陷" onClick={() => choose(null)}><Plus size={16} /></Button> : null}</div>
      {items.map(item => <button className={selectedId === item.id ? 'is-selected' : ''} key={item.id} onClick={() => choose(item)}><FileWarning size={16} /><span><strong>{item.title}</strong><small>{item.source_original_id} · {statusLabels[item.status]} · {severityLabels[item.severity]}</small></span></button>)}
      {!items.length && <p className="execution-defects-empty">尚无关联缺陷</p>}
    </div><div className="execution-defects-editor">{selected || (!readonly && candidates.items.length) ? <form onSubmit={event => void save(event)}><strong>{selected ? `缺陷 #${selected.id} · 版本 ${selected.version}` : '登记缺陷'}</strong>
      <label>来源执行用例<Select value={caseId} onValueChange={setCaseId} disabled={!!selected || readonly}><SelectTrigger aria-label="来源执行用例"><SelectValue placeholder="选择失败或阻塞的用例" /></SelectTrigger><SelectContent><SelectGroup>{candidates.items.map(item => <SelectItem key={item.run_case_id} value={String(item.run_case_id)}>{item.original_id} · {item.title} · {item.result === 'failed' ? '失败' : '阻塞'}</SelectItem>)}</SelectGroup></SelectContent></Select></label>
      <label>缺陷标题<Input value={title} onChange={event => setTitle(event.target.value)} maxLength={160} disabled={readonly} required /></label>
      <label>问题与复现说明<Textarea value={description} onChange={event => setDescription(event.target.value)} maxLength={5000} disabled={readonly} /></label>
      <div className="execution-defects-pair"><label>严重程度<Select value={severity} onValueChange={value => setSeverity(value as Severity)} disabled={readonly}><SelectTrigger aria-label="严重程度"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{Object.entries(severityLabels).map(([key, value]) => <SelectItem value={key} key={key}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select></label>
      {selected && <label>处理状态<Select value={status} onValueChange={value => setStatus(value as Status)} disabled={readonly}><SelectTrigger aria-label="处理状态"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{Object.entries(statusLabels).map(([key, value]) => <SelectItem value={key} key={key}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select></label>}</div>
      {selected && <label>处理结论<Textarea value={resolution} onChange={event => setResolution(event.target.value)} maxLength={3000} disabled={readonly} placeholder="已解决或关闭时必填" /></label>}
      {!readonly && <Button size="sm" type="submit" disabled={busy || !title.trim() || !caseId || (selected !== null && (status === 'resolved' || status === 'closed') && !resolution.trim())}><Save data-icon="inline-start" />{busy ? '保存中' : selected ? '保存修改' : '关联缺陷'}</Button>}
    </form> : <p className="execution-defects-empty">{candidates?.items.length ? '选择左侧缺陷查看记录' : '本次执行没有失败或阻塞用例'}</p>}
    {selected && <div className="execution-defects-events"><strong>审计记录 · {events.length}</strong>{events.map(item => <p key={item.id}>账号 #{item.actor_id} · {new Date(item.created_at).toLocaleString('zh-CN')} · {Object.keys(item.before).length === 0 ? '创建缺陷' : item.before.status !== item.after.status ? `${statusLabels[item.before.status!]} → ${statusLabels[item.after.status!]}` : '更新缺陷内容'}</p>)}</div>}
    </div></div>}
  </DialogContent></Dialog>
}
