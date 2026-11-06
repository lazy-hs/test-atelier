import { useEffect, useState, type FormEvent } from 'react'
import { AlertCircle, ArrowUpRight, LayoutTemplate, Plus, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { api, type Project, type ProjectTemplate, type ProjectTemplateDetail } from '@/lib/api'

export function ProjectTemplatesDialog({ project, onClose, onCreated }: {
  project: Project; onClose: () => void; onCreated: (project: Project) => void
}) {
  const base = `/projects/${project.id}/templates`
  const [templates, setTemplates] = useState<ProjectTemplate[]>([])
  const [templateName, setTemplateName] = useState(`${project.name} 配置`)
  const [selected, setSelected] = useState<ProjectTemplateDetail | null>(null)
  const [cloneId, setCloneId] = useState<number | null>(null)
  const [cloneName, setCloneName] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let active = true
    void api<ProjectTemplate[]>(base).then(value => { if (active) setTemplates(value) })
      .catch(cause => { if (active) setError(cause.message) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [base])

  async function saveTemplate(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setNotice('')
    try {
      const created = await api<ProjectTemplate>(base, { method: 'POST', body: JSON.stringify({ name: templateName.trim() }) })
      setTemplates(previous => [created, ...previous])
      setNotice('当前配置已冻结为模板 后续修改原项目不会覆盖此版本')
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }

  async function showTemplate(template: ProjectTemplate) {
    setBusy(true); setError('')
    try {
      setSelected(await api<ProjectTemplateDetail>(`${base}/${template.id}`))
      setCloneId(template.id)
      setCloneName(`${template.name} 新项目`)
    }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }

  async function instantiate(event: FormEvent) {
    event.preventDefault()
    if (!cloneId || !cloneName.trim()) return
    setBusy(true); setError('')
    try {
      const created = await api<Project>(`${base}/${cloneId}/instantiate`, {
        method: 'POST', body: JSON.stringify({ name: cloneName.trim() }),
      })
      onCreated(created)
    } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }

  return <Dialog open onOpenChange={open => { if (!open && !busy) onClose() }}><DialogContent className="project-templates-dialog"><DialogHeader><DialogTitle>项目模板 · {project.name}</DialogTitle><DialogDescription>只保存技能开关 转换选项与已发布分类 不复制人员 用例 材料 或凭据</DialogDescription></DialogHeader>
    {error && <div role="alert" className="ai-alert error"><AlertCircle size={16} />{error}</div>}
    {notice && <div role="status" className="ai-alert">{notice}</div>}
    <form className="project-template-new" onSubmit={event => void saveTemplate(event)}><label>模板名称<Input value={templateName} onChange={event => setTemplateName(event.target.value)} maxLength={120} required /></label><Button type="submit" size="sm" disabled={busy || !templateName.trim()}><Plus data-icon="inline-start" />保存当前配置</Button></form>
    <div className="project-template-layout"><div className="project-template-list"><strong>已保存模板 · {templates.length}</strong>
      {loading ? <p role="status">正在读取模板</p> : templates.map(template => <button className={selected?.id === template.id ? 'selected' : ''} key={template.id} onClick={() => void showTemplate(template)}><LayoutTemplate size={17} /><span><strong>{template.name}</strong><small>{template.enabled_skill_count}/{template.skill_count} 项技能启用 · {template.has_taxonomy ? '含分类' : '无分类'}</small></span></button>)}
      {!loading && !templates.length && <p>尚无模板 从当前项目配置保存第一份</p>}
    </div><div className="project-template-detail">{selected ? <><span className="project-template-kicker">冻结版本 · #{selected.id}</span><h3>{selected.name}</h3><p>SHA-256 {selected.snapshot_sha256.slice(0, 16)}… · {new Date(selected.created_at).toLocaleString('zh-CN')}</p>
      <div className="project-template-facts"><span>技能<strong>{selected.enabled_skill_count} / {selected.skill_count}</strong></span><span>分类<strong>{selected.has_taxonomy ? '已发布版本' : '未包含'}</strong></span><span>转换<strong>{selected.has_conversion_settings ? '已保存选项' : '默认选项'}</strong></span></div>
      <div className="project-template-inspect"><strong>技能开关</strong><p>{selected.snapshot.skills.map(skill => `${skill.skill_id} ${skill.enabled ? '开' : '关'}`).join(' · ') || '未包含项目技能'}</p>
        <strong>转换选项</strong><p>{selected.snapshot.conversion ? `${selected.snapshot.conversion.default_case_type} · ${selected.snapshot.conversion.default_case_status} · 保留 ${selected.snapshot.conversion.history_retention_days} 天` : '使用新项目默认配置'} 路径不会复制</p>
        <strong>项目分类</strong><p>{selected.snapshot.taxonomy ? `来源版本 ${selected.snapshot.taxonomy.source_number} · ${Object.values(selected.snapshot.taxonomy.definitions).reduce((total, entries) => total + entries.length, 0)} 项分类` : '未包含已发布的分类版本'}</p></div>
      <form className="project-template-clone" onSubmit={event => void instantiate(event)}><label>新项目名称<Input value={cloneName} onChange={event => setCloneName(event.target.value)} maxLength={120} required /></label><Button size="sm" type="submit" disabled={busy || cloneId !== selected.id || !cloneName.trim()}><ArrowUpRight data-icon="inline-start" />从模板新建项目</Button></form>
      <small className="project-template-trust"><ShieldCheck size={14} />创建前会重新核对本机技能仓 模板不包含旧项目权限</small>
    </> : <div className="project-template-empty"><LayoutTemplate size={24} /><p>选择模板核对其冻结配置 再新建独立项目</p></div>}</div></div>
  </DialogContent></Dialog>
}
