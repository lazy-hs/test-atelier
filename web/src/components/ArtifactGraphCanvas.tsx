import { useEffect, useState } from 'react'
import { Background, Controls, MiniMap, ReactFlow, useNodesInitialized, useReactFlow, type Edge, type Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { AlertCircle, ArrowUpRight, GitBranch, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { api, type ArtifactNode, type ArtifactRelations, type ProjectGraphSnapshot } from '@/lib/api'
import { artifactRelationLabels } from './artifactRelationLabels'

const STEP = 16
const MAX_NODES = 180
const MAX_EDGES = 240
const key = (node: Pick<ArtifactNode, 'type' | 'id'>) => `${node.type}:${node.id}`

type GraphNode = Node<ArtifactNode & { label: string }>
type Cached = { value: ArtifactRelations; shown: number }
type GraphState = { nodes: GraphNode[]; edges: Edge[]; cache: Record<string, Cached> }

function append(state: GraphState, origin: string, value: ArtifactRelations, shown: number): GraphState {
  const nodes = [...state.nodes]
  const edges = [...state.edges]
  const existing = new Set(nodes.map(node => node.id))
  const drawn = new Set(edges.map(edge => edge.id))
  const originNode = nodes.find(node => node.id === origin)
  if (!originNode) return state
  const indexed = new Map(value.nodes.map(node => [key(node), node]))
  let consumed = shown
  for (const relation of value.edges.slice(shown, shown + STEP)) {
    if (edges.length >= MAX_EDGES) break
    const from = key(relation.from)
    const to = key(relation.to)
    const other = from === origin ? to : from
    const target = indexed.get(other)
    if (!target) { consumed += 1; continue }
    if (!existing.has(other)) {
      if (nodes.length >= MAX_NODES) break
      const direction = from === origin ? 1 : -1
      const x = originNode.position.x + direction * 310
      let y = originNode.position.y + (consumed - shown - 2) * 94
      while (nodes.some(node => node.position.x === x && Math.abs(node.position.y - y) < 88)) y += 94
      nodes.push({ id: other, data: { ...target, label: target.title }, position: { x, y }, draggable: false,
        style: { width: 226 }, className: `artifact-graph-node ${target.missing ? 'missing' : ''}` })
      existing.add(other)
    }
    const edgeId = `${from}:${relation.relation}:${to}`
    if (!drawn.has(edgeId)) {
      edges.push({ id: edgeId, source: from, target: to, label: artifactRelationLabels[relation.relation] ?? relation.relation,
        animated: false, type: 'smoothstep', style: { stroke: '#8baed8', strokeWidth: 1.5 } })
      drawn.add(edgeId)
    }
    consumed += 1
  }
  return { nodes, edges, cache: { ...state.cache, [origin]: { value, shown: consumed } } }
}

function initial(value: ArtifactRelations, preload = true): GraphState {
  const root = key(value.center)
  const state: GraphState = { nodes: [{ id: root, data: { ...value.center, label: value.center.title }, position: { x: 0, y: 0 },
    draggable: false, style: { width: 226 }, className: 'artifact-graph-node root' }],
    edges: [], cache: {} }
  return preload ? append(state, root, value, 0) : state
}

function snapshotRelations(center: ArtifactNode, snapshot: ProjectGraphSnapshot): ArtifactRelations {
  const centerKey = key(center)
  const edges = snapshot.edges.filter(edge => key(edge.from) === centerKey || key(edge.to) === centerKey)
  const neighbors = new Set(edges.flatMap(edge => [key(edge.from), key(edge.to)]))
  return { center, nodes: snapshot.nodes.filter(node => node === center || neighbors.has(key(node))), edges,
    next_offset: null, truncated: false, scope: 'direct', revision: snapshot.revision,
    page_checkpoint: snapshot.revision }
}

function RefitOnResize({ nodeCount }: { nodeCount: number }) {
  const { fitView } = useReactFlow()
  const nodesInitialized = useNodesInitialized()
  useEffect(() => {
    let frame = 0
    const stage = document.querySelector('.artifact-graph-stage')
    if (!stage) return
    const resize = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => { frame = requestAnimationFrame(() => void fitView({ padding: .18, maxZoom: 1, duration: 180 })) })
    }
    const observer = new ResizeObserver(resize)
    observer.observe(stage)
    return () => { observer.disconnect(); cancelAnimationFrame(frame) }
  }, [fitView])
  useEffect(() => {
    if (!nodesInitialized) return
    const frame = requestAnimationFrame(() => void fitView({ padding: .18, maxZoom: 1, duration: 180 }))
    return () => cancelAnimationFrame(frame)
  }, [fitView, nodesInitialized, nodeCount])
  return null
}

export function ArtifactGraphCanvas({ projectId, graph, onOpen, snapshot, onRefreshSnapshot }: {
  projectId: number; graph: ArtifactRelations; onOpen: (node: ArtifactNode) => void
  snapshot?: ProjectGraphSnapshot; onRefreshSnapshot?: () => Promise<ProjectGraphSnapshot>
}) {
  const [state, setState] = useState(() => initial(graph))
  const [selected, setSelected] = useState(() => key(graph.center))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [stale, setStale] = useState(false)
  const [query, setQuery] = useState('')
  const chosen = state.nodes.find(node => node.id === selected)
  const cache = state.cache[selected]
  const more = !chosen?.data.missing && (!cache || cache.shown < cache.value.edges.length || cache.value.next_offset !== null)
  const atLimit = state.nodes.length >= MAX_NODES || state.edges.length >= MAX_EDGES

  async function expand() {
    if (!chosen || busy || stale || atLimit || !more) return
    setBusy(true)
    setError('')
    try {
      const node = chosen.data
      const base = `/projects/${projectId}/artifacts/${node.type}/${encodeURIComponent(node.id)}/relations`
      let value = cache?.value
      let shown = cache?.shown ?? 0
      if (!value) {
        value = snapshot ? snapshotRelations(node, snapshot) : await api<ArtifactRelations>(`${base}?consistency=verified`)
      } else if (shown === value.edges.length && value.next_offset !== null) {
        if (!value.revision) throw new Error('缺少完整关系游标 请刷新图谱')
        const page = await api<ArtifactRelations>(`${base}?consistency=verified&offset=${value.next_offset}&revision=${value.revision}`)
        if (page.revision !== value.revision) throw new Error('关系版本发生变化 请刷新图谱')
        const known = new Set(value.nodes.map(key))
        value = { ...value, nodes: [...value.nodes, ...page.nodes.filter(item => !known.has(key(item)))],
          edges: [...value.edges, ...page.edges], next_offset: page.next_offset, truncated: page.truncated }
      }
      const nextValue = value
      setState(previous => previous.nodes.some(item => item.id === selected)
        ? append(previous, selected, nextValue, shown) : previous)
    } catch (cause) {
      setError((cause as Error).message)
      setStale((cause as Error & { status?: number }).status === 409)
    } finally { setBusy(false) }
  }

  async function refresh() {
    if (busy) return
    setBusy(true)
    try {
      const latest = snapshot && onRefreshSnapshot ? await onRefreshSnapshot() : null
      const center = latest?.nodes.find(node => key(node) === selected) ??
        latest?.nodes.find(node => key(node) === key(graph.center)) ?? latest?.nodes[0] ?? graph.center
      const value = latest ? snapshotRelations(center, latest) : await api<ArtifactRelations>(`/projects/${projectId}/artifacts/${center.type}/${encodeURIComponent(center.id)}/relations?consistency=verified`)
      setState(initial(value))
      setSelected(key(value.center))
      setError('')
      setStale(false)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }

  return <section className="artifact-graph" aria-label="项目产物交互关系图">
    <div className="artifact-graph-toolbar">
      <div><strong>{snapshot ? '项目关系快照' : '关系图'}</strong><span>{snapshot ? `${snapshot.node_count} 个产物 · ${snapshot.edge_count} 条关系 · 同一次数据库读取` : `${state.nodes.length} 个产物 · ${state.edges.length} 条可验证关系`}</span></div>
      <Button variant="ghost" size="sm" disabled={busy} onClick={() => void refresh()}><RefreshCw size={14} />刷新图谱</Button>
    </div>
    {snapshot && <div className="artifact-graph-snapshot-search">
      <Input aria-label="查找快照产物" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索产物名称 类型或 ID" />
      {query.trim() && <div className="artifact-graph-snapshot-results">{snapshot.nodes.filter(node =>
        `${node.title} ${node.type} ${node.id}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
        .slice(0, 8).map(node => <button key={key(node)} type="button" onClick={() => {
          setState(initial(snapshotRelations(node, snapshot), false)); setSelected(key(node)); setQuery('')
        }}>{node.title}<small>{node.type} / {node.id}</small></button>)}
      </div>}
    </div>}
    <div className="artifact-graph-stage" aria-label="可缩放关系图 使用节点选择产物">
      <ReactFlow nodes={state.nodes} edges={state.edges} fitView fitViewOptions={{ padding: .22, maxZoom: 1 }} minZoom={.12}
        nodesDraggable={false} nodesConnectable={false} elementsSelectable
        onNodeClick={(_, node) => setSelected(node.id)}
        onNodeDoubleClick={(_, node) => { if (!node.data.missing) onOpen(node.data as ArtifactNode) }}>
        <Background color="#e4edf8" gap={22} size={1} />
        <Controls showInteractive={false} />
        {state.nodes.length > 12 && <MiniMap pannable zoomable nodeColor={node => node.id === selected ? '#2164bb' : '#a4bcda'} />}
        <RefitOnResize nodeCount={state.nodes.length} />
      </ReactFlow>
    </div>
    <div className="artifact-graph-inspector" aria-live="polite">
      <GitBranch size={16} />
      <div><strong>{chosen?.data.title ?? '选择一个产物'}</strong><span>{chosen?.data.type} / {chosen?.data.id}{chosen?.data.missing ? ' · 来源已移除' : ''}</span></div>
      <Button variant="outline" size="sm" disabled={!more || atLimit || busy || stale} onClick={() => void expand()}>
        {busy ? '核对中' : cache?.shown && cache.shown < cache.value.edges.length ? '继续显示' : '展开关联'}
      </Button>
      <Button variant="ghost" size="sm" disabled={!chosen || Boolean(chosen.data.missing)} onClick={() => { if (chosen) onOpen(chosen.data) }}><ArrowUpRight size={14} />查看详情</Button>
    </div>
    {atLimit && <p className="artifact-graph-note" role="status">图谱达到 180 个产物或 240 条关系的交互上限 可在列表查看其余关系</p>}
    {error && <div className="artifact-graph-warning" role="alert"><AlertCircle size={16} /><span>{error}{stale ? ' 当前节点的后续关系已停止加载' : ''}</span><Button variant="ghost" size="sm" onClick={() => void refresh()}>重新核对</Button></div>}
    <p className="artifact-graph-note">{snapshot ? '所有可导航节点来自同一次数据库读取 在快照内搜索和展开不会混入新写入 已移除来源及原始文件需查证据包 超出上限时不会生成部分快照' : '按需展开各节点的直接关系 每个节点独立核对版本 不代表整个项目在同一时刻的快照 已移除来源不可导航'}</p>
  </section>
}
