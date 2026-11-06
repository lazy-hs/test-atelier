import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { AlertCircle, LoaderCircle } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { api, type HarnessSessionReplay } from '@/lib/api'

type Props = {
  projectId: number
  taskId: string | null
  sessionId: string | null
  sessionIds?: string[]
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** Read-only view of the bounded Harness session; the business result stays in Test Atelier. */
export function HarnessSessionDialog({ projectId, taskId, sessionId, sessionIds = [], open, onOpenChange }: Props) {
  const [replay, setReplay] = useState<HarnessSessionReplay | null>(null)
  const [preferredSessionId, setPreferredSessionId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const provisionalId = taskId ? `ta-${taskId}` : ''
  const availableSessions = sessionIds.filter(value => value !== provisionalId)
  const selectedSessionId = preferredSessionId && availableSessions.includes(preferredSessionId)
    ? preferredSessionId : sessionId === provisionalId ? null : sessionId

  useEffect(() => {
    if (!open || !taskId || !selectedSessionId) return
    let active = true
    setLoading(true); setError(''); setReplay(null)
    api<HarnessSessionReplay>(`/projects/${projectId}/harness/tasks/${taskId}/session?session_id=${encodeURIComponent(selectedSessionId)}`)
      .then(value => { if (active) setReplay(value) })
      .catch(cause => { if (active) setError((cause as Error).message) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [open, projectId, taskId, selectedSessionId])

  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="harness-session-dialog">
    <DialogHeader>
      <DialogTitle>Harness Session 回放</DialogTitle>
      <DialogDescription>只读查看模型执行事件，业务结果和人工决定仍以 Test Atelier 为准</DialogDescription>
    </DialogHeader>
    <div className="harness-session-body">
      {availableSessions.length > 1 && <motion.label className="harness-session-picker" initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }}>
        <span>选择执行 Session</span>
        <select value={selectedSessionId ?? ''} onChange={event => setPreferredSessionId(event.target.value)}>
          {availableSessions.map(value => <option key={value} value={value}>{value}</option>)}
        </select>
      </motion.label>}
      {loading && <div className="prompt-state" role="status"><LoaderCircle className="spin" size={17} />正在加载 Session</div>}
      {!loading && error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
      {!loading && !error && replay && <>
        <div className="harness-session-meta"><Badge variant="secondary">{replay.format}</Badge><span>{replay.session_id}</span><strong>{replay.event_count} 个事件</strong></div>
        {replay.truncated && <div className="ai-alert warning" role="status">Session 超出回放上限，仅显示前缀事件</div>}
        <div className="harness-session-events">
          {!replay.events.length && <div className="prompt-state" role="status">Session 暂无可回放事件</div>}
          <AnimatePresence initial={false}>{replay.events.map((event, index) => {
            // Session versions use `type`; older artifacts may use `event` or
            // `kind`. Always render a concrete indexed label for readability.
            const rawLabel = event.type ?? event.event ?? event.kind ?? event.name
            const label = typeof rawLabel === 'string' && rawLabel.trim() ? rawLabel : 'session event'
            const stamp = typeof event.createdAt === 'string' ? event.createdAt : typeof event.timestamp === 'string' ? event.timestamp : ''
            return <motion.details key={`${index}-${label}`} aria-label={`事件 ${index + 1} ${label}`} initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ delay: Math.min(index * .015, .18) }}>
              <summary><span>事件 {index + 1} · {label}</span><small>{stamp || '查看详情'}</small></summary>
              <pre>{JSON.stringify(event, null, 2)}</pre>
            </motion.details>
          })}</AnimatePresence>
        </div>
      </>}
    </div>
  </DialogContent></Dialog>
}
