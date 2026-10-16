import { useEffect, useState } from 'react'
import { AlertCircle, CheckCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { api, type BatchReviewResult, type Case } from '@/lib/api'

const statusLabels: Record<BatchReviewResult['results'][number]['status'], string> = {
  saved: '已保存', unchanged: '无变化', conflict: '版本冲突', forbidden: '无权审核', invalid: '值无效', not_found: '用例不存在',
}

export function BatchReviewDialog({ projectId, items, open, onOpenChange, onFinished }: {
  projectId: number; items: Case[]; open: boolean; onOpenChange: (value: boolean) => void
  onFinished: (result: BatchReviewResult) => Promise<void>
}) {
  const [levelMode, setLevelMode] = useState('keep')
  const [smokeMode, setSmokeMode] = useState('keep')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<BatchReviewResult | null>(null)
  const [submitted, setSubmitted] = useState<Case[]>([])

  useEffect(() => {
    if (!open) { setResult(null); setError(''); setReason(''); setLevelMode('keep'); setSmokeMode('keep'); setSubmitted([]) }
  }, [open])

  async function submit() {
    setBusy(true); setError('')
    try {
      const selection = [...items]
      const payload = { reason: reason.trim(), items: selection.map(item => ({
        id: item.id, version: item.version,
        final_level: levelMode === 'keep' ? (item.final_level || item.level || '') : levelMode,
        final_smoke: smokeMode === 'keep' ? (item.final_smoke ?? item.smoke) : smokeMode === 'pending' ? '' : smokeMode,
      })) }
      const outcome = await api<BatchReviewResult>(`/projects/${projectId}/cases/batch-review`, { method: 'PUT', body: JSON.stringify(payload) })
      setSubmitted(selection); setResult(outcome)
      await onFinished(outcome)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }

  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="batch-dialog"><DialogHeader>
    <DialogTitle>批量审核</DialogTitle><DialogDescription>逐条校验版本与权限，冲突不会覆盖他人的修改</DialogDescription>
  </DialogHeader>
    {result ? <div className="batch-result"><strong>已保存 {result.saved} 条 · 冲突 {result.conflicts} 条</strong>
      <div>{result.results.map(entry => <div key={`${entry.id}-${entry.status}`}><span>{submitted.find(item => item.id === entry.id)?.original_id ?? entry.id}</span><span className={entry.status === 'saved' || entry.status === 'unchanged' ? 'reviewed-text' : 'error-line'}>{statusLabels[entry.status]}</span></div>)}</div>
      <DialogFooter><Button onClick={() => onOpenChange(false)}>完成</Button></DialogFooter>
    </div> : <div className="batch-form">
      <p>已选 {items.length} 条；保留当前值时，不会把空冒烟默认为“否”</p>
      <div className="batch-options"><label>最终等级<Select value={levelMode} onValueChange={setLevelMode}><SelectTrigger aria-label="批量等级处理"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
        <SelectItem value="keep">保留各用例当前值</SelectItem>{['1', '2', '3', '4'].map(value => <SelectItem key={value} value={value}>统一设为等级 {value}</SelectItem>)}
      </SelectGroup></SelectContent></Select></label>
        <label>最终冒烟<Select value={smokeMode} onValueChange={setSmokeMode}><SelectTrigger aria-label="批量冒烟处理"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
          <SelectItem value="keep">保留各用例当前值</SelectItem><SelectItem value="pending">统一设为待判定</SelectItem><SelectItem value="是">统一设为是</SelectItem><SelectItem value="否">统一设为否</SelectItem>
        </SelectGroup></SelectContent></Select></label></div>
      {levelMode === 'keep' && items.some(item => !(item.final_level || item.level)) && <div className="ai-alert warning"><AlertCircle size={16} />有用例缺少等级，需统一指定或待单条处理</div>}
      <label>审核理由<Textarea value={reason} onChange={event => setReason(event.target.value)} maxLength={1000} placeholder="说明本次审核依据" /></label>
      {error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
      <DialogFooter><Button disabled={busy || !reason.trim() || items.length === 0} onClick={() => void submit()}><CheckCheck data-icon="inline-start" />{busy ? '处理中…' : '逐条提交审核'}</Button></DialogFooter>
    </div>}
  </DialogContent></Dialog>
}
