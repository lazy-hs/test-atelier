import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { AlertCircle, Plus, RefreshCw, Trash2, UserPlus, UsersRound } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { api } from '@/lib/api'

type Role = 'admin' | 'manager' | 'reviewer' | 'reader'
type Member = { id: number; username: string; role: Role }
type Event = { id: number; member: string; actor: string; action: string; before_role: Role | null; after_role: Role | null; created_at: string }
const labels: Record<Role, string> = { admin: '全局管理员', manager: '项目管理员', reviewer: '评审员', reader: '只读成员' }

function eventText(event: Event) {
  if (event.action === 'added') return `将 ${event.member} 加入项目 · ${labels[event.after_role ?? 'reviewer']}`
  if (event.action === 'removed') return `将 ${event.member} 移出项目`
  return `调整 ${event.member} · ${labels[event.before_role ?? 'reviewer']} → ${labels[event.after_role ?? 'reviewer']}`
}

export function TeamPage({ projectId, role, userId }: { projectId: number; role: Role; userId: number }) {
  const base = `/projects/${projectId}`
  const canManage = role === 'admin' || role === 'manager'
  const [members, setMembers] = useState<Member[]>([])
  const [events, setEvents] = useState<Event[]>([])
  const [dialog, setDialog] = useState<'existing' | 'new' | null>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [newRole, setNewRole] = useState<Exclude<Role, 'admin'>>('reviewer')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const refresh = useCallback(async () => {
    const [people, history] = await Promise.all([
      api<Member[]>(`${base}/members`),
      canManage ? api<Event[]>(`${base}/member-events`) : Promise.resolve([]),
    ])
    setMembers(people); setEvents(history)
  }, [base, canManage])
  useEffect(() => {
    let active = true
    setLoading(true)
    void refresh().catch(cause => { if (active) setError(cause.message) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [refresh])
  useEffect(() => { if (!notice) return; const timer = window.setTimeout(() => setNotice(''), 4000); return () => window.clearTimeout(timer) }, [notice])

  function open(mode: 'existing' | 'new') {
    setDialog(mode); setUsername(''); setPassword(''); setNewRole('reviewer'); setError('')
  }
  async function add(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    try {
      const payload = dialog === 'new' ? { username: username.trim(), password, role: newRole } : { username: username.trim(), role: newRole }
      const path = dialog === 'new' ? `${base}/members/new` : `${base}/members`
      await api(path, { method: 'POST', body: JSON.stringify(payload) })
      setDialog(null); await refresh(); setNotice(dialog === 'new' ? '账号已创建并加入项目' : '成员已加入项目')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function changeRole(member: Member, next: Exclude<Role, 'admin'>) {
    if (member.role === next) return
    setBusy(true); setError('')
    try {
      await api(`${base}/members/${member.id}`, { method: 'PATCH', body: JSON.stringify({ role: next }) })
      await refresh(); setNotice(`${member.username} 的权限已更新`)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function remove(member: Member) {
    if (!window.confirm(`将 ${member.username} 移出当前项目？账号和其他项目的数据不会删除。`)) return
    setBusy(true); setError('')
    try {
      await api(`${base}/members/${member.id}`, { method: 'DELETE' })
      await refresh(); setNotice(`${member.username} 已移出项目`)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }

  return <div className="team-page">
    {error && dialog === null && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    <section className="team-section" aria-labelledby="team-members-title">
      <div className="team-section-heading"><div><h2 id="team-members-title">项目成员</h2><p>{loading ? '正在读取' : `${members.length} 位成员`}</p></div>
        <div className="team-actions">{canManage && <Button variant="outline" size="sm" onClick={() => open('existing')}><UserPlus data-icon="inline-start" />添加已有账号</Button>}
          {role === 'admin' && <Button size="sm" onClick={() => open('new')}><Plus data-icon="inline-start" />创建账号</Button>}
          <Button variant="ghost" size="icon-sm" aria-label="刷新成员" title="刷新成员" onClick={() => void refresh().catch(cause => setError(cause.message))} disabled={busy}><RefreshCw /></Button>
        </div></div>
      <div className="team-table-head"><span>成员</span><span>项目权限</span><span>操作</span></div>
      <AnimatePresence initial={false}>{members.map(member => {
        const editable = canManage && member.role !== 'admin' && (role === 'admin' || member.role !== 'manager') && member.id !== userId
        return <motion.div layout key={member.id} className="team-member-row" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, x: -12 }}>
          <span className="team-identity"><span className="team-avatar" aria-hidden="true">{member.username.slice(0, 1).toUpperCase()}</span><strong title={member.username}>{member.username}</strong>{member.id === userId && <Badge variant="secondary">我</Badge>}</span>
          <span>{editable ? <Select value={member.role} onValueChange={value => void changeRole(member, value as Exclude<Role, 'admin'>)} disabled={busy}>
            <SelectTrigger aria-label={`${member.username} 的项目权限`}><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
              {role === 'admin' && <SelectItem value="manager">项目管理员</SelectItem>}
              <SelectItem value="reviewer">评审员</SelectItem><SelectItem value="reader">只读成员</SelectItem>
            </SelectGroup></SelectContent></Select> : <Badge variant="secondary">{labels[member.role]}</Badge>}</span>
          <span className="team-row-action">{editable && <Button variant="ghost" size="icon-sm" aria-label={`移除 ${member.username}`} title={`移除 ${member.username}`} onClick={() => void remove(member)} disabled={busy}><Trash2 /></Button>}</span>
        </motion.div>
      })}</AnimatePresence>
      {!loading && members.length === 0 && <div className="team-empty"><UsersRound size={20} />暂无项目成员</div>}
    </section>
    {canManage && <section className="team-section team-history" aria-labelledby="team-history-title"><div className="team-section-heading"><div><h2 id="team-history-title">成员变更</h2><p>最近 30 条</p></div></div>
      {events.map(item => <div className="team-event" key={item.id}><span>{eventText(item)}</span><small>{item.actor} · {new Date(item.created_at).toLocaleString('zh-CN')}</small></div>)}
      {!loading && events.length === 0 && <div className="team-empty">还没有成员变更记录</div>}
    </section>}
    <Dialog open={dialog !== null} onOpenChange={value => { if (!value) setDialog(null) }}><DialogContent><DialogHeader>
      <DialogTitle>{dialog === 'new' ? '创建成员账号' : '添加已有账号'}</DialogTitle>
      <DialogDescription>{dialog === 'new' ? '创建后立即加入当前项目' : '输入已存在的准确用户名并指定项目权限'}</DialogDescription>
    </DialogHeader><form className="team-form" onSubmit={event => void add(event)}>
      <label>用户名<Input value={username} onChange={event => setUsername(event.target.value)} minLength={2} maxLength={80} autoComplete="username" required /></label>
      {dialog === 'new' && <label>初始密码<Input type="password" value={password} onChange={event => setPassword(event.target.value)} minLength={12} autoComplete="new-password" required /></label>}
      <label>项目权限<Select value={newRole} onValueChange={value => setNewRole(value as Exclude<Role, 'admin'>)}><SelectTrigger aria-label="新成员项目权限"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
        {role === 'admin' && <SelectItem value="manager">项目管理员</SelectItem>}
        <SelectItem value="reviewer">评审员</SelectItem><SelectItem value="reader">只读成员</SelectItem>
      </SelectGroup></SelectContent></Select></label>
      {error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
      <DialogFooter><Button type="submit" disabled={busy}>{busy ? '处理中…' : dialog === 'new' ? '创建并加入' : '加入项目'}</Button></DialogFooter>
    </form></DialogContent></Dialog>
  </div>
}
