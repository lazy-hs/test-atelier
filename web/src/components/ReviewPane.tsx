import { useEffect, useState } from 'react'
import { MessageSquare, RefreshCw, Save, Trash2, UserRoundCheck, UserRoundX, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { api, formatDateTime, type CaseActivity, type CaseDetail } from '@/lib/api'
import { SuggestionPanel } from '@/components/SuggestionPanel'
import type { DimensionKey, TaxonomyVersion } from '@/lib/api'

type Member = { id: number; username: string; role: string }
type Props = {
  projectId: number; userId: number; role: string; item: CaseDetail; readonly: boolean; saving: boolean
  onClose: () => void; onSave: (level: string, smoke: string, reason: string) => Promise<void>
  onDelete: () => void
  onAssign: (assigneeId: number | null) => Promise<void>
  onSuggestionDecided: (value: CaseDetail) => Promise<void>; suggestionRevision: number
}

function time(value: string) { return formatDateTime(value) }

export function ReviewPane({ projectId, userId, role, item, readonly, saving, onClose, onSave, onAssign,
  onDelete, onSuggestionDecided, suggestionRevision }: Props) {
  const [tab, setTab] = useState('review')
  const [level, setLevel] = useState(item.final_level || item.level || 'pending')
  const [smoke, setSmoke] = useState((item.final_smoke ?? item.smoke) || 'pending')
  const [reason, setReason] = useState('')
  const [members, setMembers] = useState<Member[]>([])
  const [activity, setActivity] = useState<CaseActivity>({ events: [], comments: [] })
  const [comment, setComment] = useState('')
  const [activityError, setActivityError] = useState('')
  const [commentBusy, setCommentBusy] = useState(false)
  const [finalLabels, setFinalLabels] = useState<Record<DimensionKey, string | null> | null>(null)
  const base = `/projects/${projectId}`

  useEffect(() => {
    let active = true
    void Promise.all([
      api<Member[]>(`${base}/members`), api<CaseActivity>(`${base}/cases/${item.id}/activity`),
    ]).then(([people, entries]) => { if (active) { setMembers(people); setActivity(entries) } })
      .catch(cause => { if (active) setActivityError(cause.message) })
    return () => { active = false }
  }, [base, item.id, item.version])
  useEffect(() => {
    if (!item.final_taxonomy_version_id) { setFinalLabels(null); return }
    let active = true
    void api<TaxonomyVersion>(`${base}/taxonomy/versions/${item.final_taxonomy_version_id}`).then(version => {
      if (!active) return
      setFinalLabels(Object.fromEntries((['business_module', 'business_scenario', 'test_dimension', 'endpoint_type'] as DimensionKey[])
        .map(key => [key, version.definitions[key].find(entry => entry.id === item.final_categories[key])?.name ?? null])) as Record<DimensionKey, string | null>)
    }).catch(cause => { if (active) setActivityError(cause.message) })
    return () => { active = false }
  }, [base, item.final_taxonomy_version_id, item.final_categories])

  async function refreshActivity() {
    setActivity(await api<CaseActivity>(`${base}/cases/${item.id}/activity`))
  }

  async function addComment() {
    setCommentBusy(true); setActivityError('')
    try {
      await api(`${base}/cases/${item.id}/comments`, { method: 'POST', body: JSON.stringify({ text: comment.trim() }) })
      await refreshActivity(); setComment('')
    } catch (cause) { setActivityError((cause as Error).message) } finally { setCommentBusy(false) }
  }

  const canManage = role === 'admin' || role === 'manager'
  const assignedToSelf = item.assignee_id === userId
  return <aside className="review-pane" aria-label="用例审核详情">
    <header className="pane-heading"><div><span>用例详情 / {item.original_id}</span><h2>{item.title}</h2></div><div className="pane-heading-actions">{!readonly && <Button variant="destructive" size="icon" aria-label="删除用例" title="删除用例" onClick={onDelete}><Trash2 /></Button>}<Button variant="ghost" size="icon" aria-label="关闭详情" title="关闭详情" onClick={onClose}><X /></Button></div></header>
    <div className="pane-scroll">
      <div className="assignment-strip"><div><strong>负责人</strong><span>{item.assignee ?? '未分派'}</span></div>
        {canManage ? <Select value={String(item.assignee_id ?? 'none')} onValueChange={value => void onAssign(value === 'none' ? null : Number(value))} disabled={saving}>
          <SelectTrigger aria-label="分派负责人"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
            <SelectItem value="none">未分派</SelectItem>{members.filter(member => member.role !== 'reader').map(member => <SelectItem key={member.id} value={String(member.id)}>{member.username}</SelectItem>)}
          </SelectGroup></SelectContent></Select> : !readonly && (item.assignee_id === null || assignedToSelf) ?
          <Button variant="outline" size="sm" onClick={() => void onAssign(assignedToSelf ? null : userId)} disabled={saving}>
            {assignedToSelf ? <UserRoundX data-icon="inline-start" /> : <UserRoundCheck data-icon="inline-start" />}{assignedToSelf ? '释放' : '认领'}
          </Button> : null}
      </div>
      <Tabs value={tab} onValueChange={setTab} className="review-tabs"><TabsList variant="line" aria-label="详情视图"><TabsTrigger value="review">审核</TabsTrigger><TabsTrigger value="suggestions">建议</TabsTrigger><TabsTrigger value="activity">讨论与记录</TabsTrigger></TabsList>
        <TabsContent value="review">
          <div className="section-caption">原始信息 <span>导入内容不可修改</span></div>
          <div className="review-meta case-timestamps"><span>创建时间 {item.created_at ? time(item.created_at) : '未知'}</span><span>最后修改 {item.updated_at ? time(item.updated_at) : '未知'}</span></div>
          <dl className="original-fields">{item.headers.map((header, index) => <div key={`${header}-${index}`}><dt>{header}</dt><dd>{item.original[index] || <span className="empty-value">空值</span>}</dd></div>)}</dl>
          <div className="section-caption review-caption">人工审核 <span>与原始字段分开保存</span></div>
          <div className="review-controls">
            <label>最终等级<Select value={level} onValueChange={setLevel} disabled={readonly || (role === 'reviewer' && item.assignee_id !== null && !assignedToSelf)}><SelectTrigger aria-label="最终等级"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="pending">待判定</SelectItem>{['1', '2', '3', '4'].map(value => <SelectItem value={value} key={value}>等级 {value}</SelectItem>)}</SelectGroup></SelectContent></Select></label>
            <label>最终冒烟<Select value={smoke} onValueChange={setSmoke} disabled={readonly || (role === 'reviewer' && item.assignee_id !== null && !assignedToSelf)}><SelectTrigger aria-label="最终冒烟"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="pending">待判定</SelectItem><SelectItem value="是">是</SelectItem><SelectItem value="否">否</SelectItem></SelectGroup></SelectContent></Select></label>
          </div>
          <label className="reason-label">审核理由<Textarea value={reason} onChange={event => setReason(event.target.value)} maxLength={1000} disabled={readonly || (role === 'reviewer' && item.assignee_id !== null && !assignedToSelf)} placeholder="说明判断依据或修改原因" /></label>
          {item.reviewer && <p className="review-meta">上次审核 {item.reviewer} · {item.reviewed_at ? time(item.reviewed_at) : ''}</p>}
          <div className="section-caption review-caption">最终分类 <span>{item.final_taxonomy_version_id ? `版本 #${item.final_taxonomy_version_id}` : '未归类'}</span></div>
          <dl className="original-fields">{([['business_module', '业务模块'], ['business_scenario', '业务场景'], ['test_dimension', '测试维度'], ['endpoint_type', '端类型']] as [DimensionKey, string][]).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{finalLabels?.[key] ?? <span className="empty-value">待归类</span>}</dd></div>)}</dl>
        </TabsContent>
        <TabsContent value="suggestions"><SuggestionPanel projectId={projectId} item={item} canDecide={!readonly && (canManage || item.assignee_id === null || assignedToSelf)} refreshSignal={suggestionRevision} onApplied={value => onSuggestionDecided({ ...item, ...value })} /></TabsContent>
        <TabsContent value="activity">
          <div className="section-caption">讨论 <span className="activity-caption-actions">{activity.comments.length} 条<Button size="icon-sm" variant="ghost" aria-label="刷新讨论与记录" title="刷新讨论与记录" onClick={() => void refreshActivity().catch(cause => setActivityError(cause.message))}><RefreshCw /></Button></span></div>
          {!readonly && <div className="comment-compose"><Textarea aria-label="讨论内容" value={comment} onChange={event => setComment(event.target.value)} maxLength={2000} placeholder="记录澄清、审核依据或需要同事确认的问题" /><Button size="sm" disabled={commentBusy || !comment.trim()} onClick={() => void addComment()}><MessageSquare data-icon="inline-start" />发表</Button></div>}
          {activityError && <p className="error-line" role="alert">{activityError}</p>}
          {activity.comments.map(entry => <div className="activity-entry" key={`comment-${entry.id}`}><div><strong>{entry.actor}</strong><time>{time(entry.created_at)}</time></div><p>{entry.text}</p></div>)}
          {activity.comments.length === 0 && <p className="activity-empty">暂无讨论</p>}
          <div className="section-caption review-caption">变更记录 <span>最近 {activity.events.length} 条</span></div>
          {activity.events.map(entry => <div className="activity-entry" key={`event-${entry.id}`}><div><strong>{entry.action === 'assignment' ? '负责人变更' : entry.action === 'suggestion' ? '建议采纳' : '人工审核'} · {entry.actor}</strong><time>{time(entry.created_at)}</time></div><p>{entry.reason}</p>
            {entry.action === 'review' && <small>等级 {entry.before.level || '—'} → {entry.after.level || '—'} · 冒烟 {entry.before.smoke || '待判定'} → {entry.after.smoke || '待判定'}</small>}
          </div>)}
          {activity.events.length === 0 && <p className="activity-empty">暂无变更记录</p>}
        </TabsContent>
      </Tabs>
    </div>
    {!readonly && tab === 'review' && !(role === 'reviewer' && item.assignee_id !== null && !assignedToSelf) && <footer className="pane-footer"><Button disabled={saving || level === 'pending' || !reason.trim()} onClick={() => onSave(level, smoke === 'pending' ? '' : smoke, reason.trim())}><Save data-icon="inline-start" />{saving ? '保存中…' : '保存审核'}</Button></footer>}
  </aside>
}
