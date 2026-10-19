import { useEffect, useReducer } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { AlertCircle, Check, Clipboard, LoaderCircle } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { api, type AiPromptLog } from '@/lib/api'

type Props = {
  projectId: number
  jobId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

type PromptResponse = { job_id: string; kind: string; status: string; prompts: (AiPromptLog | string)[] }

type ViewerState = { prompts: AiPromptLog[]; status: string; loading: boolean; error: string; copied: number | null }
type ViewerAction =
  | { type: 'load' }
  | { type: 'success'; prompts: AiPromptLog[]; status: string }
  | { type: 'failure'; error: string }
  | { type: 'copied'; index: number | null }

const initialState: ViewerState = { prompts: [], status: '', loading: false, error: '', copied: null }

function viewerReducer(state: ViewerState, action: ViewerAction): ViewerState {
  switch (action.type) {
    case 'load': return { ...initialState, loading: true }
    case 'success': return { ...state, prompts: action.prompts, status: action.status, loading: false }
    case 'failure': return { ...state, loading: false, error: action.error }
    case 'copied': return { ...state, copied: action.index }
  }
}

function normalizePrompt(value: AiPromptLog | string, index: number): AiPromptLog {
  if (typeof value === 'string') return { index: index + 1, provider: 'AI', text: value, length: value.length }
  const text = value.text ?? ''
  const provider = value.provider === 'codex_cli' || value.provider === 'codex'
    ? 'Codex CLI'
    : value.provider === 'claude_code' || value.provider === 'claude'
      ? 'Claude Code'
      : value.provider || 'AI'
  return { index: value.index ?? index + 1, provider, text, length: value.length ?? text.length }
}

/** Displays the exact prompt payload captured for an AI job without exposing it in the main report. */
export function PromptViewerDialog({ projectId, jobId, open, onOpenChange }: Props) {
  const [state, dispatch] = useReducer(viewerReducer, initialState)

  useEffect(() => {
    if (!open || !jobId) return
    let active = true
    dispatch({ type: 'load' })
    const load = async () => {
      // Prefer the structured route so the viewer can show provider metadata;
      // fall back to the legacy raw-string route for older development servers.
      let result: PromptResponse
      try {
        result = await api<PromptResponse>(`/projects/${projectId}/ai/jobs/${jobId}/prompts`)
      } catch {
        result = await api<PromptResponse>(`/projects/${projectId}/ai/jobs/${jobId}/prompt`)
      }
      if (!active) return
      dispatch({ type: 'success', prompts: (result.prompts ?? []).map(normalizePrompt), status: result.status })
    }
    load()
      .then(() => {
        if (!active) return
      })
      .catch(cause => { if (active) dispatch({ type: 'failure', error: (cause as Error).message }) })
    return () => { active = false }
  }, [open, projectId, jobId])

  async function copyPrompt(index: number, text: string) {
    try {
      await navigator.clipboard.writeText(text)
      dispatch({ type: 'copied', index })
      window.setTimeout(() => dispatch({ type: 'copied', index: null }), 1800)
    } catch {
      dispatch({ type: 'failure', error: '复制失败，请手动选择提示词文本' })
    }
  }

  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="prompt-dialog">
    <DialogHeader>
      <DialogTitle>发送给 AI 的提示词</DialogTitle>
      <DialogDescription>查看本次任务实际发送的完整内容，可复制用于排查分析结果</DialogDescription>
    </DialogHeader>
    <div className="prompt-dialog-body">
      {state.loading && <div className="prompt-state" role="status"><LoaderCircle className="spin" size={17} />正在加载提示词</div>}
      {!state.loading && state.error && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{state.error}</div>}
      {!state.loading && !state.error && !state.prompts.length && <div className="prompt-state">当前任务尚未记录提示词（状态：{state.status || '未知'}）</div>}
      {!state.loading && !state.error && state.prompts.length > 0 && <div className="prompt-list"><AnimatePresence initial={false}>{state.prompts.map((prompt, index) => <motion.article key={`${prompt.index}-${index}`} className="prompt-card" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ delay: index * 0.04 }}>
        <div className="prompt-card-heading"><div><strong>提示词 {prompt.index ?? index + 1}</strong><Badge variant="secondary">{prompt.provider}</Badge><span>{prompt.length ?? prompt.text.length} 字符</span></div><Button variant="outline" size="sm" onClick={() => void copyPrompt(index, prompt.text)}>{state.copied === index ? <Check data-icon="inline-start" /> : <Clipboard data-icon="inline-start" />}{state.copied === index ? '已复制' : '复制'}</Button></div>
        <pre className="prompt-text">{prompt.text}</pre>
      </motion.article>)}</AnimatePresence></div>}
    </div>
  </DialogContent></Dialog>
}
