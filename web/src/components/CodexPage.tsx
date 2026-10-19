import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { Bot, ChevronRight, CircleStop, Command, Loader2, Plus, RefreshCw, Send, Terminal, UserRound } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { api } from '@/lib/api'

type CodexMessage = { role: 'user' | 'assistant' | 'system'; content: string; created_at?: string }
type CodexSession = { id: string; title?: string; status?: string; model?: string; working_directory?: string; created_at?: string; updated_at?: string; messages?: CodexMessage[] }

export function CodexPage({ projectId, readonly }: { projectId: number; readonly: boolean }) {
  const base = `/projects/${projectId}/codex/sessions`
  const [sessions, setSessions] = useState<CodexSession[]>([])
  const [selectedId, setSelectedId] = useState<string>('')
  const [prompt, setPrompt] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const selected = useMemo(() => sessions.find(session => session.id === selectedId) ?? null, [sessions, selectedId])
  const load = useCallback(async () => {
    setLoading(true); setError('')
    try {
      const data = await api<CodexSession[] | { sessions?: CodexSession[] }>(base)
      const rows = Array.isArray(data) ? data : data.sessions ?? []
      setSessions(rows)
      setSelectedId(current => current && rows.some(item => item.id === current) ? current : rows[0]?.id ?? '')
    } catch (cause) { setError((cause as Error).message) } finally { setLoading(false) }
  }, [base])
  useEffect(() => { void load() }, [load])

  async function createSession() {
    if (readonly || busy) return
    setBusy(true); setError('')
    try {
      const created = await api<CodexSession>(base, { method: 'POST', body: JSON.stringify({ title: '新建 Codex 会话' }) })
      setSessions(previous => [created, ...previous.filter(item => item.id !== created.id)])
      setSelectedId(created.id); setPrompt(''); setNotice('会话已创建')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    const text = prompt.trim()
    if (!selected || !text || readonly || busy) return
    setBusy(true); setError(''); setNotice('')
    const optimistic: CodexMessage = { role: 'user', content: text, created_at: new Date().toISOString() }
    setSessions(previous => previous.map(item => item.id === selected.id ? { ...item, messages: [...(item.messages ?? []), optimistic], status: 'running' } : item))
    setPrompt('')
    try {
      const result = await api<CodexSession>(`${base}/${encodeURIComponent(selected.id)}/turn`, { method: 'POST', body: JSON.stringify({ prompt: text }) })
      setSessions(previous => previous.map(item => item.id === selected.id ? result : item))
    } catch (cause) {
      setError((cause as Error).message)
      setSessions(previous => previous.map(item => item.id === selected.id ? { ...item, status: 'failed' } : item))
    } finally { setBusy(false) }
  }

  const messages = selected?.messages ?? []
  return <div className="codex-page">
    <div className="codex-toolbar"><div className="codex-heading"><span className="codex-icon"><Terminal size={18} /></span><div><strong>Codex 工作台</strong><span>嵌入 Codex CLI 会话 在项目内直接执行任务</span></div></div><div className="codex-toolbar-actions"><Button variant="outline" size="sm" onClick={() => void load()} disabled={loading || busy}><RefreshCw data-icon="inline-start" className={loading ? 'spin' : undefined} />刷新</Button><Button size="sm" onClick={() => void createSession()} disabled={readonly || busy}><Plus data-icon="inline-start" />新建会话</Button></div></div>
    {error && <motion.div className="codex-alert error" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} role="alert">{error}</motion.div>}
    {notice && <motion.div className="codex-alert" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} role="status">{notice}</motion.div>}
    <div className="codex-layout">
      <aside className="codex-sessions"><div className="codex-sessions-label">会话 <span>{sessions.length}</span></div><AnimatePresence initial={false}>{sessions.map((session, index) => <motion.button key={session.id} className={`codex-session ${selectedId === session.id ? 'is-active' : ''}`} onClick={() => setSelectedId(session.id)} initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -8 }} transition={{ delay: index * .03 }}><span className="codex-session-dot" /><span className="codex-session-copy"><strong>{session.title || `会话 ${session.id.slice(0, 8)}`}</strong><small>{session.status === 'running' ? '执行中' : session.status === 'failed' ? '执行失败' : '已就绪'}</small></span><ChevronRight size={14} /></motion.button>)}</AnimatePresence>{!sessions.length && !loading && <div className="codex-empty-side"><Command size={19} /><span>尚无会话</span><Button variant="outline" size="sm" onClick={() => void createSession()} disabled={readonly}>创建第一个</Button></div>}</aside>
      <section className="codex-console">{selected ? <><div className="codex-console-head"><div><h2>{selected.title || 'Codex 会话'}</h2><p><span>{selected.model || 'Codex CLI'}</span>{selected.working_directory && <><i />{selected.working_directory}</>}</p></div><span className={`codex-status ${selected.status === 'running' ? 'running' : selected.status === 'failed' ? 'failed' : ''}`}>{selected.status === 'running' ? <><Loader2 size={13} />执行中</> : selected.status === 'failed' ? '失败' : '已连接'}</span></div><div className="codex-transcript"><AnimatePresence initial={false}>{messages.map((message, index) => <motion.article key={`${message.created_at ?? index}-${index}`} className={`codex-message ${message.role}`} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={{ duration: .2 }}><span className="codex-message-avatar">{message.role === 'user' ? <UserRound size={14} /> : message.role === 'system' ? <Command size={14} /> : <Bot size={15} />}</span><div className="codex-message-body"><small>{message.role === 'user' ? '你' : message.role === 'system' ? '系统' : 'Codex'}</small><pre>{message.content}</pre></div></motion.article>)}</AnimatePresence>{!messages.length && <div className="codex-empty-console"><Terminal size={25} /><h3>准备好开始</h3><p>输入任务 让 Codex CLI 在当前项目中执行</p></div>}</div><form className="codex-composer" onSubmit={submit}><Input value={prompt} onChange={event => setPrompt(event.target.value)} placeholder={readonly ? '只读成员无法执行 Codex 任务' : '描述你希望 Codex 完成的任务'} disabled={readonly || busy || !selected} aria-label="Codex 任务提示"/><Button type="submit" disabled={readonly || busy || !prompt.trim() || !selected}>{busy ? <Loader2 className="spin" /> : <Send />}<span className="sr-only">发送</span></Button>{busy && <Button type="button" variant="ghost" size="icon-sm" aria-label="停止任务" title="停止任务" disabled><CircleStop /></Button>}</form></> : <div className="codex-empty-console"><Terminal size={28} /><h3>选择一个会话</h3><p>创建会话后即可嵌入 Codex CLI</p><Button onClick={() => void createSession()} disabled={readonly}><Plus data-icon="inline-start" />新建会话</Button></div>}</section>
    </div>
  </div>
}
