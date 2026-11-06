import { lazy, Suspense, useEffect, useState } from 'react'
import { motion } from 'motion/react'
import { useLocation, useNavigate } from 'react-router'
import { AlertCircle, ArrowDownLeft, ArrowUpRight, ChevronDown, ChevronLeft, Download, GitBranch, Layers3, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { api, type ArtifactCatalog, type ArtifactKind, type ArtifactNode, type ArtifactRelations, type ProjectGraphSnapshot } from '@/lib/api'
import { artifactRelationLabels } from './artifactRelationLabels'

const categories: { id: Exclude<ArtifactKind, 'report_case' | 'conversion_input'>; label: string; group: string }[] = [
  { id: 'document', label: '需求材料', group: '材料' }, { id: 'import', label: '导入批次', group: '材料' },
  { id: 'analysis', label: '分析结果', group: '生成' }, { id: 'harness_task', label: 'Harness 任务', group: '生成' },
  { id: 'draft', label: '用例草稿', group: '生成' }, { id: 'case', label: '用例库', group: '生成' },
  { id: 'conversion', label: '转换结果', group: '生成' },
  { id: 'taxonomy', label: '分类版本', group: '分类' }, { id: 'suggestion_job', label: '建议任务', group: '分类' },
  { id: 'suggestion', label: '用例建议', group: '分类' },
  { id: 'governance_directory', label: '治理目录', group: '治理' },
  { id: 'governance_change', label: '治理变更', group: '治理' },
  { id: 'duplicate_group', label: '重复组', group: '治理' },
  { id: 'duplicate_member', label: '重复成员', group: '治理' },
  { id: 'governance_validation', label: '导入前验证', group: '治理' },
  { id: 'governance_metric', label: '批次指标', group: '治理' },
  { id: 'governance_export_job', label: '治理导出', group: '治理' },
  { id: 'smoke_suite', label: '冒烟集', group: '执行' }, { id: 'smoke_item', label: '冒烟用例', group: '执行' },
  { id: 'execution_run', label: '执行记录', group: '执行' }, { id: 'run_case', label: '执行用例', group: '执行' },
  { id: 'execution_report', label: '冻结报告', group: '执行' },
  { id: 'defect', label: '关联缺陷', group: '沉淀' }, { id: 'knowledge', label: '项目知识', group: '沉淀' },
]
const labels: Record<ArtifactKind, string> = {
  ...Object.fromEntries(categories.map(item => [item.id, item.label])) as Record<Exclude<ArtifactKind, 'report_case' | 'conversion_input'>, string>,
  report_case: '报告内用例', conversion_input: '转换原始文件',
}
function ref(node: Pick<ArtifactNode, 'type' | 'id'>) { return `${node.type}:${node.id}` }
function snapshotRoot(value: ProjectGraphSnapshot): ArtifactRelations {
  const center = value.nodes[0]
  const edges = value.edges.filter(edge => ref(edge.from) === ref(center) || ref(edge.to) === ref(center))
  const related = new Set(edges.flatMap(edge => [ref(edge.from), ref(edge.to)]))
  return { center, nodes: value.nodes.filter(node => ref(node) === ref(center) || related.has(ref(node))),
    edges, scope: 'direct', next_offset: null, truncated: false,
    revision: value.revision, page_checkpoint: value.revision }
}
const ArtifactGraphCanvas = lazy(() => import('./ArtifactGraphCanvas').then(module => ({ default: module.ArtifactGraphCanvas })))

export function ArtifactExplorerPage({ projectId }: { projectId: number }) {
  const location = useLocation()
  const navigate = useNavigate()
  const params = new URLSearchParams(location.search)
  const requested = params.get('kind') as ArtifactKind | null
  const kind: ArtifactKind = requested && (requested === 'report_case' || requested === 'conversion_input' || categories.some(item => item.id === requested)) ? requested : 'document'
  const id = params.get('id') || null
  const base = `/projects/${projectId}/artifacts`
  const selectedRef = id ? `${kind}:${id}` : null
  const selectionKey = selectedRef ? `${base}:${selectedRef}` : null
  const catalogKey = `${base}:${kind}`
  const [catalog, setCatalog] = useState<{ key: string; value: ArtifactCatalog; error: string; errorStatus?: number } | null>(null)
  const [catalogModeOverride, setCatalogModeOverride] = useState<{ key: string; mode: 'live' } | null>(null)
  const [graph, setGraph] = useState<{ key: string; value: ArtifactRelations | null; error: string; errorStatus?: number } | null>(null)
  const [relationModeOverride, setRelationModeOverride] = useState<{ key: string; mode: 'live' } | null>(null)
  const [paging, setPaging] = useState(false)
  const [edgePaging, setEdgePaging] = useState<string | null>(null)
  const [edgeError, setEdgeError] = useState<{ key: string; text: string; stale: boolean } | null>(null)
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 800px)').matches)
  const [typesOpen, setTypesOpen] = useState(false)
  const [detailView, setDetailView] = useState<'list' | 'graph'>('list')
  const [snapshotOpenKey, setSnapshotOpenKey] = useState<string | null>(null)
  const [projectGraph, setProjectGraph] = useState<{ key: string; value: ProjectGraphSnapshot | null; error: string } | null>(null)
  const currentCatalog = catalog?.key === catalogKey ? catalog : null
  const catalogMode = catalogModeOverride?.key === catalogKey ? 'live' : 'verified'
  const relationMode = relationModeOverride?.key === selectionKey ? 'live' : 'verified'
  const currentGraph = graph?.key === selectionKey ? graph : null
  const snapshotOpen = snapshotOpenKey === base
  const currentSnapshot = projectGraph?.key === base ? projectGraph : null

  useEffect(() => {
    const query = window.matchMedia('(max-width: 800px)')
    const update = () => setMobile(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  useEffect(() => {
    if (kind === 'report_case' || kind === 'conversion_input') return
    let active = true
    void api<ArtifactCatalog>(`${base}?kind=${kind}&consistency=${catalogMode}`)
      .then(value => { if (active) setCatalog({ key: catalogKey, value, error: '' }) })
      .catch(cause => { if (active) setCatalog({ key: catalogKey, value: { items: [], next_before_id: null }, error: cause.message, errorStatus: cause.status }) })
    return () => { active = false }
  }, [base, kind, catalogKey, catalogMode])
  useEffect(() => {
    if (!id || !selectionKey) return
    let active = true
    void api<ArtifactRelations>(`${base}/${kind}/${encodeURIComponent(id)}/relations?consistency=${relationMode}`)
      .then(value => { if (active) setGraph({ key: selectionKey, value, error: '' }) })
      .catch(cause => { if (active) setGraph({ key: selectionKey, value: null, error: cause.message, errorStatus: cause.status }) })
    return () => { active = false }
  }, [base, kind, id, selectionKey, relationMode])

  function open(node: ArtifactNode) {
    navigate(`${base}?kind=${node.type}&id=${encodeURIComponent(node.id)}`)
  }
  function choose(next: ArtifactKind) { setTypesOpen(false); navigate(`${base}?kind=${next}`) }
  async function more() {
    if (!currentCatalog?.value.next_before_id || paging) return
    setPaging(true)
    try {
      const requestedCursor = currentCatalog.value.next_before_id
      const revision = catalogMode === 'verified' ? currentCatalog.value.revision : undefined
      if (catalogMode === 'verified' && !revision) throw Object.assign(new Error('目录缺少一致性游标 请从头刷新'), { status: 409 })
      const page = await api<ArtifactCatalog>(`${base}?kind=${kind}&consistency=${catalogMode}&before_id=${encodeURIComponent(requestedCursor)}${revision ? `&revision=${encodeURIComponent(revision)}` : ''}`)
      if (catalogMode === 'verified' && page.revision !== revision) throw Object.assign(new Error('目录版本已变 请从头刷新'), { status: 409 })
      setCatalog(previous => previous?.key === catalogKey && previous.value.next_before_id === requestedCursor && previous.value.revision === revision ? {
        key: catalogKey, error: '', value: { items: [...previous.value.items, ...page.items], next_before_id: page.next_before_id, revision: page.revision },
      } : previous)
    } catch (cause) {
      const status = (cause as Error & { status?: number }).status
      setCatalog(previous => previous?.key === catalogKey ? {
        ...previous, error: (cause as Error).message, errorStatus: status,
        value: status === 409 ? { items: [], next_before_id: null } : previous.value,
      } : previous)
    } finally { setPaging(false) }
  }
  async function refreshCatalog() {
    if (paging) return
    setPaging(true)
    try {
      const value = await api<ArtifactCatalog>(`${base}?kind=${kind}&consistency=${catalogMode}`)
      setCatalog(previous => previous?.key === catalogKey ? { key: catalogKey, value, error: '' } : previous)
    } catch (cause) {
      setCatalog(previous => previous?.key === catalogKey ? {
        key: catalogKey, value: { items: [], next_before_id: null }, error: (cause as Error).message,
        errorStatus: (cause as Error & { status?: number }).status,
      } : previous)
    } finally { setPaging(false) }
  }
  async function moreRelations() {
    const offset = currentGraph?.value?.next_offset
    if (offset == null || !selectionKey || edgePaging === selectionKey) return
    setEdgePaging(selectionKey)
    setEdgeError(null)
    try {
      const revision = currentGraph!.value!.revision
      const checkpoint = currentGraph!.value!.page_checkpoint
      if (relationMode === 'verified' && !revision) throw new Error('关联缺少一致性游标 请从头刷新')
      if (relationMode === 'live' && !checkpoint) throw new Error('关联缺少相邻页校验 请从头刷新')
      const cursor = relationMode === 'verified' ? `revision=${encodeURIComponent(revision!)}` : `previous_page=${encodeURIComponent(checkpoint)}`
      const page = await api<ArtifactRelations>(`${base}/${kind}/${encodeURIComponent(id!)}/relations?consistency=${relationMode}&offset=${offset}&${cursor}`)
      if (relationMode === 'verified' && page.revision !== revision) throw Object.assign(new Error('关联版本已变 请从头刷新'), { status: 409 })
      setGraph(previous => {
        if (previous?.key !== selectionKey || !previous.value || previous.value.next_offset !== offset || previous.value.revision !== page.revision) return previous
        const existing = new Set(previous.value.nodes.map(ref))
        return { ...previous, value: {
          ...previous.value,
          nodes: [...previous.value.nodes, ...page.nodes.filter(node => !existing.has(ref(node)))],
          edges: [...previous.value.edges, ...page.edges],
          next_offset: page.next_offset, truncated: page.truncated, page_checkpoint: page.page_checkpoint, revision: page.revision,
        } }
      })
    } catch (cause) {
      setEdgeError({ key: selectionKey, text: (cause as Error).message,
        stale: (cause as Error & { status?: number }).status === 409 })
    } finally { setEdgePaging(previous => previous === selectionKey ? null : previous) }
  }
  async function refreshRelations() {
    if (!selectionKey || !id || edgePaging === selectionKey) return
    setEdgePaging(selectionKey)
    try {
      const value = await api<ArtifactRelations>(`${base}/${kind}/${encodeURIComponent(id)}/relations?consistency=${relationMode}`)
      setGraph(previous => previous?.key === selectionKey ? { key: selectionKey, value, error: '' } : previous)
      setEdgeError(null)
    } catch (cause) {
      setEdgeError({ key: selectionKey, text: (cause as Error).message, stale: false })
    } finally { setEdgePaging(previous => previous === selectionKey ? null : previous) }
  }

  async function loadSnapshot(reset = true) {
    if (reset) setProjectGraph({ key: base, value: null, error: '' })
    try {
      const value = await api<ProjectGraphSnapshot>(`${base}/snapshot`)
      setProjectGraph({ key: base, value, error: '' })
      return value
    } catch (cause) {
      setProjectGraph({ key: base, value: null, error: (cause as Error).message })
      throw cause
    }
  }

  return <motion.section className="artifact-explorer" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}>
    <header className="artifact-explorer-intro"><div><span>EVIDENCE / THREADS</span><h2>沿着证据往回看</h2><p>只展示项目内已保存的直接关系 点击任意产物继续追踪 不推断同名文件之间的联系</p></div><div className="artifact-explorer-intro-actions"><Button variant="outline" size="sm" onClick={() => {
      if (snapshotOpen) { setSnapshotOpenKey(null); return }
      setSnapshotOpenKey(base); void loadSnapshot().catch(() => {})
    }}><GitBranch size={15} />{snapshotOpen ? '返回逐项关系' : '建立项目快照'}</Button><a href={`/api/v1/projects/${projectId}/evidence/export`} download={`project-${projectId}-evidence.zip`} title="下载包含原始材料的项目证据 ZIP"><Download size={15} />下载证据包</a></div></header>
    {snapshotOpen ? <motion.div className="artifact-project-snapshot" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}>
      {!currentSnapshot ? <p role="status">正在建立同一数据库读取视图</p> : currentSnapshot.error ? <div role="alert" className="artifact-explorer-error"><p>{currentSnapshot.error}</p><p>未返回部分关系图 如项目超过上限 请使用逐项关系和证据包</p><Button variant="outline" size="sm" onClick={() => void loadSnapshot().catch(() => {})}>重试建立快照</Button></div> : !currentSnapshot.value ? <p role="status">正在建立同一数据库读取视图</p> : currentSnapshot.value.nodes.length === 0 ? <div className="artifact-explorer-zero">当前项目尚无可导航产物</div> : <Suspense fallback={<div role="status">正在打开项目快照图</div>}>
        <ArtifactGraphCanvas key={`${base}:snapshot`} projectId={projectId} graph={snapshotRoot(currentSnapshot.value)} snapshot={currentSnapshot.value}
          onRefreshSnapshot={() => loadSnapshot(false)} onOpen={node => { setSnapshotOpenKey(null); open(node) }} />
      </Suspense>}
    </motion.div> : <div className="artifact-explorer-shell"><aside className="artifact-explorer-index" aria-label="项目产物索引">
      <div className="artifact-explorer-index-heading"><Layers3 size={16} /><strong>产物目录</strong><button type="button" className="artifact-explorer-types-toggle" aria-label={`产物目录 当前${labels[kind]} 选择类型`} aria-controls="artifact-types" aria-expanded={!mobile || typesOpen} onClick={() => setTypesOpen(open => !open)}>{labels[kind]}<ChevronDown size={14} /></button></div>
      <motion.div id="artifact-types" className="artifact-explorer-types" initial={false} animate={{ height: mobile && !typesOpen ? 0 : 'auto', opacity: mobile && !typesOpen ? 0 : 1 }} transition={{ duration: .22 }} inert={mobile && !typesOpen} aria-hidden={mobile && !typesOpen}>
        <div className="artifact-explorer-types-grid" role="group" aria-label="产物类型 滚动查看更多">{categories.map((item, index) => <button type="button" key={item.id} className={kind === item.id ? 'active' : ''} aria-current={kind === item.id ? 'true' : undefined} onClick={() => choose(item.id)}><small>{index === 0 || categories[index - 1].group !== item.group ? item.group : ''}</small><span>{item.label}</span></button>)}</div>
      </motion.div>
      <div className="artifact-explorer-list-head"><strong>{labels[kind]}</strong><span>{kind === 'report_case' ? '通过冻结报告追踪' : kind === 'conversion_input' ? '通过转换结果追踪' : currentCatalog ? `${currentCatalog.value.items.length}${currentCatalog.value.next_before_id ? '+' : ''} 项` : '读取中'}</span></div>
      <div className="artifact-explorer-records">{kind !== 'report_case' && kind !== 'conversion_input' && (!currentCatalog ? <p role="status" className="artifact-explorer-placeholder">正在读取项目产物</p> : <>
        {currentCatalog.error && <div role="alert" className="artifact-explorer-error"><p>{currentCatalog.error}</p><Button variant="ghost" size="sm" disabled={paging} onClick={() => void refreshCatalog()}>从头刷新目录</Button>{currentCatalog.errorStatus === 413 && catalogMode === 'verified' && <Button variant="outline" size="sm" onClick={() => { setCatalog(null); setCatalogModeOverride({ key: catalogKey, mode: 'live' }) }}>使用实时目录</Button>}</div>}
        {catalogMode === 'live' && <p className="artifact-explorer-limit">实时目录 翻页期间可能发生变化</p>}
        {currentCatalog.value.items.map((node, index) => <motion.button key={ref(node)} type="button" className={`artifact-explorer-list-row ${selectedRef === ref(node) ? 'active' : ''}`} onClick={() => open(node)} initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: Math.min(index, 12) * .017 }}><strong>{node.title}</strong><small>{node.id} · v{node.version}</small></motion.button>)}
        {!currentCatalog.value.items.length && !currentCatalog.error && <p className="artifact-explorer-placeholder">当前类型还没有产物</p>}
        {currentCatalog.value.next_before_id && <Button variant="ghost" size="sm" disabled={paging} onClick={() => void more()}>{paging ? '读取中' : '加载更多'}</Button>}
      </>)}</div>
    </aside><div className="artifact-explorer-detail" aria-live="polite">
      {!selectionKey ? <div className="artifact-explorer-zero"><GitBranch size={27} /><h3>从一份产物开始</h3><p>选择左侧记录 看它来自哪里 又流向了哪里</p></div> : !currentGraph ? <div className="artifact-explorer-zero" role="status"><RefreshCw size={23} /><p>正在核对项目权限与关联</p></div> : currentGraph.error ? <div className="artifact-explorer-zero" role="alert"><AlertCircle size={23} /><h3>无法读取产物</h3><p>{currentGraph.error}</p>{currentGraph.errorStatus === 413 && relationMode === 'verified' && <Button variant="outline" size="sm" onClick={() => { setGraph(null); setEdgeError(null); setDetailView('list'); setRelationModeOverride({ key: selectionKey, mode: 'live' }) }}>使用实时关系列表</Button>}<Button variant="outline" size="sm" onClick={() => choose(kind)}>返回目录</Button></div> : currentGraph.value && (() => {
        const current = currentGraph.value
        return <><div className="artifact-explorer-center"><button className="artifact-explorer-back" type="button" onClick={() => navigate(-1)}><ChevronLeft size={15} />返回上一层</button><span>{labels[current.center.type]} / {current.center.id}</span><h3>{current.center.title}</h3><div className="artifact-explorer-metadata"><span>版本 {current.center.version}</span>{current.center.frozen && <span>执行时冻结</span>}{current.center.original_id && <span>原始 ID {current.center.original_id}</span>}{current.center.sha256 && <code>SHA-256 {current.center.sha256}</code>}</div>
          {current.center.type === 'execution_report' && <a href={`/api/v1/projects/${projectId}/execution-runs/${current.center.id}/report`} download>下载报告校验原文 <ArrowUpRight size={14} /></a>}
          {current.center.type === 'conversion_input' && <a href={`/api/v1/projects/${projectId}/conversion/history/${current.center.id}/source`} download>下载原始上传文件 <ArrowUpRight size={14} /></a>}</div>
          <div className="artifact-explorer-view-switch" role="group" aria-label="关联展示方式">
            <button type="button" aria-pressed={detailView === 'list'} onClick={() => setDetailView('list')}>关联列表</button>
            <button type="button" aria-pressed={detailView === 'graph'} disabled={relationMode === 'live'} title={relationMode === 'live' ? '实时关联不提供完整版本核对 请使用关系列表' : undefined} onClick={() => setDetailView('graph')}>关系图</button>
          </div>
          {relationMode === 'live' && <motion.div className="artifact-explorer-limit" role="status" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }}><AlertCircle size={15} />实时关联仅校验相邻页 翻页期间其他页面可能变化 不能作为完整快照</motion.div>}
          {detailView === 'graph' ? <Suspense fallback={<div className="artifact-explorer-placeholder" role="status">正在打开关系图</div>}>
            <ArtifactGraphCanvas key={`${selectionKey}:${current.revision}:${current.edges.length}`} projectId={projectId} graph={current} onOpen={open} />
          </Suspense> : <div className="artifact-explorer-edges"><div className="artifact-explorer-edges-title"><h4>直接关联</h4><span>{current.edges.length} 条{current.truncated ? ' · 后续关系未载入' : ''}</span></div>
            {current.edges.map((edge, index) => {
              const incoming = edge.to.type === current.center.type && edge.to.id === current.center.id
              const other = incoming ? edge.from : edge.to
              const node = current.nodes.find(item => item.type === other.type && item.id === other.id)
              if (!node) return null
              return <motion.button key={`${edge.relation}-${ref(node)}-${index}`} type="button" className="artifact-explorer-edge" disabled={node.missing} onClick={() => open(node)} initial={{ opacity: 0, x: incoming ? -7 : 7 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: Math.min(index, 12) * .025 }}><span className="artifact-explorer-direction">{incoming ? <ArrowDownLeft size={16} /> : <ArrowUpRight size={16} />}{incoming ? '来源' : '去向'}</span><span className="artifact-explorer-edge-copy"><small>{artifactRelationLabels[edge.relation] ?? edge.relation} · {labels[node.type]}</small><strong>{node.title}</strong><code>{node.id} · v{node.version}{node.missing ? ' · 来源已移除' : ''}</code></span></motion.button>
            })}
            {!current.edges.length && <div className="artifact-explorer-placeholder">尚无可验证的直接关联</div>}
            {current.next_offset !== null && <Button variant="outline" size="sm" disabled={edgePaging === selectionKey || edgeError?.key === selectionKey && edgeError.stale} onClick={() => void moreRelations()}>{edgePaging === selectionKey ? '读取关联中' : '加载后续关联'}</Button>}
            {edgeError?.key === selectionKey && <div className="artifact-explorer-error" role="alert"><p>{edgeError.text}</p><Button variant="ghost" size="sm" disabled={edgePaging === selectionKey} onClick={() => void refreshRelations()}>从头刷新关联</Button></div>}
            {current.truncated && relationMode === 'verified' && <div className="artifact-explorer-limit"><AlertCircle size={15} />仍有直接关联未载入 翻页时将核对完整关系版本 项目在浏览期间发生变更时请刷新重查</div>}
          </div>}</>
      })()}
    </div></div>}
  </motion.section>
}
