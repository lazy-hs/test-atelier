import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { AlertCircle, Check, Clock3, Pencil, Play, Plus, RefreshCw, Save, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { api, type ExecutionRun, type RunCase, type RunResult, type SmokeCandidate, type SmokeItem, type SmokeSuite } from '@/lib/api'

const resultLabels: Record<RunResult, string> = {
  not_run: '未执行', passed: '通过', failed: '失败', blocked: '阻塞', skipped: '跳过',
}
function minutes(seconds: number | null) { return seconds === null ? '—' : `${(seconds / 60).toFixed(1)} 分钟` }
function date(value: string | null) { return value ? new Date(value).toLocaleString('zh-CN') : '—' }
function chainsFrom(value: string) { return value.split(/[\n,，]/).map(item => item.trim()).filter(Boolean) }

function SuiteForm({ current, onClose, onSave }: {
  current: SmokeSuite | null; onClose: () => void
  onSave: (value: { title: string; target_version: string; environment: string; required_chains: string[] }) => Promise<void>
}) {
  const [title, setTitle] = useState(current?.title ?? '')
  const [targetVersion, setTargetVersion] = useState(current?.target_version ?? '')
  const [environment, setEnvironment] = useState(current?.environment ?? '')
  const [chains, setChains] = useState(current?.required_chains.join('\n') ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    try { await onSave({ title: title.trim(), target_version: targetVersion.trim(), environment: environment.trim(), required_chains: chainsFrom(chains) }) }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  return <Dialog open onOpenChange={value => { if (!value) onClose() }}><DialogContent><DialogHeader><DialogTitle>{current ? '编辑冒烟集' : '新建冒烟集'}</DialogTitle><DialogDescription>按版本和环境组织核心链路，目标时长固定为 30 分钟</DialogDescription></DialogHeader>
    <form className="smoke-form" onSubmit={event => void submit(event)}>
      <label>名称<Input value={title} onChange={event => setTitle(event.target.value)} maxLength={120} required /></label>
      <div className="smoke-form-pair"><label>目标版本<Input value={targetVersion} onChange={event => setTargetVersion(event.target.value)} maxLength={80} required /></label><label>环境<Input value={environment} onChange={event => setEnvironment(event.target.value)} maxLength={80} required /></label></div>
      <label>核心链路<Textarea value={chains} onChange={event => setChains(event.target.value)} placeholder="每行一条链路" /></label>
      {error && <p className="error-line" role="alert">{error}</p>}
      <DialogFooter><Button type="submit" disabled={busy}>{busy ? '保存中…' : current ? '保存修改' : '创建冒烟集'}</Button></DialogFooter>
    </form>
  </DialogContent></Dialog>
}

function SmokeItemRow({ item, suite, readonly, onSave, onRemove }: {
  item: SmokeItem; suite: SmokeSuite; readonly: boolean
  onSave: (chain: string, seconds: number) => Promise<void>; onRemove: () => Promise<void>
}) {
  const [chain, setChain] = useState(item.chain_name)
  const [duration, setDuration] = useState(String(item.estimated_seconds / 60))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const changed = item.stale || chain !== item.chain_name || Math.round(Number(duration) * 60) !== item.estimated_seconds
  async function save() {
    setBusy(true); setError('')
    try { await onSave(chain, Math.round(Number(duration) * 60)) }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function remove() {
    if (!window.confirm(`从冒烟集中移除 ${item.original_id}？历史执行记录不受影响。`)) return
    setBusy(true); setError('')
    try { await onRemove() } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  return <div className="smoke-item-row"><div className="smoke-item-title"><strong>{item.title}</strong><small>{item.original_id} · 等级 {item.level || '待判定'}</small>{item.stale && <span className="pending-text">需重新确认</span>}</div>
    {readonly ? <><span>{item.chain_name}</span><span>{minutes(item.estimated_seconds)}</span></> : <><Select value={chain} onValueChange={setChain}><SelectTrigger aria-label={`${item.original_id} 核心链路`}><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{suite.required_chains.map(value => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select>
      <Input aria-label={`${item.original_id} 预计分钟`} type="number" min="0.5" max="240" step="0.5" value={duration} onChange={event => setDuration(event.target.value)} /></>}
    <div className="smoke-item-actions">{!readonly && <><Button variant="ghost" size="icon-sm" title={item.stale ? '重新确认' : '保存时长与链路'} aria-label={`保存 ${item.original_id}`} disabled={busy || !changed || !Number(duration) || Number(duration) < .5} onClick={() => void save()}><Save /></Button><Button variant="ghost" size="icon-sm" title="移出冒烟集" aria-label={`移除 ${item.original_id}`} disabled={busy} onClick={() => void remove()}><Trash2 /></Button></>}</div>
    {error && <p className="error-line smoke-row-error" role="alert">{error}</p>}
  </div>
}

function RunCaseRow({ item, active, readonly, onSave }: {
  item: RunCase; active: boolean; readonly: boolean
  onSave: (result: RunResult, seconds: number | null, note: string) => Promise<void>
}) {
  const [result, setResult] = useState<RunResult>(item.result)
  const [duration, setDuration] = useState(item.actual_seconds === null ? '' : String(item.actual_seconds / 60))
  const [note, setNote] = useState(item.note)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const needsNote = ['failed', 'blocked', 'skipped'].includes(result)
  const seconds = duration === '' ? null : Math.round(Number(duration) * 60)
  const changed = result !== item.result || seconds !== item.actual_seconds || note.trim() !== item.note
  async function save() {
    setBusy(true); setError('')
    try { await onSave(result, seconds, note.trim()) }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  return <div className="run-case-row"><div className="run-case-title"><strong>{item.title}</strong><small>{item.original_id} · {item.chain_name} · 预计 {minutes(item.estimated_seconds)}</small></div>
    {active && !readonly ? <><Select value={result} onValueChange={value => setResult(value as RunResult)}><SelectTrigger aria-label={`${item.original_id} 执行结果`}><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{Object.entries(resultLabels).map(([key, label]) => <SelectItem key={key} value={key}>{label}</SelectItem>)}</SelectGroup></SelectContent></Select>
      <Input aria-label={`${item.original_id} 实际分钟`} type="number" min="0" max="240" step="0.5" value={duration} onChange={event => setDuration(event.target.value)} placeholder="分钟" /></> : <><span>{resultLabels[item.result]}</span><span>{minutes(item.actual_seconds)}</span></>}
    {active && !readonly && <><Input aria-label={`${item.original_id} 执行说明`} value={note} onChange={event => setNote(event.target.value)} maxLength={2000} placeholder={needsNote ? '必填说明' : '备注'} /><Button variant="ghost" size="icon-sm" aria-label={`保存 ${item.original_id} 执行结果`} title="保存执行结果" disabled={busy || !changed || (needsNote && !note.trim()) || (duration !== '' && (!Number.isFinite(Number(duration)) || Number(duration) < 0))} onClick={() => void save()}><Save /></Button></>}
    {(!active || readonly) && <small className="run-case-note">{item.note || item.executed_by || '—'}</small>}
    {error && <p className="error-line smoke-row-error" role="alert">{error}</p>}
  </div>
}

export function SmokePage({ projectId, readonly, onOpenCases }: { projectId: number; readonly: boolean; onOpenCases: () => void }) {
  const base = `/projects/${projectId}`
  const [tab, setTab] = useState('suites')
  const [suites, setSuites] = useState<SmokeSuite[]>([])
  const [runs, setRuns] = useState<ExecutionRun[]>([])
  const [suiteId, setSuiteId] = useState<number | null>(null)
  const [runId, setRunId] = useState<number | null>(null)
  const [run, setRun] = useState<ExecutionRun | null>(null)
  const [form, setForm] = useState<'new' | 'edit' | null>(null)
  const [query, setQuery] = useState('')
  const [candidateId, setCandidateId] = useState('')
  const [candidates, setCandidates] = useState<SmokeCandidate[]>([])
  const [chain, setChain] = useState('')
  const [duration, setDuration] = useState('2')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const suite = suites.find(item => item.id === suiteId) ?? null

  const refresh = useCallback(async () => {
    const [suiteRows, runRows] = await Promise.all([
      api<SmokeSuite[]>(`${base}/smoke-suites`), api<ExecutionRun[]>(`${base}/execution-runs`),
    ])
    setSuites(suiteRows); setRuns(runRows)
    setSuiteId(previous => previous && suiteRows.some(item => item.id === previous) ? previous : suiteRows[0]?.id ?? null)
    setRunId(previous => previous && runRows.some(item => item.id === previous) ? previous : runRows[0]?.id ?? null)
  }, [base])
  useEffect(() => { void refresh().catch(cause => setError(cause.message)) }, [refresh])
  useEffect(() => {
    if (!runId) { setRun(null); return }
    let active = true
    void api<ExecutionRun>(`${base}/execution-runs/${runId}`).then(value => { if (active) setRun(value) })
      .catch(cause => { if (active) setError(cause.message) })
    return () => { active = false }
  }, [base, runId])
  useEffect(() => {
    if (!suiteId) { setCandidates([]); return }
    let active = true
    const timer = window.setTimeout(() => {
      void api<SmokeCandidate[]>(`${base}/smoke-suites/${suiteId}/candidates?q=${encodeURIComponent(query)}`)
        .then(value => { if (active) setCandidates(value) }).catch(cause => { if (active) setError(cause.message) })
    }, 220)
    return () => { active = false; window.clearTimeout(timer) }
  }, [base, suiteId, suite?.revision, query])
  useEffect(() => { if (!notice) return; const timer = window.setTimeout(() => setNotice(''), 4500); return () => window.clearTimeout(timer) }, [notice])

  function updateSuite(next: SmokeSuite) { setSuites(previous => previous.map(item => item.id === next.id ? next : item)) }
  async function saveSuite(value: { title: string; target_version: string; environment: string; required_chains: string[] }) {
    const next = form === 'new' ? await api<SmokeSuite>(`${base}/smoke-suites`, { method: 'POST', body: JSON.stringify(value) }) :
      await api<SmokeSuite>(`${base}/smoke-suites/${suite!.id}`, { method: 'PATCH', body: JSON.stringify({ ...value, revision: suite!.revision }) })
    if (form === 'new') { setSuites(previous => [next, ...previous]); setSuiteId(next.id) } else updateSuite(next)
    setForm(null); setNotice(form === 'new' ? '冒烟集已创建' : '冒烟集已更新')
  }
  async function addItem() {
    if (!suite || !candidateId) return
    setBusy(true); setError('')
    try {
      const next = await api<SmokeSuite>(`${base}/smoke-suites/${suite.id}/items`, { method: 'POST', body: JSON.stringify({
        revision: suite.revision, case_id: Number(candidateId), chain_name: chain, estimated_seconds: Math.round(Number(duration) * 60),
      }) })
      updateSuite(next); setCandidateId(''); setQuery(''); setNotice('用例已由人工确认并加入冒烟集')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function updateItem(item: SmokeItem, nextChain: string, seconds: number) {
    if (!suite) return
    const next = await api<SmokeSuite>(`${base}/smoke-suites/${suite.id}/items/${item.id}`, { method: 'PUT', body: JSON.stringify({
      revision: suite.revision, chain_name: nextChain, estimated_seconds: seconds,
    }) })
    updateSuite(next); setNotice('链路与时长已重新确认')
  }
  async function removeItem(item: SmokeItem) {
    if (!suite) return
    const next = await api<SmokeSuite>(`${base}/smoke-suites/${suite.id}/items/${item.id}?revision=${suite.revision}`, { method: 'DELETE' })
    updateSuite(next); setNotice('用例已从冒烟集移出')
  }
  async function startRun() {
    if (!suite) return
    if ((suite.over_budget || suite.uncovered_chains.length) && !window.confirm('当前冒烟集超时或存在未覆盖链路，仍要开始本次执行吗？')) return
    setBusy(true); setError('')
    try {
      const created = await api<ExecutionRun>(`${base}/smoke-suites/${suite.id}/runs`, { method: 'POST', body: JSON.stringify({ suite_revision: suite.revision }) })
      setRuns(previous => [{ ...created, items: [], events: [] }, ...previous]); setRunId(created.id); setRun(created); setTab('runs'); setNotice('执行记录已创建')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function saveResult(item: RunCase, result: RunResult, seconds: number | null, note: string) {
    if (!run) return
    const updated = await api<ExecutionRun>(`${base}/execution-runs/${run.id}/cases/${item.id}`, { method: 'PUT', body: JSON.stringify({
      version: item.version, result, actual_seconds: seconds, note,
    }) })
    setRun(updated); setRuns(previous => previous.map(value => value.id === updated.id ? { ...updated, items: [], events: [] } : value))
    setNotice('执行结果已保存')
  }
  async function completeRun() {
    if (!run) return
    if (!window.confirm('结束本次执行？结束后结果不可再修改。')) return
    setBusy(true); setError('')
    try {
      const completed = await api<ExecutionRun>(`${base}/execution-runs/${run.id}/complete`, { method: 'POST' })
      setRun(completed); setRuns(previous => previous.map(value => value.id === completed.id ? { ...completed, items: [], events: [] } : value)); setNotice('执行已结束')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  function chooseSuite(value: string) { setSuiteId(Number(value)); setCandidateId(''); setQuery(''); setChain('') }
  const ready = suite && suite.items.length > 0 && !suite.items.some(item => item.stale) && suite.required_chains.length > 0

  return <div className="smoke-page">
    {error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    <Tabs value={tab} onValueChange={setTab} className="smoke-tabs"><TabsList variant="line" aria-label="冒烟工作视图"><TabsTrigger value="suites">冒烟集</TabsTrigger><TabsTrigger value="runs">执行记录</TabsTrigger></TabsList>
      <TabsContent value="suites"><div className="smoke-section-heading"><div><h2>冒烟集</h2><p>目标 30 分钟 · 由人工确认用例和预计时长</p></div><div className="smoke-actions">
        {suites.length > 0 && <Select value={String(suiteId ?? '')} onValueChange={chooseSuite}><SelectTrigger aria-label="选择冒烟集"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{suites.map(value => <SelectItem key={value.id} value={String(value.id)}>{value.title} · {value.target_version}</SelectItem>)}</SelectGroup></SelectContent></Select>}
        {!readonly && <Button size="sm" onClick={() => setForm('new')}><Plus data-icon="inline-start" />新建冒烟集</Button>}
        <Button variant="ghost" size="icon-sm" aria-label="刷新冒烟数据" title="刷新冒烟数据" onClick={() => void refresh().catch(cause => setError(cause.message))}><RefreshCw /></Button>
      </div></div>
      {!suite && <div className="smoke-empty">当前项目还没有冒烟集</div>}
      {suite && <><div className="smoke-suite-meta"><strong>{suite.title}</strong><span>{suite.target_version} · {suite.environment} · 修订 {suite.revision}</span>{!readonly && <Button variant="ghost" size="icon-sm" aria-label="编辑冒烟集" title="编辑冒烟集" onClick={() => setForm('edit')}><Pencil /></Button>}</div>
        <div className="smoke-summary"><div><span>预计时长</span><strong>{minutes(suite.estimated_seconds)} / 30 分钟</strong></div><div><span>核心链路覆盖</span><strong>{suite.covered_count} / {suite.required_count}</strong></div><div><span>已确认用例</span><strong>{suite.items.length}</strong></div></div>
        <div className={`smoke-budget-track ${suite.over_budget ? 'is-over' : ''}`}><span style={{ width: `${Math.min(100, suite.estimated_seconds / suite.budget_seconds * 100)}%` }} /></div>
        {suite.over_budget && <div className="ai-alert warning"><Clock3 size={16} />预计超过 30 分钟，请人工调整；用例不会自动移除</div>}
        {suite.uncovered_chains.length > 0 && <div className="ai-alert warning"><AlertCircle size={16} />未覆盖链路：{suite.uncovered_chains.join('、')}</div>}
        {suite.items.some(item => item.stale) && <div className="ai-alert error"><AlertCircle size={16} />有用例审核或分派已变化，请重新确认后再执行</div>}
        <div className="smoke-section-heading"><div><h2>已选用例</h2><p>{suite.items.length} 条</p></div>{!readonly && <Button onClick={() => void startRun()} disabled={busy || !ready}><Play data-icon="inline-start" />开始执行</Button>}</div>
        <div className="smoke-item-head"><span>用例</span><span>核心链路</span><span>预计分钟</span><span>操作</span></div>
        {suite.items.map(item => <SmokeItemRow key={`${item.id}-${item.confirmed_at}`} item={item} suite={suite} readonly={readonly}
          onSave={(nextChain, seconds) => updateItem(item, nextChain, seconds)} onRemove={() => removeItem(item)} />)}
        {suite.items.length === 0 && <div className="smoke-empty">尚无确认的冒烟用例</div>}
        {!readonly && <section className="smoke-add"><div className="smoke-section-heading"><div><h2>加入用例</h2><p>仅显示最终冒烟已由评审员确认为“是”的用例</p></div><Button variant="ghost" size="sm" onClick={onOpenCases}>返回用例库</Button></div>
          <div className="smoke-add-fields"><Input aria-label="搜索冒烟候选" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索用例 ID 名称或目录" />
            <Select value={candidateId} onValueChange={setCandidateId} disabled={!candidates.length}><SelectTrigger aria-label="选择冒烟用例"><SelectValue placeholder="选择已审核用例" /></SelectTrigger><SelectContent><SelectGroup>{candidates.map(value => <SelectItem key={value.id} value={String(value.id)}>{value.original_id} · {value.title}</SelectItem>)}</SelectGroup></SelectContent></Select>
            <Select value={chain} onValueChange={setChain} disabled={!suite.required_chains.length}><SelectTrigger aria-label="选择核心链路"><SelectValue placeholder="核心链路" /></SelectTrigger><SelectContent><SelectGroup>{suite.required_chains.map(value => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select>
            <Input aria-label="预计执行分钟" type="number" min="0.5" max="240" step="0.5" value={duration} onChange={event => setDuration(event.target.value)} />
            <Button disabled={busy || !candidateId || !chain || !Number(duration) || Number(duration) < .5} onClick={() => void addItem()}><Check data-icon="inline-start" />确认加入</Button></div>
          {candidates.length === 0 && <p className="smoke-empty">没有可加入的已审核冒烟用例</p>}
        </section>}
      </>}
      </TabsContent>
      <TabsContent value="runs"><div className="smoke-section-heading"><div><h2>执行记录</h2><p>每次执行冻结版本、环境和用例内容</p></div><Button variant="ghost" size="icon-sm" aria-label="刷新执行记录" title="刷新执行记录" onClick={() => void refresh().catch(cause => setError(cause.message))}><RefreshCw /></Button></div>
        <div className="smoke-run-list">{runs.map(value => <button key={value.id} className={runId === value.id ? 'active' : ''} onClick={() => setRunId(value.id)}><strong>{value.title}</strong><span>{value.target_version} · {value.environment}</span><small>{value.status === 'active' ? '执行中' : '已完成'} · {date(value.started_at)}</small></button>)}</div>
        {runs.length === 0 && <div className="smoke-empty">还没有执行记录</div>}
        {run && <div className="smoke-run-detail"><div className="smoke-suite-meta"><strong>{run.title}</strong><span>{run.target_version} · {run.environment} · 来源冒烟集修订 {run.suite_revision}</span></div>
          <div className="smoke-summary"><div><span>预计时长</span><strong>{minutes(run.estimated_seconds)}</strong></div><div><span>实际时长</span><strong>{minutes(run.actual_seconds)}</strong></div><div><span>执行进度</span><strong>{run.item_count - run.summary.not_run} / {run.item_count}</strong></div></div>
          {run.over_budget && <div className="ai-alert warning"><Clock3 size={16} />本次快照预计超过 30 分钟</div>}
          {run.uncovered_chains.length > 0 && <div className="ai-alert warning"><AlertCircle size={16} />本次快照未覆盖：{run.uncovered_chains.join('、')}</div>}
          <div className="smoke-result-counts">{Object.entries(resultLabels).map(([key, label]) => <span key={key}>{label} <strong>{run.summary[key as RunResult]}</strong></span>)}</div>
          <div className="smoke-section-heading"><h2>执行用例</h2>{!readonly && run.status === 'active' && <Button size="sm" disabled={busy || run.summary.not_run > 0} onClick={() => void completeRun()}><Check data-icon="inline-start" />结束执行</Button>}</div>
          <div className="run-case-head"><span>用例</span><span>结果</span><span>实际分钟</span><span>说明</span></div>
          {run.items.map(item => <RunCaseRow key={`${item.id}-${item.version}`} item={item} active={run.status === 'active'} readonly={readonly}
            onSave={(result, seconds, note) => saveResult(item, result, seconds, note)} />)}
          {run.status === 'completed' && <p className="smoke-run-complete">由 {run.completed_by} 于 {date(run.completed_at)} 结束</p>}
          <div className="smoke-section-heading smoke-event-heading"><h2>变更记录</h2><span>{run.events.length} 条</span></div>
          {run.events.map(event => <div className="smoke-event" key={event.id}><strong>{event.actor}</strong><span>{resultLabels[event.before.result]} → {resultLabels[event.after.result]} · {event.after.note || '—'}</span><time>{date(event.created_at)}</time></div>)}
        </div>}
      </TabsContent>
    </Tabs>
    {form && <SuiteForm key={`${form}-${suite?.id ?? 'new'}`} current={form === 'edit' ? suite : null} onClose={() => setForm(null)} onSave={saveSuite} />}
  </div>
}
