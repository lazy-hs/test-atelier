import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, Check, RefreshCw, Sparkles, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { api, type AiConfig, type Case, type CaseSuggestion, type DimensionKey, type SuggestionJob, type TaxonomyVersion } from '@/lib/api'

const dimensions: { key: DimensionKey; label: string }[] = [
  { key: 'business_module', label: '业务模块' }, { key: 'business_scenario', label: '业务场景' },
  { key: 'test_dimension', label: '测试维度' }, { key: 'endpoint_type', label: '端类型' },
]
type Choice = { categories: Record<DimensionKey, string | null>; level: string | null; smoke: string | null }

export function SuggestionPanel({ projectId, item, canDecide, refreshSignal, onApplied }: {
  projectId: number; item: Case; canDecide: boolean; refreshSignal: number; onApplied: (value: Case) => Promise<void>
}) {
  const base = `/projects/${projectId}`
  const [suggestions, setSuggestions] = useState<CaseSuggestion[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [version, setVersion] = useState<TaxonomyVersion | null>(null)
  const [choice, setChoice] = useState<Choice | null>(null)
  const [reason, setReason] = useState('')
  const [provider, setProvider] = useState('remote')
  const [remoteEnabled, setRemoteEnabled] = useState(false)
  const [codexEnabled, setCodexEnabled] = useState(false)
  const [claudeEnabled, setClaudeEnabled] = useState(false)
  const [job, setJob] = useState<SuggestionJob | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    const rows = await api<CaseSuggestion[]>(`${base}/cases/${item.id}/suggestions`)
    setSuggestions(rows)
    setSelectedId(previous => previous && rows.some(row => row.id === previous) ? previous : rows[0]?.id ?? null)
  }, [base, item.id])
  useEffect(() => {
    let active = true
    void Promise.all([load(), api<Pick<AiConfig, 'local_enabled' | 'remote_enabled' | 'codex_enabled' | 'claude_enabled'>>(`${base}/ai/config`),
      api<SuggestionJob[]>(`${base}/suggestion-jobs`)]).then(([, config, jobs]) => {
        if (active) {
          setRemoteEnabled(config.remote_enabled); setCodexEnabled(Boolean(config.codex_enabled)); setClaudeEnabled(Boolean(config.claude_enabled))
          setProvider(current => current === 'local' ? (config.remote_enabled ? 'remote' : config.codex_enabled ? 'codex' : config.claude_enabled ? 'claude' : 'remote') : current)
          setJob(jobs.find(value => value.case_ids.includes(item.id) && (value.status === 'queued' || value.status === 'running' || value.status === 'failed' || value.status === 'partial')) ?? null)
        }
      }).catch(cause => { if (active) setError(cause.message) })
    return () => { active = false }
  }, [base, item.id, load, refreshSignal])
  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.status)) return
    const timer = window.setInterval(() => {
      void api<SuggestionJob>(`${base}/suggestion-jobs/${job.id}`).then(value => {
        setJob(value)
        if (!['queued', 'running'].includes(value.status)) void load().catch(cause => setError(cause.message))
      }).catch(cause => setError(cause.message))
    }, 2200)
    return () => window.clearInterval(timer)
  }, [base, job, load])
  const selected = suggestions.find(value => value.id === selectedId) ?? null
  useEffect(() => {
    if (!selected) { setChoice(null); setVersion(null); return }
    setChoice({ categories: { ...selected.categories }, level: selected.level, smoke: selected.smoke })
    setReason('')
    if (!selected.taxonomy_version_id) { setVersion(null); return }
    let active = true
    void api<TaxonomyVersion>(`${base}/taxonomy/versions/${selected.taxonomy_version_id}`)
      .then(value => { if (active) setVersion(value) }).catch(cause => { if (active) setError(cause.message) })
    return () => { active = false }
  }, [base, selectedId, selected?.taxonomy_version_id])

  async function generateRule() {
    setBusy(true); setError('')
    try { await api(`${base}/suggestions/rules`, { method: 'POST', body: JSON.stringify({ case_ids: [item.id] }) }); await load(); setNotice('规则候选已生成') }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function generateModel() {
    if (provider === 'remote' && !window.confirm('远程模型会接收用例正文。确认发送吗？')) return
    if (provider === 'codex' || provider === 'claude') {
      const label = provider === 'codex' ? 'Codex CLI' : 'Claude Code'
      if (!window.confirm(`${label} 将在 API 所在机器执行并读取用例正文。确认继续吗？`)) return
    }
    setBusy(true); setError('')
    try { const result = await api<SuggestionJob>(`${base}/suggestions/model`, { method: 'POST', body: JSON.stringify({ case_ids: [item.id], provider }) }); setJob(result); setNotice('AI 建议任务已创建') }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function retry() {
    if (!job) return
    setBusy(true); setError('')
    try { setJob(await api<SuggestionJob>(`${base}/suggestion-jobs/${job.id}/retry`, { method: 'POST' })) }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function decide(action: 'accept' | 'reject') {
    if (!selected || !choice) return
    setBusy(true); setError('')
    try {
      const original = { categories: selected.categories, level: selected.level, smoke: selected.smoke }
      const edited = JSON.stringify(choice) !== JSON.stringify(original)
      const response = await api<{ case: Case }>(`${base}/suggestions/${selected.id}/decision`, {
        method: 'POST', body: JSON.stringify({ action, case_version: item.version, reason: reason.trim(),
          choice: action === 'accept' && edited ? choice : undefined }),
      })
      await load(); setNotice(action === 'accept' ? '建议已采纳' : '建议已驳回')
      await onApplied(response.case)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  function changeCategory(key: DimensionKey, value: string) {
    setChoice(previous => previous ? { ...previous, categories: { ...previous.categories, [key]: value === 'pending' ? null : value } } : null)
  }
  const editable = canDecide && selected?.status === 'pending' && !selected.stale

  return <div className="suggestion-panel">
    <div className="section-caption">用例建议 <span>{suggestions.length} 条</span></div>
    <div className="suggestion-actions"><Button variant="outline" size="sm" disabled={!canDecide || busy} onClick={() => void generateRule()}>规则建议</Button>
      {(remoteEnabled || codexEnabled || claudeEnabled) && <Select value={provider} onValueChange={setProvider}><SelectTrigger aria-label="建议模型来源"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{remoteEnabled && <SelectItem value="remote">远程模型</SelectItem>}{codexEnabled && <SelectItem value="codex">Codex CLI（本机）</SelectItem>}{claudeEnabled && <SelectItem value="claude">Claude Code（本机）</SelectItem>}</SelectGroup></SelectContent></Select>}
      <Button variant="outline" size="sm" disabled={!canDecide || busy} onClick={() => void generateModel()}><Sparkles data-icon="inline-start" />AI 建议</Button></div>
    {job && <div className={`suggestion-job ${['failed', 'partial'].includes(job.status) ? 'is-error' : ''}`} role="status">
      <span>{job.status === 'done' ? 'AI 建议完成' : job.status === 'failed' ? 'AI 建议失败' : job.status === 'partial' ? '部分失败' : `AI 建议处理中 ${job.completed}/${job.total}`}</span>
      {job.errors.map(value => <small key={value.case_id ?? 'task'}>{value.message}</small>)}
      {canDecide && ['failed', 'partial'].includes(job.status) && <Button variant="ghost" size="icon-sm" aria-label="重试 AI 建议" title="重试 AI 建议" onClick={() => void retry()}><RefreshCw /></Button>}
    </div>}
    {notice && <p className="reviewed-text" role="status">{notice}</p>}
    {error && <p className="error-line" role="alert">{error}</p>}
    {suggestions.length ? <div className="suggestion-list">{suggestions.map(value => <button key={value.id} className={value.id === selectedId ? 'active' : ''} onClick={() => setSelectedId(value.id)}>
      <strong>{value.source === 'rule' ? '规则候选' : 'AI 候选'}</strong><span>{value.stale ? '已过期' : value.status === 'pending' ? '待确认' : value.status === 'accepted' ? '已采纳' : '已驳回'}</span>
    </button>)}</div> : <p className="activity-empty">暂无用例建议</p>}
    {selected && choice && <div className="suggestion-detail">
      <div className="suggestion-values"><span>建议等级 <strong>{selected.level ?? '待判定'}</strong></span><span>建议冒烟 <strong>{selected.smoke ?? '待判定'}</strong></span></div>
      <p>{selected.rationale}</p>
      {selected.questions.length > 0 && <div className="suggestion-questions">{selected.questions.map(text => <span key={text}><AlertCircle size={13} />{text}</span>)}</div>}
      <div className="suggestion-categories">{dimensions.map(({ key, label }) => <label key={key}>{label}
        {editable && version ? <Select value={choice.categories[key] ?? 'pending'} onValueChange={value => changeCategory(key, value)}><SelectTrigger aria-label={`${label}建议`}><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
          <SelectItem value="pending">待归类</SelectItem>{version.definitions[key].map(entry => <SelectItem key={entry.id} value={entry.id!}>{entry.name}</SelectItem>)}
        </SelectGroup></SelectContent></Select> : <strong>{selected.category_labels[key] ?? '待归类'}</strong>}
      </label>)}</div>
      {editable && <><div className="suggestion-overrides"><label>等级<Select value={choice.level ?? 'pending'} onValueChange={value => setChoice(previous => previous ? { ...previous, level: value === 'pending' ? null : value } : null)}><SelectTrigger aria-label="修改建议等级"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="pending">待判定</SelectItem>{['1', '2', '3', '4'].map(value => <SelectItem key={value} value={value}>等级 {value}</SelectItem>)}</SelectGroup></SelectContent></Select></label>
        <label>冒烟<Select value={choice.smoke ?? 'pending'} onValueChange={value => setChoice(previous => previous ? { ...previous, smoke: value === 'pending' ? null : value } : null)}><SelectTrigger aria-label="修改建议冒烟"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="pending">待判定</SelectItem><SelectItem value="是">是</SelectItem><SelectItem value="否">否</SelectItem></SelectGroup></SelectContent></Select></label></div>
        <Textarea aria-label="建议决策理由" value={reason} onChange={event => setReason(event.target.value)} maxLength={1000} placeholder="说明接受、修改或驳回的依据" />
        <div className="suggestion-decision-actions"><Button variant="outline" size="sm" disabled={busy || !reason.trim()} onClick={() => void decide('reject')}><X data-icon="inline-start" />驳回</Button><Button size="sm" disabled={busy || !reason.trim()} onClick={() => void decide('accept')}><Check data-icon="inline-start" />{JSON.stringify(choice) === JSON.stringify({ categories: selected.categories, level: selected.level, smoke: selected.smoke }) ? '接受建议' : '修改并接受'}</Button></div></>}
      {selected.decision_reason && <p className="suggestion-decision-note">处理依据：{selected.decision_reason}</p>}
      {selected.stale && selected.status === 'pending' && <p className="ai-alert warning">用例已更新，此建议不能继续采纳</p>}
    </div>}
  </div>
}
