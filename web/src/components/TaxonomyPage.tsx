import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, Plus, RefreshCw, Save, Send, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { api, type DimensionKey, type TaxonomyDefinitions, type TaxonomyState, type TaxonomyVersion } from '@/lib/api'

const dimensions: { key: DimensionKey; label: string }[] = [
  { key: 'business_module', label: '业务模块' }, { key: 'business_scenario', label: '业务场景' },
  { key: 'test_dimension', label: '测试维度' }, { key: 'endpoint_type', label: '端类型' },
]

export function TaxonomyPage({ projectId, role }: { projectId: number; role: string }) {
  const base = `/projects/${projectId}/taxonomy`
  const canManage = role === 'admin' || role === 'manager'
  const [state, setState] = useState<TaxonomyState | null>(null)
  const [selected, setSelected] = useState<TaxonomyVersion | null>(null)
  const [definitions, setDefinitions] = useState<TaxonomyDefinitions | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const refresh = useCallback(async () => {
    const value = await api<TaxonomyState>(base)
    setState(value)
    const next = value.draft ?? value.published
    setSelected(next)
    setDefinitions(next ? structuredClone(next.definitions) : null)
  }, [base])
  useEffect(() => {
    let active = true
    void refresh().catch(cause => { if (active) setError(cause.message) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [refresh])
  useEffect(() => { if (!notice) return; const timer = window.setTimeout(() => setNotice(''), 4000); return () => window.clearTimeout(timer) }, [notice])

  async function selectVersion(id: string) {
    setError('')
    try {
      const version = await api<TaxonomyVersion>(`${base}/versions/${id}`)
      setSelected(version); setDefinitions(structuredClone(version.definitions))
    } catch (cause) { setError((cause as Error).message) }
  }
  async function createDraft() {
    setBusy(true); setError('')
    try { await api(`${base}/versions`, { method: 'POST' }); await refresh(); setNotice('分类草稿已创建') }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  function changeEntry(key: DimensionKey, index: number, patch: { name?: string; description?: string }) {
    setDefinitions(previous => previous ? { ...previous, [key]: previous[key].map((item, i) => i === index ? { ...item, ...patch } : item) } : previous)
  }
  function addEntry(key: DimensionKey) {
    setDefinitions(previous => previous ? { ...previous, [key]: [...previous[key], { name: '', description: '' }] } : previous)
  }
  function removeEntry(key: DimensionKey, index: number) {
    setDefinitions(previous => previous ? { ...previous, [key]: previous[key].filter((_, i) => i !== index) } : previous)
  }
  async function save() {
    if (!selected || !definitions) return
    setBusy(true); setError('')
    try {
      const updated = await api<TaxonomyVersion>(`${base}/versions/${selected.id}`, { method: 'PUT',
        body: JSON.stringify({ revision: selected.revision, definitions }) })
      setSelected(updated); setDefinitions(structuredClone(updated.definitions))
      setState(previous => previous ? { ...previous, draft: updated } : previous)
      setNotice(`分类草稿 v${updated.number} 已保存`)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function publish() {
    if (!selected) return
    if (!window.confirm(`发布分类 v${selected.number}？已发布版本不可再修改。`)) return
    setBusy(true); setError('')
    try { await api(`${base}/versions/${selected.id}/publish`, { method: 'POST', body: JSON.stringify({ revision: selected.revision }) }); await refresh(); setNotice('分类版本已发布') }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  const editing = canManage && selected?.status === 'draft'
  const dirty = editing && JSON.stringify(definitions) !== JSON.stringify(selected?.definitions)
  const invalid = definitions && dimensions.some(({ key }) => definitions[key].length === 0 || definitions[key].some(item => !item.name.trim()))

  return <div className="taxonomy-page">
    {error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    <div className="taxonomy-toolbar"><div><strong>{state?.published ? `当前发布 v${state.published.number}` : '尚无已发布分类'}</strong><span>{selected?.status === 'draft' ? `正在编辑草稿 v${selected.number}` : selected ? `查看 v${selected.number}` : '分类只由项目管理员发布'}</span></div>
      <div className="taxonomy-actions">{state?.versions.length ? <Select value={String(selected?.id ?? '')} onValueChange={value => void selectVersion(value)}><SelectTrigger aria-label="选择分类版本"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
        {state.versions.map(version => <SelectItem key={version.id} value={String(version.id)}>v{version.number} · {version.status === 'draft' ? '草稿' : '已发布'}</SelectItem>)}
      </SelectGroup></SelectContent></Select> : null}
        {canManage && !state?.draft && <Button variant="outline" size="sm" disabled={busy} onClick={() => void createDraft()}><Plus data-icon="inline-start" />新建版本</Button>}
        <Button variant="ghost" size="icon-sm" aria-label="刷新分类版本" title="刷新分类版本" onClick={() => void refresh().catch(cause => setError(cause.message))} disabled={busy}><RefreshCw /></Button>
      </div></div>
    {loading && <div className="taxonomy-empty" role="status">正在读取分类版本</div>}
    {!loading && !selected && <div className="taxonomy-empty">当前项目还没有分类版本</div>}
    {selected && definitions && <div className="taxonomy-dimensions">
      {dimensions.map(({ key, label }) => <section className="taxonomy-dimension" key={key} aria-label={label}>
        <div className="taxonomy-dimension-heading"><div><h2>{label}</h2><span>{definitions[key].length} 项</span></div>{editing && <Button variant="outline" size="sm" onClick={() => addEntry(key)} disabled={busy || definitions[key].length >= 100}><Plus data-icon="inline-start" />添加</Button>}</div>
        {definitions[key].map((entry, index) => <div className="taxonomy-entry" key={entry.id ?? `new-${index}`}>
          {editing ? <><Input aria-label={`${label}名称 ${index + 1}`} value={entry.name} maxLength={80} placeholder="分类名称" onChange={event => changeEntry(key, index, { name: event.target.value })} />
            <Input aria-label={`${label}说明 ${index + 1}`} value={entry.description} maxLength={500} placeholder="适用范围或判断依据" onChange={event => changeEntry(key, index, { description: event.target.value })} />
            <Button variant="ghost" size="icon-sm" aria-label={`删除${label} ${entry.name || index + 1}`} title="删除分类" onClick={() => removeEntry(key, index)}><Trash2 /></Button></> : <><strong>{entry.name}</strong><span>{entry.description || '—'}</span></>}
        </div>)}
        {definitions[key].length === 0 && <div className="taxonomy-dimension-empty">待配置</div>}
      </section>)}
      {editing && <div className="taxonomy-footer">{invalid && <span>四个维度都需要分类名称才能发布</span>}
        <Button variant="outline" disabled={busy || !dirty} onClick={() => void save()}><Save data-icon="inline-start" />保存草稿</Button>
        <Button disabled={busy || Boolean(invalid) || Boolean(dirty)} onClick={() => void publish()}><Send data-icon="inline-start" />发布版本</Button>
      </div>}
    </div>}
  </div>
}
