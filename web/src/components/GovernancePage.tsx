import { useEffect, useMemo, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { AlertTriangle, Archive, Check, ChevronRight, Download, Edit3, FileCheck2, FolderTree, ListChecks, RefreshCw, ShieldCheck, Sparkles, UploadCloud, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { api, formatDateTime, rawApi } from '@/lib/api'

type Props = { projectId: number; readonly: boolean; role: string }
type Dashboard = { batch: { id: number; filename: string; sha256: string; created_at: string } | null; metrics: Record<string, number>; status_counts: Record<string, number> }
type Duplicate = { id: number; gid: string; name: string; size: number; cluster_count: number; category: string; status: string; revision: number; suggested_discard_count: number }
type Detail = Duplicate & { members: { case_id: number; cluster_fp: string; score: number; decision: string; reason: string; case: { id: number; platform_id: string; name: string; directory: string; endpoint_type: string; level: string; status: string; steps: string; expected: string; created_at: string; updated_at: string; merged_into: number | null } }[] }
type ValidationIssue = { row: number; code: string; message: string }
type ValidationResult = { id: number; status: string; blocked: number; warnings: number; can_promote: boolean; errors: ValidationIssue[]; warning_items: ValidationIssue[]; rows?: Record<string, string>[] }
type Directory = { path: string; level: number; whitelisted: boolean; frozen: boolean; case_count: number; case_ids: number[] }
type DirectoryNode = { path: string; label: string; directory?: Directory; children: DirectoryNode[] }
type RegressionItem = Record<string, string>
type ExportResult = { id: number; filename: string; rows: number; selfcheck: { format: string; sheet: string; rows: number; readback: boolean; readback_rows?: number; readback_headers?: string[]; headers?: string[]; error?: string }; download: string }
type Tab = 'audit' | 'duplicates' | 'validation' | 'directories' | 'regression'
type GovernanceDecision = 'keep' | 'merge' | 'discard' | 'retired' | 'rename' | string

const metricLabels: Record<string, string> = { case_count: '用例总量', directory_count: '目录数量', duplicate_group_count: '同名用例组', duplicate_case_count: '涉及的用例数', fingerprint_cluster_count: '内容相同组', multi_cluster_group_count: '内容分成多组的重名组', discard_count: '建议治理' }
const csvHeaders = ['测试用例ID', '用例名称', '用例类型', '用例状态', '用例等级', '创建人', '用例目录', '用例步骤', '前置条件', '预期结果', '备注', '是否冒烟用例', '端类型']
const decisionLabels: Record<string, string> = { keep: '保留', merge: '合并', discard: '废弃', retired: '已废弃', rename: '改名保留' }
const endpointOptions = ['win/mac/web', 'ios/android', 'appletv/androidtv']

function formatDecision(value: GovernanceDecision) { return decisionLabels[value] ?? value }
function lastDirectorySegment(value: string) { return value.split(' - ').map(item => item.trim()).filter(Boolean).at(-1) ?? value }

function parseCsv(value: string) {
  const lines = value.split(/\r?\n/).filter(line => line.trim())
  if (!lines.length) return []
  const cells = (line: string) => line.split(',').map(item => item.trim().replace(/^"|"$/g, ''))
  const headers = cells(lines[0])
  return lines.slice(1).map(line => Object.fromEntries(headers.map((header, index) => [header, cells(line)[index] || ''])))
}

function buildDirectoryTree(items: Directory[]): DirectoryNode[] {
  const roots: DirectoryNode[] = []
  for (const directory of items) {
    const parts = directory.path.split(' - ').map(part => part.trim()).filter(Boolean)
    let branch = roots
    let path = ''
    parts.forEach((label, index) => {
      path = path ? `${path} - ${label}` : label
      let node = branch.find(item => item.path === path)
      if (!node) { node = { path, label, children: [] }; branch.push(node) }
      if (index === parts.length - 1) node.directory = directory
      branch = node.children
    })
  }
  return roots
}

export function GovernancePage({ projectId, readonly, role }: Props) {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null)
  const [groups, setGroups] = useState<Duplicate[]>([])
  const [detail, setDetail] = useState<Detail | null>(null)
  const [tab, setTab] = useState<Tab>('audit')
  const [category, setCategory] = useState('all')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [preview, setPreview] = useState<{ id: number; filename: string; count: number; validation?: { blocked: number; warnings: number } } | null>(null)
  const [validationRows, setValidationRows] = useState<Record<string, string>[]>([])
  const [validationText, setValidationText] = useState('')
  const [validationMode, setValidationMode] = useState<'new' | 'legacy'>('new')
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const [directories, setDirectories] = useState<Directory[]>([])
  const [directoryQuery, setDirectoryQuery] = useState('')
  const [selectedDirectory, setSelectedDirectory] = useState<Directory | null>(null)
  const [targetDirectory, setTargetDirectory] = useState('')
  const [regression, setRegression] = useState<{ total: number; items: RegressionItem[] }>({ total: 0, items: [] })
  const [iteration, setIteration] = useState('')
  const [level, setLevel] = useState('')
  const [domain, setDomain] = useState('')
  const [exported, setExported] = useState<ExportResult | null>(null)
  const [editingCase, setEditingCase] = useState<number | null>(null)
  const [editDirectory, setEditDirectory] = useState('')
  const [editEndpoint, setEditEndpoint] = useState(endpointOptions[0])
  const [aiProvider, setAiProvider] = useState('codex_cli')
  const [aiRawOutput, setAiRawOutput] = useState('')
  const [directoryPickerOpen, setDirectoryPickerOpen] = useState(false)
  const [directoryPickerQuery, setDirectoryPickerQuery] = useState('')
  const [expandedPickerPaths, setExpandedPickerPaths] = useState<Set<string>>(new Set())

  async function reload() {
    const [summary, duplicate] = await Promise.all([
      api<Dashboard>(`/projects/${projectId}/governance/dashboard`),
      api<{ items: Duplicate[] }>(`/projects/${projectId}/governance/duplicates?category=${category}&q=${encodeURIComponent(query)}`),
    ])
    setDashboard(summary); setGroups(duplicate.items)
    if (detail) setDetail(await api<Detail>(`/projects/${projectId}/governance/duplicates/${detail.id}`))
  }
  useEffect(() => { void reload().catch(cause => setError((cause as Error).message)) }, [projectId, category, query])

  async function upload(file: File) {
    setBusy(true); setError(''); setNotice('')
    try {
      const form = new FormData(); form.append('file', file)
      const response = await rawApi(`/projects/${projectId}/governance/imports/preview`, { method: 'POST', body: form })
      const next = await response.json()
      setPreview(next)
      setNotice(next.validation?.blocked ? `预览完成 发现 ${next.validation.blocked} 个阻断项` : `已读取 ${next.count} 条用例  请确认导入`)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function confirmImport() {
    if (!preview) return
    setBusy(true); setError('')
    try { const result = await api<{ count: number }>(`/projects/${projectId}/governance/imports/${preview.id}/confirm`, { method: 'POST' }); setNotice(`已导入 ${result.count} 条用例`); setPreview(null); await reload() }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function retryGovernance() {
    if (!canWrite) return
    setBusy(true); setError('')
    try { const result = await api<{ groups: number; cases: number }>(`/projects/${projectId}/governance/retry`, { method: 'POST' }); setNotice(`已重新开放 ${result.groups} 个治理组`); await reload() }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function aiGovernance(groupId: number) {
    if (!canWrite) return
    setBusy(true); setError('')
    try { const result = await api<{ changed: number; fallback?: boolean; raw_output?: string; message?: string }>(`/projects/${projectId}/governance/duplicates/${groupId}/ai`, { method: 'POST', body: JSON.stringify({ provider: aiProvider }) }); if (result.fallback) { setAiRawOutput(result.raw_output ?? ''); setNotice(result.message ?? '模型返回无法兼容，已保留原始输出') } else { setAiRawOutput(''); setNotice(`AI 已按治理评分规则更新 ${result.changed} 条建议`) }; await reload() }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function applyGroup(groupId: number) {
    if (readonly || !['admin', 'manager'].includes(role)) return
    setBusy(true); setError('')
    try { const result = await api<{ changed: number }>(`/projects/${projectId}/governance/duplicates/apply`, { method: 'POST', body: JSON.stringify({ group_ids: [groupId] }) }); setNotice(`已治理 ${result.changed} 条用例`); setDetail(null); await reload() }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function saveCaseEdit(caseId: number) {
    if (!editDirectory.trim() || !editEndpoint.trim()) return
    setBusy(true); setError('')
    try {
      await api(`/projects/${projectId}/governance/cases/${caseId}`, { method: 'PUT', body: JSON.stringify({ directory: editDirectory.trim(), endpoint_type: editEndpoint.trim() }) })
      setNotice('已更新目录和端类型'); setEditingCase(null); await reload()
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function loadDirectories() {
    setBusy(true)
    try { setDirectories(await api<Directory[]>(`/projects/${projectId}/governance/directories/tree`)) }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function runValidation() {
    const rows = validationRows.length ? validationRows : parseCsv(validationText)
    if (!rows.length) { setError('请先粘贴 CSV 或上传待校验文件'); return }
    setBusy(true); setError(''); setValidation(null)
    try { setValidation(await api<ValidationResult>(`/projects/${projectId}/governance/validations`, { method: 'POST', body: JSON.stringify({ filename: 'governance-validation.csv', rows, mode: validationMode }) })) }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function promoteValidation() {
    if (!validation || !validation.can_promote) return
    setBusy(true); setError('')
    try { const result = await api<{ count: number }>(`/projects/${projectId}/governance/validations/${validation.id}/promote`, { method: 'POST' }); setNotice(`已生成待导入批次 ${result.count} 条`); setValidation({ ...validation, status: 'promoted' }) }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function moveDirectory() {
    if (!selectedDirectory || !targetDirectory.trim()) return
    setBusy(true); setError('')
    try { const result = await api<{ changed: number }>(`/projects/${projectId}/governance/directories/move`, { method: 'POST', body: JSON.stringify({ case_ids: selectedDirectory.case_ids, directory: targetDirectory.trim() }) }); setNotice(`已迁移 ${result.changed} 条用例`); setSelectedDirectory(null); setTargetDirectory(''); await loadDirectories() }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function loadRegression() {
    setBusy(true)
    try { setRegression(await api<{ total: number; items: RegressionItem[] }>(`/projects/${projectId}/governance/regression?iteration=${encodeURIComponent(iteration)}&level=${encodeURIComponent(level)}&domain=${encodeURIComponent(domain)}`)) }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function exportRegression(format: 'xlsx' | 'xls') {
    setBusy(true); setError('')
    try { setExported(await api<ExportResult>(`/projects/${projectId}/governance/exports?format=${format}&iteration=${encodeURIComponent(iteration)}&level=${encodeURIComponent(level)}&domain=${encodeURIComponent(domain)}`, { method: 'POST' })); setNotice(`已生成 ${format.toUpperCase()} 导出文件`) }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  function switchTab(next: Tab) { setTab(next); setError(''); if (next === 'directories' && !directories.length) void loadDirectories(); if (next === 'regression') void loadRegression() }

  const counts = dashboard?.status_counts ?? {}
  const metrics = useMemo(() => Object.entries(dashboard?.metrics ?? {}).filter(([key]) => metricLabels[key]), [dashboard])
  const filteredDirectories = directories.filter(item => !directoryQuery || item.path.toLowerCase().includes(directoryQuery.toLowerCase()))
  const canWrite = !readonly && ['admin', 'manager'].includes(role)
  const directoryTree = useMemo(() => buildDirectoryTree(filteredDirectories), [filteredDirectories])
  const pickerDirectories = useMemo(() => directories.filter(item => !directoryPickerQuery || item.path.toLowerCase().includes(directoryPickerQuery.toLowerCase())), [directories, directoryPickerQuery])
  const pickerTree = useMemo(() => buildDirectoryTree(pickerDirectories), [pickerDirectories])
  function expandPickerToDirectory(path: string) {
    const parts = path.split(' - ').map(item => item.trim()).filter(Boolean)
    const prefixes = new Set<string>()
    parts.slice(0, -1).forEach((_, index) => prefixes.add(parts.slice(0, index + 1).join(' - ')))
    setExpandedPickerPaths(prefixes)
  }

  function renderPickerNode(node: DirectoryNode, depth = 0): ReactNode {
    const hasChildren = node.children.length > 0
    const expanded = expandedPickerPaths.has(node.path) || Boolean(directoryPickerQuery)
    return <div className="directory-picker-node" key={node.path}>
      <div className={`directory-picker-row ${editDirectory === node.path ? 'selected' : ''}`} style={{ '--tree-depth': depth } as CSSProperties}>
        <button type="button" className="directory-picker-toggle" aria-label={expanded ? `收起 ${node.label}` : `展开 ${node.label}`} disabled={!hasChildren} onClick={() => setExpandedPickerPaths(previous => { const next = new Set(previous); if (next.has(node.path)) next.delete(node.path); else next.add(node.path); return next })}>{hasChildren ? <ChevronRight className={expanded ? 'is-expanded' : ''} size={14} /> : <span />}</button>
        <button type="button" className="directory-picker-value" onClick={() => { setEditDirectory(node.path); setDirectoryPickerOpen(false) }}>{node.label}</button>
      </div>
      {hasChildren && expanded && <div className="directory-picker-children">{node.children.map(child => renderPickerNode(child, depth + 1))}</div>}
    </div>
  }

  function renderDirectoryNode(node: DirectoryNode, depth = 0): ReactNode {
    const directory = node.directory
    const caseCount = directory?.case_count ?? node.children.reduce((total, child) => total + (child.directory?.case_count ?? 0), 0)
    return <div className="directory-tree-node" key={node.path}>
      <motion.button layout className={`directory-tree-row ${directory && selectedDirectory?.path === directory.path ? 'selected' : ''}`} style={{ '--tree-depth': depth } as CSSProperties} onClick={() => directory && (setSelectedDirectory(directory), setTargetDirectory(directory.path))}>
        <span className="directory-tree-chevron">{node.children.length ? <ChevronRight size={13} /> : <span />}</span><strong>{node.label}</strong><span>{caseCount} 条</span><span>{directory?.frozen ? '已冻结' : directory?.whitelisted ? '白名单' : directory ? '待治理' : '目录节点'}</span>
      </motion.button>
      {node.children.length > 0 && <div className="directory-tree-children">{node.children.map(child => renderDirectoryNode(child, depth + 1))}</div>}
    </div>
  }
  return <section className="governance-page" aria-label="用例治理工作台">
    <motion.div className="governance-hero" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}>
      <div><div className="eyebrow"><ShieldCheck size={14} /> 用例治理</div><h2>用例治理工作台</h2><p>先看清资产 再决定如何处理</p></div>
      <label className="governance-upload"><UploadCloud size={18} /><span>{busy ? '读取中' : '导入监控中心快照'}</span><Input type="file" accept=".xls,.xlsx,.csv" disabled={busy || readonly} onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file) }} /></label>
    </motion.div>
    {error && <motion.div className="governance-alert is-error" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }}><AlertTriangle size={16} />{error}<button aria-label="关闭" onClick={() => setError('')}><X size={14} /></button></motion.div>}
    {notice && <motion.div className="governance-alert" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }}><Check size={16} />{notice}</motion.div>}
    <AnimatePresence>{preview && <motion.div className="governance-preview" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }}><div><strong>{preview.filename}</strong><span>{preview.count} 条待导入 · {preview.validation?.blocked ?? 0} 个阻断 · {preview.validation?.warnings ?? 0} 个提醒</span></div><div><Button variant="ghost" size="sm" onClick={() => setPreview(null)}>取消</Button><Button size="sm" disabled={busy || (preview.validation?.blocked ?? 0) > 0} onClick={() => void confirmImport()}><Check data-icon="inline-start" />确认导入</Button></div></motion.div>}</AnimatePresence>
    <div className="governance-metrics">{metrics.map(([key, value], index) => <motion.div className="governance-metric" key={key} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * .035 }}><span>{metricLabels[key]}</span><strong>{value}</strong><small>{key === 'discard_count' ? '按内容相同组分析' : '当前快照'}</small></motion.div>)}</div>
    <div className="governance-status"><span>正常 {counts.active ?? 0}</span><span>待更新 {counts.pending ?? 0}</span><span>已治理 {counts.retired ?? 0}</span><span className="governance-source">{dashboard?.batch ? `${dashboard.batch.filename} · ${dashboard.batch.sha256.slice(0, 10)}` : '尚未导入治理快照'}</span></div>
    <div className="governance-tabs">{([['audit', Archive, '体检看板'], ['duplicates', Sparkles, '重复治理'], ['validation', FileCheck2, '入库闸门'], ['directories', FolderTree, '目录治理'], ['regression', ListChecks, '回归与导出']] as const).map(([key, Icon, label]) => <Button key={key} variant={tab === key ? 'default' : 'ghost'} onClick={() => switchTab(key)}><Icon data-icon="inline-start" />{label}{key === 'duplicates' && <span className="tab-count">{dashboard?.metrics.duplicate_group_count ?? 0}</span>}</Button>)}{canWrite && <Button variant="outline" size="sm" disabled={busy} onClick={() => void retryGovernance()}><RefreshCw data-icon="inline-start" />再次治理</Button>}<Button variant="ghost" size="icon-sm" title="刷新" aria-label="刷新" onClick={() => void reload()}><RefreshCw /></Button></div>
    <AnimatePresence mode="wait">
      {tab === 'audit' && <motion.div key="audit" className="governance-audit" initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 8 }}><div className="governance-panel"><div className="panel-kicker">M1 体检快照</div><h3>本版健康度</h3><p>导入后自动生成基线与重复分析  所有原始字段保持不变</p><div className="governance-checks"><span><Check size={15} />文件校验码可追溯</span><span><Check size={15} />原始值与结论分离</span><span><Check size={15} />规则只读受保护</span></div></div><div className="governance-panel"><div className="panel-kicker">下一步</div><h3>从重复组开始</h3><p>按 A、B、C 类别查看  同名但内容不同的用例不会被合并</p><Button onClick={() => switchTab('duplicates')}><ChevronRight data-icon="inline-start" />打开重复治理</Button></div></motion.div>}
      {tab === 'duplicates' && <motion.div key="duplicates" className="governance-duplicates" initial={{ opacity: 0, x: 8 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -8 }}><div className="duplicate-toolbar"><div className="category-pills">{['all', 'A', 'B', 'C'].map(item => <button key={item} className={category === item ? 'active' : ''} onClick={() => setCategory(item)}>{item === 'all' ? '全部' : `${item} 类`}</button>)}</div><Input placeholder="搜索用例名称" value={query} onChange={event => setQuery(event.target.value)} /></div><div className="duplicate-layout"><div className="duplicate-list">{groups.map((group, index) => <motion.button layout key={group.id} className={`duplicate-card ${detail?.id === group.id ? 'selected' : ''}`} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * .025 }} onClick={() => void api<Detail>(`/projects/${projectId}/governance/duplicates/${group.id}`).then(setDetail)}><span className={`duplicate-type type-${group.category}`}>{group.category}</span><span className="duplicate-main"><strong>{group.name}</strong><small>{group.size} 条 · {group.cluster_count} 个内容相同组</small></span><span className="duplicate-count">{group.suggested_discard_count}<small>建议治理</small></span><ChevronRight size={15} /></motion.button>)}{groups.length === 0 && <div className="governance-empty">没有匹配的同名用例组</div>}</div><div className="duplicate-detail">{detail ? <motion.div key={detail.id} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}><div className="detail-head"><div><span className={`duplicate-type type-${detail.category}`}>{detail.category} 类</span><h3>{detail.name}</h3><p>{detail.size} 条用例 · 分成 {detail.cluster_count} 组相同内容</p></div>{canWrite && <div className="governance-ai-actions"><Select value={aiProvider} onValueChange={setAiProvider}><SelectTrigger className="governance-ai-provider" aria-label="选择 AI 治理提供方"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="codex_cli">Codex CLI（默认）</SelectItem><SelectItem value="local">本地模型</SelectItem><SelectItem value="remote">远程模型</SelectItem></SelectContent></Select><Button size="sm" variant="outline" disabled={busy} onClick={() => void aiGovernance(detail.id)}><Sparkles data-icon="inline-start" />AI 辅助治理</Button><Button size="sm" disabled={busy} onClick={() => void applyGroup(detail.id)}>应用建议</Button></div>}</div>{aiRawOutput && <motion.pre className="ai-raw-output" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }}><strong>模型原始输出</strong>{aiRawOutput}</motion.pre>}<div className="cluster-list">{detail.members.map(member => <motion.div layout className="cluster-item" key={member.case_id}><div className="cluster-meta"><span>{formatDecision(member.decision)}</span><b>{member.score} 分</b>{canWrite && <Button variant="ghost" size="icon-sm" aria-label="编辑用例" title="编辑用例" onClick={() => { setEditingCase(member.case_id); setEditDirectory(member.case.directory); expandPickerToDirectory(member.case.directory); setDirectoryPickerQuery(''); setEditEndpoint(endpointOptions.includes(member.case.endpoint_type) ? member.case.endpoint_type : endpointOptions[0]); if (!directories.length) void loadDirectories() }}><Edit3 size={14} /></Button>}</div><strong>{member.case.name}</strong><small title={member.case.directory}>{member.case.platform_id} · {lastDirectorySegment(member.case.directory)} · {member.case.endpoint_type || '未设置端类型'}</small><p>{member.case.steps || '步骤为空'}</p><p className="expected">{member.case.expected || '预期为空'}</p><small>建议理由：{member.reason || '暂无理由'} · 创建 {formatDateTime(member.case.created_at)} · 最后修改 {formatDateTime(member.case.updated_at)}</small>{editingCase === member.case_id && <motion.div className="governance-inline-editor" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }}><div className="directory-picker">
  <button type="button" className={`directory-picker-trigger ${directoryPickerOpen ? 'is-open' : ''}`} onClick={() => setDirectoryPickerOpen(value => !value)} aria-expanded={directoryPickerOpen}>{editDirectory ? lastDirectorySegment(editDirectory) : '选择用例目录'}<ChevronRight size={15} /></button>
  {directoryPickerOpen && <motion.div className="directory-picker-popover" initial={{ opacity: 0, y: -4, scale: .99 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -4 }}>
    <Input value={directoryPickerQuery} onChange={event => setDirectoryPickerQuery(event.target.value)} placeholder="搜索目录" aria-label="搜索目录" />
    <div className="directory-picker-tree">{pickerTree.length ? pickerTree.map(node => renderPickerNode(node)) : <span className="directory-picker-empty">没有匹配目录</span>}</div>
  </motion.div>}
</div><Select value={editEndpoint} onValueChange={setEditEndpoint}><SelectTrigger aria-label="选择端类型"><SelectValue placeholder="选择端类型" /></SelectTrigger><SelectContent>{endpointOptions.map(endpoint => <SelectItem key={endpoint} value={endpoint}>{endpoint}</SelectItem>)}</SelectContent></Select><Button size="sm" disabled={busy} onClick={() => void saveCaseEdit(member.case_id)}>保存</Button><Button size="sm" variant="ghost" onClick={() => setEditingCase(null)}>取消</Button></motion.div>}</motion.div>)}</div></motion.div> : <div className="governance-empty"><Sparkles size={24} /><span>选择一个重复组查看内容相同的用例</span></div>}</div></div></motion.div>}
      {tab === 'validation' && <motion.div key="validation" className="governance-workspace" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}><div className="workspace-heading"><div><div className="panel-kicker">M3 入库质量门</div><h3>新增用例入库闸门</h3><p>逐行校验必填字段 目录 白名单和标签  阻断项未清零不可继续</p></div><select value={validationMode} onChange={event => setValidationMode(event.target.value as 'new' | 'legacy')}><option value="new">新增用例严格模式</option><option value="legacy">历史数据宽松模式</option></select></div><div className="validation-input"><textarea value={validationText} onChange={event => { setValidationText(event.target.value); setValidationRows([]) }} placeholder={`粘贴 CSV 内容\n${csvHeaders.join(',')}`} /><label className="inline-file"><UploadCloud size={14} />上传 CSV<input type="file" accept=".csv" onChange={event => { const file = event.target.files?.[0]; if (!file) return; void file.text().then(value => { setValidationText(value); setValidationRows(parseCsv(value)) }) }} /></label><Button disabled={busy || readonly} onClick={() => void runValidation()}><FileCheck2 data-icon="inline-start" />开始校验</Button></div>{validation && <motion.div className="validation-result" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}><div className="validation-summary"><span className={validation.blocked ? 'is-blocked' : 'is-ok'}>{validation.blocked} 个阻断</span><span>{validation.warnings} 个提醒</span><Button size="sm" disabled={!canWrite || busy || !validation.can_promote} onClick={() => void promoteValidation()}>生成待导入批次</Button></div><div className="issue-columns"><div><h4>阻断项</h4>{validation.errors.map(issue => <p key={`${issue.row}-${issue.code}`}>{issue.message}</p>)}{!validation.errors.length && <small>阻断项已清零</small>}</div><div><h4>提醒项</h4>{validation.warning_items.map(issue => <p key={`${issue.row}-${issue.code}`}>{issue.message}</p>)}{!validation.warning_items.length && <small>没有提醒</small>}</div></div></motion.div>}</motion.div>}
      {tab === 'directories' && <motion.div key="directories" className="governance-workspace" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}><div className="workspace-heading"><div><div className="panel-kicker">M4 目录治理</div><h3>目录白名单与迁移</h3><p>冻结目录不可作为新增目标 迁移保持平台 ID 与历史记录</p></div><Button variant="ghost" size="sm" onClick={() => void loadDirectories()}><RefreshCw data-icon="inline-start" />刷新目录</Button></div><div className="directory-toolbar"><Input placeholder="筛选目录" value={directoryQuery} onChange={event => setDirectoryQuery(event.target.value)} /><span>{filteredDirectories.length} 个目录 · {filteredDirectories.filter(item => item.whitelisted).length} 个白名单</span></div><div className="directory-tree" aria-label="用例目录树">{directoryTree.length ? directoryTree.map(node => renderDirectoryNode(node)) : <div className="governance-empty">暂无目录数据</div>}</div>{selectedDirectory && <motion.div className="directory-editor" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}><div><strong>{selectedDirectory.path}</strong><small>将迁移 {selectedDirectory.case_count} 条用例</small></div><Input value={targetDirectory} onChange={event => setTargetDirectory(event.target.value)} placeholder="目标白名单目录" /><Button disabled={!canWrite || busy || !targetDirectory.trim() || selectedDirectory.frozen} onClick={() => void moveDirectory()}>提交迁移</Button></motion.div>}</motion.div>}
      {tab === 'regression' && <motion.div key="regression" className="governance-workspace" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}><div className="workspace-heading"><div><div className="panel-kicker">M3 回归清单</div><h3>回归清单与导出</h3><p>按迭代 等级和功能域筛选当前在用用例 导出固定 Tcase_list</p></div><div className="export-actions"><Button size="sm" onClick={() => void exportRegression('xlsx')} disabled={busy}><Download data-icon="inline-start" />导出 XLSX</Button><Button size="sm" variant="outline" onClick={() => void exportRegression('xls')} disabled={busy}><Download data-icon="inline-start" />导出 XLS</Button></div></div><div className="regression-filters"><Input placeholder="迭代日期 20261021" value={iteration} onChange={event => setIteration(event.target.value)} /><Input placeholder="等级 1,2" value={level} onChange={event => setLevel(event.target.value)} /><Input placeholder="功能域关键词" value={domain} onChange={event => setDomain(event.target.value)} /><Button variant="ghost" onClick={() => void loadRegression()}><RefreshCw data-icon="inline-start" />刷新清单</Button></div><div className="regression-summary"><strong>{regression.total}</strong><span>条符合筛选的在用用例</span>{exported && <span className={exported.selfcheck.readback ? 'is-ok' : 'is-blocked'}>最近导出 {exported.selfcheck.readback ? '回读通过' : '回读失败'}</span>}</div>{exported && <motion.div className="export-selfcheck" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }}><div><strong>{exported.filename}</strong><span>{exported.selfcheck.format.toUpperCase()} · {exported.rows} 行 · 工作表 {exported.selfcheck.sheet}</span></div><a href={exported.download} download><Download size={15} />下载文件</a></motion.div>}<div className="regression-table"><div className="regression-head"><span>ID</span><span>名称</span><span>目录</span><span>等级</span></div>{regression.items.slice(0, 120).map((item, index) => <motion.div className="regression-row" key={`${item['测试用例ID']}-${index}`} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: Math.min(index, 20) * .01 }}><span>{item['测试用例ID']}</span><strong>{item['用例名称']}</strong><span>{item['用例目录']}</span><span>{item['用例等级']}</span></motion.div>)}{!regression.items.length && <div className="governance-empty">点击刷新生成回归清单</div>}</div></motion.div>}
    </AnimatePresence>
  </section>
}
