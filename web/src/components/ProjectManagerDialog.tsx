import { useMemo, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { Check, CopyPlus, FolderCog, FolderOpen, LayoutTemplate, Pencil, Plus, RotateCcw, Trash2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { api, type Project } from '@/lib/api'
import { ProjectTemplatesDialog } from './ProjectTemplatesDialog'

type ProjectManagerDialogProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  projects: Project[]
  currentId: number | null
  onSelect: (project: Project) => void
  onProjectsChange: (projects: Project[], removedId?: number) => void
  onCreate: () => void
  onNotice: (message: string) => void
  onError: (message: string) => void
}

/** Project-level operations live in one place instead of being hidden in the selector. */
export function ProjectManagerDialog({ open, onOpenChange, projects, currentId, onSelect, onProjectsChange, onCreate, onNotice, onError }: ProjectManagerDialogProps) {
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editingName, setEditingName] = useState('')
  const [duplicateId, setDuplicateId] = useState<number | null>(null)
  const [duplicateName, setDuplicateName] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null)
  const [templateSource, setTemplateSource] = useState<Project | null>(null)
  const [deleteError, setDeleteError] = useState('')
  const [busyId, setBusyId] = useState<number | null>(null)
  const sortedProjects = useMemo(() => projects.slice().sort((a, b) => b.id - a.id), [projects])

  function beginRename(project: Project) {
    setEditingId(project.id)
    setEditingName(project.name)
  }

  async function renameProject(project: Project) {
    const name = editingName.trim()
    if (!name || name === project.name) { setEditingId(null); return }
    setBusyId(project.id); onError('')
    try {
      const updated = await api<Project>(`/projects/${project.id}`, { method: 'PATCH', body: JSON.stringify({ name }) })
      onProjectsChange(projects.map(item => item.id === updated.id ? { ...item, ...updated } : item))
      setEditingId(null); onNotice('项目名称已更新')
    } catch (cause) { onError((cause as Error).message) } finally { setBusyId(null) }
  }

  async function duplicateProject(project: Project) {
    const name = duplicateName.trim()
    if (!name) return
    setBusyId(project.id); onError('')
    try {
      const created = await api<Project>(`/projects/${project.id}/duplicate`, { method: 'POST', body: JSON.stringify({ name }) })
      onProjectsChange([created, ...projects]); setDuplicateId(null); setDuplicateName(''); onNotice('项目已复制')
    } catch (cause) { onError((cause as Error).message) } finally { setBusyId(null) }
  }

  async function deleteProject() {
    if (!deleteTarget) return
    setBusyId(deleteTarget.id); setDeleteError(''); onError('')
    try {
      await api<{ ok: boolean; id: number }>(`/projects/${deleteTarget.id}`, { method: 'DELETE' })
      onProjectsChange(projects.filter(item => item.id !== deleteTarget.id), deleteTarget.id)
      setDeleteTarget(null); onNotice('项目已删除')
    } catch (cause) {
      setDeleteError((cause as Error).message)
      onError((cause as Error).message)
      try {
        const refreshed = await api<Project[]>('/projects')
        const current = refreshed.find(item => item.id === deleteTarget.id)
        onProjectsChange(refreshed, !current || current.deletion_pending_at ? deleteTarget.id : undefined)
        if (current) setDeleteTarget(current)
      } catch { /* Keep the original Host error visible if refresh fails. */ }
    } finally { setBusyId(null) }
  }


  return <>
    <Dialog open={open} onOpenChange={value => { if (!value) { setEditingId(null); setDuplicateId(null) }; onOpenChange(value) }}>
      <DialogContent className="project-manager-dialog">
        <DialogHeader className="project-manager-header">
          <div className="project-manager-heading"><span className="project-manager-icon"><FolderCog size={18} /></span><div><DialogTitle>项目管理</DialogTitle><DialogDescription>选择项目并管理名称与副本</DialogDescription></div></div>
          <Button variant="outline" size="sm" onClick={() => { onOpenChange(false); onCreate() }}><Plus data-icon="inline-start" />新建项目</Button>
        </DialogHeader>
        <div className="project-manager-list" role="list">
          <AnimatePresence initial={false} mode="popLayout">
            {sortedProjects.map(project => {
              const isCurrent = project.id === currentId
              const editable = project.role === 'admin' || project.role === 'manager'
              return <motion.div layout key={project.id} className={`project-manager-row ${isCurrent ? 'is-current' : ''} ${project.deletion_pending_at ? 'is-deleting' : ''}`} role="listitem" initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, height: 0, marginBottom: 0 }} transition={{ duration: .2 }}>
                <button className="project-manager-select" disabled={Boolean(project.deletion_pending_at)} onClick={() => { onSelect(project); onOpenChange(false) }} aria-label={project.deletion_pending_at ? `${project.name} 正在等待删除重试` : `切换到 ${project.name}`}>
                  <span className="project-manager-badge"><FolderOpen size={15} /></span><span className="project-manager-copy"><strong>{project.name}</strong><small>{project.deletion_pending_at ? <motion.span initial={{ opacity: 0, x: -4 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -4 }}>删除待完成 · 可重试</motion.span> : project.role === 'admin' ? '管理员' : project.role === 'manager' ? '项目管理员' : project.role === 'reviewer' ? '评审员' : '只读成员'}</small></span>{isCurrent && <Check className="project-manager-check" size={16} />}
                </button>
                {editable && <div className="project-manager-actions">
                  {!project.deletion_pending_at && <><Button variant="ghost" size="icon-sm" title="重命名项目" aria-label="重命名项目" disabled={busyId === project.id} onClick={() => beginRename(project)}><Pencil /></Button>
                  <Button variant="ghost" size="icon-sm" title="复制项目" aria-label="复制项目" disabled={busyId === project.id} onClick={() => { setDuplicateId(project.id); setDuplicateName(`${project.name} 副本`) }}><CopyPlus /></Button>
                  <Button variant="ghost" size="icon-sm" title="项目模板" aria-label={`管理 ${project.name} 模板`} disabled={busyId === project.id} onClick={() => setTemplateSource(project)}><LayoutTemplate /></Button></>}
                  <Button variant="ghost" size="icon-sm" title={project.deletion_pending_at ? '重试清理项目' : '删除项目'} aria-label={project.deletion_pending_at ? '重试清理项目' : '删除项目'} disabled={busyId === project.id} onClick={() => { setDeleteError(''); setDeleteTarget(project) }}>{project.deletion_pending_at ? <RotateCcw /> : <Trash2 />}</Button>
                </div>}
                {editingId === project.id && <motion.form className="project-manager-inline-form" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} onSubmit={event => { event.preventDefault(); void renameProject(project) }}><Input value={editingName} onChange={event => setEditingName(event.target.value)} maxLength={120} autoFocus aria-label="项目名称" /><Button type="submit" size="icon-sm" disabled={!editingName.trim() || busyId === project.id} title="保存名称" aria-label="保存名称"><Check /></Button><Button type="button" variant="ghost" size="icon-sm" onClick={() => setEditingId(null)} title="取消重命名" aria-label="取消重命名"><X /></Button></motion.form>}
              </motion.div>
            })}
          </AnimatePresence>
          {sortedProjects.length === 0 && <div className="project-manager-empty">还没有项目</div>}
        </div>
        <DialogFooter><span className="project-manager-footnote">项目数据会随删除一并移除</span><Button variant="outline" onClick={() => onOpenChange(false)}>完成</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={duplicateId !== null} onOpenChange={value => !value && setDuplicateId(null)}>
      <DialogContent>
        <DialogHeader><DialogTitle>复制项目</DialogTitle><DialogDescription>复制项目结构与设置 不复制成员权限</DialogDescription></DialogHeader>
        <form className="project-manager-form" onSubmit={event => { event.preventDefault(); const project = projects.find(item => item.id === duplicateId); if (project) void duplicateProject(project) }}><label>新项目名称<Input value={duplicateName} onChange={event => setDuplicateName(event.target.value)} maxLength={120} required autoFocus /></label><DialogFooter><Button type="button" variant="outline" onClick={() => setDuplicateId(null)}>取消</Button><Button type="submit" disabled={!duplicateName.trim() || busyId === duplicateId}>复制</Button></DialogFooter></form>
      </DialogContent>
    </Dialog>
    <Dialog open={deleteTarget !== null} onOpenChange={value => !value && setDeleteTarget(null)}>
      <DialogContent>
        <DialogHeader><DialogTitle>{deleteTarget?.deletion_pending_at ? '继续清理项目' : '删除项目'}</DialogTitle><DialogDescription>{deleteTarget?.deletion_pending_at ? '上次清理未完成 请先恢复 Harness Host 或核对旧版 Session 再重试' : '删除项目会移除其中的用例 材料与执行记录 此操作无法撤销'}</DialogDescription></DialogHeader>
        <p className="project-delete-copy">确认删除「{deleteTarget?.name}」</p>
        <AnimatePresence>{deleteError && <motion.p className="error-line" role="alert" initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }}>{deleteError}</motion.p>}</AnimatePresence>
        <DialogFooter><Button variant="outline" onClick={() => setDeleteTarget(null)}>取消</Button><Button variant="destructive" onClick={() => void deleteProject()} disabled={busyId === deleteTarget?.id}>{deleteTarget?.deletion_pending_at ? <RotateCcw data-icon="inline-start" /> : <Trash2 data-icon="inline-start" />}{deleteTarget?.deletion_pending_at ? '重试清理' : '确认删除'}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    {templateSource && <ProjectTemplatesDialog key={templateSource.id} project={templateSource} onClose={() => setTemplateSource(null)} onCreated={created => {
      onProjectsChange([created, ...projects]); onSelect(created); setTemplateSource(null)
      onOpenChange(false); onNotice('已从冻结模板创建独立项目')
    }} />}
  </>
}
