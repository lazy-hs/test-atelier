import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { motion } from 'motion/react'
import { Link } from 'react-router'
import { AlertCircle, Archive, BookOpen, Download, FileClock, GitBranch, Plus, RefreshCw, Save } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { api, type ArtifactRelations, type KnowledgeEntry, type KnowledgeHistory, type KnowledgeList, type KnowledgeRevision, type KnowledgeSourceType } from '@/lib/api'

const sourceLabels: Record<KnowledgeSourceType, string> = {
  document: '需求材料', analysis: '需求分析', harness_task: 'Harness 任务', case: '测试用例',
  conversion: '转换产物', execution_report: '执行报告', defect: '执行缺陷',
}
const relationLabels: Record<string, string> = { curated_as: '沉淀为知识' }

export function KnowledgePage({ projectId, readonly, initialSource, initialSourceId }: {
  projectId: number; readonly: boolean; initialSource?: KnowledgeSourceType; initialSourceId?: string
}) {
  const base = `/projects/${projectId}/knowledge`
  const [filter, setFilter] = useState<'active' | 'archived'>('active')
  const [entries, setEntries] = useState<KnowledgeEntry[]>([])
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null)
  const [selected, setSelected] = useState<KnowledgeEntry | null>(null)
  const [revisions, setRevisions] = useState<KnowledgeRevision[]>([])
  const [nextVersion, setNextVersion] = useState<number | null>(null)
  const [relations, setRelations] = useState<ArtifactRelations | null>(null)
  const [relationsError, setRelationsError] = useState('')
  const [sourceType, setSourceType] = useState<KnowledgeSourceType>(initialSource ?? 'document')
  const [sourceId, setSourceId] = useState(initialSourceId ?? '')
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [status, setStatus] = useState<'active' | 'archived'>('active')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const selectedId = selected?.id
  const selectedVersion = selected?.version

  const load = useCallback(async (beforeId?: number) => {
    const params = new URLSearchParams({ status: filter })
    if (beforeId) params.set('before_id', String(beforeId))
    return api<KnowledgeList>(`${base}?${params}`)
  }, [base, filter])
  useEffect(() => {
    let active = true
    void load().then(page => { if (active) {
      setEntries(page.items); setNextBeforeId(page.next_before_id); setError('')
    } }).catch(cause => { if (active) setError(cause.message) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [load])
  useEffect(() => {
    if (selectedId === undefined) return
    let active = true
    void Promise.allSettled([
      api<KnowledgeHistory>(`${base}/${selectedId}/revisions`),
      api<ArtifactRelations>(`/projects/${projectId}/artifacts/knowledge/${selectedId}/relations`),
    ]).then(([history, graph]) => {
      if (!active) return
      if (history.status === 'fulfilled') { setRevisions(history.value.items); setNextVersion(history.value.next_before_version) }
      else setError(history.reason.message)
      if (graph.status === 'fulfilled') setRelations(graph.value)
      else setRelationsError(graph.reason.message)
    })
    return () => { active = false }
  }, [base, projectId, selectedId, selectedVersion])

  function choose(entry: KnowledgeEntry | null) {
    setSelected(entry); setRevisions([]); setNextVersion(null); setError(''); setNotice('')
    setRelations(null); setRelationsError('')
    setSourceType(entry?.source_type ?? initialSource ?? 'document')
    setSourceId(entry?.source_id ?? initialSourceId ?? '')
    setTitle(entry?.title ?? ''); setBody(entry?.body ?? ''); setStatus(entry?.status ?? 'active')
  }

  function changeFilter(value: 'active' | 'archived') {
    if (filter === value) return
    setLoading(true); setEntries([]); setNextBeforeId(null)
    choose(null); setFilter(value)
  }

  async function refresh() {
    setLoading(true); setError('')
    try {
      const page = await load()
      setEntries(page.items); setNextBeforeId(page.next_before_id)
    } catch (cause) { setError((cause as Error).message) } finally { setLoading(false) }
  }

  async function more() {
    if (!nextBeforeId) return
    setBusy(true); setError('')
    try {
      const page = await load(nextBeforeId)
      setEntries(previous => [...previous, ...page.items]); setNextBeforeId(page.next_before_id)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }

  async function olderVersions() {
    if (!selected || !nextVersion) return
    setBusy(true); setError('')
    try {
      const history = await api<KnowledgeHistory>(`${base}/${selected.id}/revisions?before_version=${nextVersion}`)
      setRevisions(previous => [...previous, ...history.items])
      setNextVersion(history.next_before_version)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (readonly || busy || !title.trim() || !body.trim()) return
    setBusy(true); setError(''); setNotice('')
    try {
      const next = selected
        ? await api<KnowledgeEntry>(`${base}/${selected.id}`, { method: 'PATCH', body: JSON.stringify({
            version: selected.version, title: title.trim(), body: body.trim(), status,
          }) })
        : await api<KnowledgeEntry>(base, { method: 'POST', body: JSON.stringify({
            source_type: sourceType, source_id: sourceId.trim(), title: title.trim(), body: body.trim(),
          }) })
      const message = selected ? '已保存新版本 来源引用保持不变' : '知识条目已建立并冻结来源'
      choose(next)
      const page = await load()
      setEntries(page.items); setNextBeforeId(page.next_before_id)
      if (next.status !== filter) choose(null)
      setNotice(message)
    } catch (cause) {
      setError((cause as Error).message)
      if (selected && (cause as Error & { status?: number }).status === 409) {
        try {
          choose(await api<KnowledgeEntry>(`${base}/${selected.id}`))
          setError('条目已被修改，已加载最新版本，请核对后重新编辑')
        } catch { /* Preserve the original conflict error. */ }
      }
    } finally { setBusy(false) }
  }

  return <motion.section className="knowledge-page" initial={{ opacity: 0, y: 9 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}>
    <div className="knowledge-intro"><div><span>CURATED / EVIDENCE</span><h2>把结论连回证据</h2><p>知识由人工编写 来源与历史版本独立保留 不直接改写 Harness 输出</p></div><div className="knowledge-intro-actions"><a className="knowledge-export-link" href={`/api/v1/projects/${projectId}/evidence/export`} download={`project-${projectId}-evidence.zip`} title="包含项目原始材料和业务结果 仅向授权成员分享"><Download size={14} />下载项目证据</a><Button size="sm" variant="outline" onClick={() => void refresh()} disabled={loading}><RefreshCw data-icon="inline-start" />刷新</Button></div></div>
    {error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    <div className="knowledge-layout"><div className="knowledge-index"><div className="knowledge-index-head"><div><strong>项目知识</strong><small>{entries.length}{nextBeforeId ? '+' : ''} 条</small></div>{!readonly && <Button size="icon-sm" variant="ghost" aria-label="新建知识" title="新建知识" onClick={() => choose(null)}><Plus size={16} /></Button>}</div>
      <div className="knowledge-tabs"><button className={filter === 'active' ? 'active' : ''} onClick={() => changeFilter('active')}>当前</button><button className={filter === 'archived' ? 'active' : ''} onClick={() => changeFilter('archived')}>归档</button></div>
      {loading ? <p className="knowledge-empty" role="status">正在读取项目知识</p> : entries.map(entry => <button key={entry.id} className={`knowledge-row ${selected?.id === entry.id ? 'active' : ''}`} onClick={() => choose(entry)}><BookOpen size={17} /><span><strong>{entry.title}</strong><small>{sourceLabels[entry.source_type]} · 第 {entry.version} 版</small></span></button>)}
      {!loading && !entries.length && <div className="knowledge-empty"><FileClock size={22} /><p>{filter === 'active' ? '尚无知识条目 可从已有产物沉淀结论' : '尚无归档条目'}</p></div>}
      {nextBeforeId && <Button variant="ghost" size="sm" onClick={() => void more()} disabled={busy}>加载更早条目</Button>}
    </div><div className="knowledge-detail">
      {selected || !readonly ? <form onSubmit={event => void save(event)}><div className="knowledge-detail-head"><div><span>{selected ? `条目 #${selected.id} · v${selected.version}` : '新的知识条目'}</span><h3>{selected ? selected.title : '整理结论'}</h3></div>{selected && <span className={`knowledge-state ${selected.status}`}>{selected.status === 'active' ? '当前' : '已归档'}</span>}</div>
        {selected ? <div className="knowledge-source">
          <small>冻结来源</small><strong>{sourceLabels[selected.source_type]} · {selected.source_snapshot.title || selected.source_id}</strong>
          <code>{selected.source_id}</code><span>来源版本 {String(selected.source_snapshot.version ?? '—')} · 来源删除或改动不影响这份引用快照</span>
          {selected.source_type === 'document' && (selected.source_snapshot.source_integrity === 'recorded' && selected.source_snapshot.source_sha256
            ? <><span>创建条目时记录的原件 SHA-256 · {selected.source_snapshot.source_size} 字节，下载原件时仍需核对</span><code>{selected.source_snapshot.source_sha256}</code></>
            : <span>旧材料未记录原件摘要，不能据此确认原件完整性</span>)}
          {selected.source_type === 'conversion' && (selected.source_snapshot.output_integrity === 'recorded' && selected.source_snapshot.output_sha256
            ? <><span>创建条目时记录的转换结果 SHA-256 · {selected.source_snapshot.output_size} 字节，下载结果时仍需核对</span><code>{selected.source_snapshot.output_sha256}</code></>
            : <span>旧转换记录未保存结果摘要，不能据此确认转换文件完整性</span>)}
          {selected.source_type === 'harness_task' && (selected.source_snapshot.result_snapshot_sha256
            ? <><span>Harness 结果 ID · {selected.source_snapshot.result_id} · 持久化结构化结果的规范化 SHA-256</span><code>{selected.source_snapshot.result_snapshot_sha256}</code></>
            : <span>旧知识条目未冻结 Harness 结果摘要，不能据此核对结果版本</span>)}
          {selected.source_type === 'harness_task' && selected.source_snapshot.source_analysis_snapshot_sha256 && <>
            <span>关联分析 {selected.source_snapshot.analysis_run_id} · 建立知识时的原始分析规范化 SHA-256</span>
            <code>{selected.source_snapshot.source_analysis_snapshot_sha256}</code>
          </>}
          {selected.source_type === 'analysis' && (selected.source_snapshot.analysis_result_snapshot_sha256
            ? <><span>{selected.source_snapshot.provider === 'harness' ? `Harness 任务 ${selected.source_snapshot.harness_task_id} · ` : ''}业务分析持久化结果的规范化 SHA-256</span><code>{selected.source_snapshot.analysis_result_snapshot_sha256}</code></>
            : <span>旧知识条目未冻结分析结果摘要，不能据此核对结果版本</span>)}
        </div> : <div className="knowledge-source-edit"><label>来源类型<Select value={sourceType} onValueChange={value => setSourceType(value as KnowledgeSourceType)}><SelectTrigger aria-label="来源类型"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{Object.entries(sourceLabels).map(([key, label]) => <SelectItem key={key} value={key}>{label}</SelectItem>)}</SelectGroup></SelectContent></Select></label><label>来源 ID<Input value={sourceId} onChange={event => setSourceId(event.target.value)} required maxLength={64} placeholder="使用项目内已有产物的 ID" /></label><p>只接受本项目的已存在来源 分析与 Harness 任务须已完成</p></div>}
        <label>知识标题<Input value={title} onChange={event => setTitle(event.target.value)} required maxLength={160} disabled={readonly} placeholder="例如 接口超时处理约定" /></label>
        <label>人工结论<Textarea value={body} onChange={event => setBody(event.target.value)} required maxLength={20000} disabled={readonly} placeholder="记录可复用的判断、边界条件及复核依据" /></label>
        {selected && !readonly && <div className="knowledge-status-line"><label>条目状态<Select value={status} onValueChange={value => setStatus(value as 'active' | 'archived')}><SelectTrigger aria-label="条目状态"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="active">当前</SelectItem><SelectItem value="archived">已归档</SelectItem></SelectGroup></SelectContent></Select></label><Archive size={16} /><small>归档保留全部历史 不删除证据</small></div>}
        {!readonly && <Button type="submit" size="sm" disabled={busy || !sourceId.trim() || !title.trim() || !body.trim()}><Save data-icon="inline-start" />{busy ? '保存中' : selected ? '保存新版本' : '建立知识条目'}</Button>}
      </form> : <div className="knowledge-empty"><BookOpen size={25} /><p>选择左侧条目查看来源与审计版本</p></div>}
      {selected && <div className="knowledge-history"><strong>版本记录 · {revisions.length}{nextVersion ? '+' : ''}</strong>{revisions.map(item => <details key={item.version}><summary>v{item.version} · 账号 #{item.actor_id} · {new Date(item.created_at).toLocaleString('zh-CN')} · {item.status === 'active' ? '当前' : '归档'}</summary><p>{item.title}</p><pre>{item.body}</pre></details>)}{nextVersion && <Button variant="ghost" size="sm" disabled={busy} onClick={() => void olderVersions()}>查看更早版本</Button>}</div>}
      {selected && <section className="knowledge-relations" aria-label="关联产物"><h4><GitBranch size={15} />直接关联</h4><Link className="knowledge-explore-link" to={`/projects/${projectId}/artifacts?kind=knowledge&id=${selected.id}`}>在证据关系中继续追踪 <GitBranch size={14} /></Link>
        {relationsError ? <p role="alert">关联读取失败 {relationsError}</p> : !relations ? <p role="status">正在核对关联</p> : <>
          {relations.edges.map(edge => {
            const node = relations.nodes.find(item => item.type === edge.from.type && item.id === edge.from.id)
            return node && <motion.div key={`${edge.relation}-${edge.from.id}`} initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="knowledge-relation">
              <span>{relationLabels[edge.relation] ?? edge.relation}</span><strong>{sourceLabels[node.type as KnowledgeSourceType] ?? node.type} · {node.title}</strong>
              <small>{node.id} · 来源版本 {String(node.version)}{node.missing ? ' · 原始产物已移除 保留冻结引用' : ''}</small>
              {node.type === 'execution_report' && !node.missing && <a href={`/api/v1/projects/${projectId}/execution-runs/${node.id}/report`} download>下载冻结报告</a>}
            </motion.div>
          })}
          {!relations.edges.length && <p>暂无可验证的直接关联</p>}
          {relations.truncated && <p>关联结果达到上限 请按来源 ID 单独核查</p>}
        </>}
      </section>}
    </div></div>
  </motion.section>
}
