import { useCallback, useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { BookOpen, Check, CircleOff, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { api, type ProjectSkill } from '@/lib/api'

export function SkillsPage({ projectId, readonly }: { projectId: number; readonly: boolean }) {
  const [skills, setSkills] = useState<ProjectSkill[]>([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const base = `/projects/${projectId}`
  const load = useCallback(async () => {
    setError('')
    try { setSkills(await api<ProjectSkill[]>(`${base}/skills`)) }
    catch (cause) { setError((cause as Error).message) }
  }, [base])
  useEffect(() => { void load() }, [load])
  async function toggle(skill: ProjectSkill) {
    setBusy(skill.skill_id); setError('')
    try {
      const updated = await api<{ skill_id: string; enabled: boolean }>(`${base}/skills/${skill.skill_id}`, {
        method: 'PATCH', body: JSON.stringify({ enabled: !skill.enabled }),
      })
      setSkills(previous => previous.map(item => item.skill_id === skill.skill_id ? { ...item, enabled: updated.enabled } : item))
    } catch (cause) { setError((cause as Error).message) } finally { setBusy('') }
  }
  return <div className="skills-page">
    <div className="skills-toolbar"><div><strong>项目技能</strong><span>技能规范来自 qa-doc-skills 项目</span></div><Button variant="outline" size="sm" onClick={() => void load()} disabled={Boolean(busy)}><RefreshCw data-icon="inline-start" />刷新</Button></div>
    {error && <div className="ai-alert error" role="alert">{error}</div>}
    <div className="skills-grid"><AnimatePresence initial={false}>
      {skills.map((skill, index) => <motion.article key={skill.skill_id} className={`skill-card ${skill.enabled ? '' : 'is-disabled'}`} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={{ delay: index * .04 }}>
        <div className="skill-card-icon">{skill.enabled ? <Check size={18} /> : <CircleOff size={18} />}</div>
        <div className="skill-card-body"><div className="skill-card-heading"><h2>{skill.label}</h2><Badge variant={skill.enabled ? 'secondary' : 'outline'}>{skill.enabled ? '已启用' : '已停用'}</Badge></div><p>{skill.description || '来自 qa-doc-skills 的项目技能'}</p><code>{skill.source_path}</code></div>
        {!readonly && <Button variant="outline" size="sm" onClick={() => void toggle(skill)} disabled={busy === skill.skill_id}>{busy === skill.skill_id ? '保存中' : skill.enabled ? '停用' : '启用'}</Button>}
      </motion.article>)}
    </AnimatePresence></div>
    {!skills.length && !error && <div className="ai-empty main"><BookOpen size={26} /><h2>正在读取项目技能</h2></div>}
  </div>
}
