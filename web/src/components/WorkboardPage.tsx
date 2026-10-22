import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { CalendarClock, Check, CircleDot, ClipboardList, FileCheck2, FolderKanban, ListTodo, Plus, Trash2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { api, type Project, type WorkItem, type WorkItemKind, type WorkItemStatus, type WorkboardResponse } from '@/lib/api'

const kindLabels: Record<WorkItemKind, string> = { requirement: '跟进需求', tested_requirement: '已完成测试的需求', task: '任务', test_plan: '测试计划', other: '其他任务' }
const statusLabels: Record<WorkItemStatus, string> = { todo: '待开始', in_progress: '进行中', done: '已完成' }
const kindIcons = { requirement: FolderKanban, tested_requirement: FileCheck2, task: ListTodo, test_plan: ClipboardList, other: CircleDot }

function dateLabel(value: string | null) {
  if (!value) return '未设置截止日期'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(date)
}

export function WorkboardPage({ projects }: { projects: Project[] }) {
  const [data, setData] = useState<WorkboardResponse | null>(null)
  const [kind, setKind] = useState<WorkItemKind>('requirement')
  const [status, setStatus] = useState<WorkItemStatus>('todo')
  const [quickRecord, setQuickRecord] = useState('')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [startAt, setStartAt] = useState('')
  const [dueAt, setDueAt] = useState('')
  const [projectId, setProjectId] = useState('none')
  const [filter, setFilter] = useState<'all' | WorkItemStatus>('all')
  const [editingSchedule, setEditingSchedule] = useState<number | null>(null)
  const [editStartAt, setEditStartAt] = useState('')
  const [editDueAt, setEditDueAt] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const reload = () => api<WorkboardResponse>('/workboard').then(setData).catch(cause => setError((cause as Error).message))
  useEffect(() => { void reload() }, [])
  const items = useMemo(() => (data?.items ?? []).filter(item => filter === 'all' || item.status === filter), [data, filter])

  function parseQuickRecord(value: string) {
    const lines = value.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
    return { title: lines[0] ?? '', description: lines.slice(1).join('\n') }
  }
  function changeQuickRecord(value: string) {
    setQuickRecord(value)
    const parsed = parseQuickRecord(value)
    setTitle(parsed.title)
    setDescription(parsed.description)
  }

  async function addItem(event: FormEvent) {
    event.preventDefault()
    const parsed = parseQuickRecord(quickRecord)
    const finalTitle = (title.trim() || parsed.title).trim()
    const finalDescription = description.trim() || parsed.description
    if (!finalTitle) return
    setBusy(true); setError('')
    try {
      await api<WorkItem>('/workboard', { method: 'POST', body: JSON.stringify({ kind, status, title: finalTitle, description: finalDescription, start_at: startAt || null, due_at: dueAt || null, project_id: projectId === 'none' ? null : Number(projectId) }) })
      setQuickRecord(''); setTitle(''); setDescription(''); setStartAt(''); setDueAt(''); setKind('requirement'); setStatus('todo'); setProjectId('none'); await reload()
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function updateStatus(item: WorkItem, next: WorkItemStatus) {
    setError('')
    try { await api<WorkItem>(`/workboard/${item.id}`, { method: 'PATCH', body: JSON.stringify({ status: next }) }); await reload() }
    catch (cause) { setError((cause as Error).message) }
  }
  async function remove(item: WorkItem) {
    try { await api(`/workboard/${item.id}`, { method: 'DELETE' }); await reload() }
    catch (cause) { setError((cause as Error).message) }
  }
  function beginScheduleEdit(item: WorkItem) {
    setEditingSchedule(item.id)
    setEditStartAt(item.start_at ?? '')
    setEditDueAt(item.due_at ?? '')
  }
  async function saveSchedule(item: WorkItem) {
    setError('')
    try {
      await api<WorkItem>(`/workboard/${item.id}`, { method: 'PATCH', body: JSON.stringify({ start_at: editStartAt || null, due_at: editDueAt || null }) })
      setEditingSchedule(null)
      await reload()
    } catch (cause) { setError((cause as Error).message) }
  }

  return <div className="workboard-page">
    <section className="workboard-hero">
      <div><span className="workboard-kicker">PERSONAL WORKSPACE</span><h2>我的工作看板</h2><p>把需求跟进 测试计划和日常任务放在一个节奏里</p></div>
      <div className="workboard-hero-mark"><CalendarClock size={22} /><span>{data?.summary.active ?? 0} 项进行中</span></div>
    </section>
    <section className="workboard-summary" aria-label="工作概览">
      {[['全部事项', data?.summary.total ?? 0, 'total'], ['跟进需求', data?.summary.followed_requirements ?? 0, 'requirement'], ['已完成测试', data?.summary.tested_requirements ?? 0, 'tested_requirement'], ['用例输出', data?.summary.test_case_outputs ?? 0, 'outputs']].map(([label, value, tone], index) => <motion.div key={label} className={`workboard-stat tone-${tone}`} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * .05 }} whileHover={{ y: -2 }}><span>{label}</span><strong>{value}</strong><small>{tone === 'outputs' ? '已发布与已入库' : tone === 'total' ? '跨项目个人事项' : '持续更新'}</small></motion.div>)}
    </section>
    <div className="workboard-layout">
      <motion.form className="workboard-create" onSubmit={addItem} initial={{ opacity: 0, x: -10 }} animate={{ opacity: 1, x: 0 }}>
        <div className="workboard-section-heading"><div><h3>记录一项工作</h3><p>先写下来 再让进度变得可见</p></div><Plus size={17} /></div>
        <label className="workboard-quick-field">快速记录<textarea value={quickRecord} onChange={event => changeQuickRecord(event.target.value)} placeholder={'粘贴一段内容 自动取第一行作为标题\n例如 【监控中心】自研IPC配网SDK替换\nhttps://www.tapd.cn/...'} rows={3} /></label>
        <label>事项标题<Input value={title} onChange={event => setTitle(event.target.value)} placeholder="例如 跟进支付链路验收" required /></label>
        <label>事项类型<Select value={kind} onValueChange={value => setKind(value as WorkItemKind)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{Object.entries(kindLabels).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></label>
        <label>关联项目<Select value={projectId} onValueChange={setProjectId}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">个人事项</SelectItem>{projects.map(project => <SelectItem key={project.id} value={String(project.id)}>{project.name}</SelectItem>)}</SelectContent></Select></label>
        <div className="workboard-form-row"><label>状态<Select value={status} onValueChange={value => setStatus(value as WorkItemStatus)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{Object.entries(statusLabels).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></label><label>开始时间<Input type="datetime-local" value={startAt} onChange={event => setStartAt(event.target.value)} /></label></div>
        <label>截止时间<Input type="datetime-local" value={dueAt} onChange={event => setDueAt(event.target.value)} /></label>
        <label>备注<textarea value={description} onChange={event => setDescription(event.target.value)} placeholder="补充上下文 可留空" rows={3} /></label>
        {error && <div className="workboard-error" role="alert">{error}<button type="button" onClick={() => setError('')} aria-label="关闭错误"><X size={14} /></button></div>}
        <Button type="submit" disabled={busy}><Plus data-icon="inline-start" />{busy ? '保存中' : '加入看板'}</Button>
      </motion.form>
      <section className="workboard-list-section">
        <div className="workboard-section-heading"><div><h3>我的事项</h3><p>按状态切换 处理完一项就勾掉一项</p></div><div className="workboard-filters">{(['all', 'todo', 'in_progress', 'done'] as const).map(value => <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>{value === 'all' ? '全部' : statusLabels[value]}</button>)}</div></div>
        <div className="workboard-items"><AnimatePresence mode="popLayout">{items.map((item, index) => { const Icon = kindIcons[item.kind]; return <motion.article layout key={item.id} className={`workboard-item status-${item.status}`} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, scale: .96 }} transition={{ delay: index * .035 }}>
          <button className="workboard-check" onClick={() => void updateStatus(item, item.status === 'done' ? 'todo' : 'done')} aria-label={item.status === 'done' ? '标记未完成' : '标记完成'}><Check size={15} /></button>
          <div className="workboard-item-icon"><Icon size={16} /></div><div className="workboard-item-copy"><div><strong>{item.title}</strong><Select value={item.status} onValueChange={value => void updateStatus(item, value as WorkItemStatus)}><SelectTrigger className={`workboard-status status-${item.status}`} aria-label={`${item.title} 状态`}><SelectValue /></SelectTrigger><SelectContent>{Object.entries(statusLabels).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></div><p>{item.description || '暂无备注'}</p><small>{item.project_name || '个人事项'} · {item.start_at ? `开始 ${dateLabel(item.start_at)}` : '未设置开始时间'} · {item.due_at ? `截止 ${dateLabel(item.due_at)}` : '未设置截止时间'}</small>{editingSchedule === item.id && <motion.div className="workboard-schedule-editor" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}><label>开始<Input type="datetime-local" value={editStartAt} onChange={event => setEditStartAt(event.target.value)} /></label><label>截止<Input type="datetime-local" value={editDueAt} onChange={event => setEditDueAt(event.target.value)} /></label><Button size="sm" onClick={() => void saveSchedule(item)}>保存</Button><Button size="sm" variant="ghost" onClick={() => setEditingSchedule(null)}>取消</Button></motion.div>}</div>
          <div className="workboard-item-actions"><button className="workboard-schedule" onClick={() => editingSchedule === item.id ? setEditingSchedule(null) : beginScheduleEdit(item)} aria-label={`编辑 ${item.title} 时间`}><CalendarClock size={15} /></button><button className="workboard-delete" onClick={() => void remove(item)} aria-label={`删除 ${item.title}`}><Trash2 size={15} /></button></div>
        </motion.article> })}</AnimatePresence>{items.length === 0 && <motion.div className="workboard-empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }}><ClipboardList size={22} /><p>这里还没有事项</p><span>从左侧记录第一项工作</span></motion.div>}</div>
      </section>
    </div>
  </div>
}

