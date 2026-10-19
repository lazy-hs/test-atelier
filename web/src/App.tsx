import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { AnimatePresence, motion, MotionConfig } from 'motion/react'
import { Archive, BookOpen, Download, FileSpreadsheet, FileText, FolderPlus, Home, Layers3, ListChecks, LogOut, Plus, RefreshCw, Settings2, Trash2, Upload, UsersRound, WandSparkles, X } from 'lucide-react'
import { BrowserRouter, useLocation, useNavigate } from 'react-router'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { Overview } from '@/components/HomePage'
import { api, login, refreshCsrf, upload, type BatchReviewResult, type Case, type CaseDetail, type ImportRecord, type Preview, type Project, type SuggestionJob, type User } from '@/lib/api'
import './App.css'

const HomePage = lazy(() => import('@/components/HomePage').then(module => ({ default: module.HomePage })))
const RequirementsPage = lazy(() => import('@/components/RequirementsPage').then(module => ({ default: module.RequirementsPage })))
const AuthoringPage = lazy(() => import('@/components/AuthoringPage').then(module => ({ default: module.AuthoringPage })))
const SkillsPage = lazy(() => import('@/components/SkillsPage').then(module => ({ default: module.SkillsPage })))
const TaxonomyPage = lazy(() => import('@/components/TaxonomyPage').then(module => ({ default: module.TaxonomyPage })))
const TeamPage = lazy(() => import('@/components/TeamPage').then(module => ({ default: module.TeamPage })))
const SmokePage = lazy(() => import('@/components/SmokePage').then(module => ({ default: module.SmokePage })))
const ModelSettingsPage = lazy(() => import('@/components/ModelSettingsPage').then(module => ({ default: module.ModelSettingsPage })))
const CaseTable = lazy(() => import('@/components/CaseTable').then(module => ({ default: module.CaseTable })))
const ReviewPane = lazy(() => import('@/components/ReviewPane').then(module => ({ default: module.ReviewPane })))
const ImportDialog = lazy(() => import('@/components/ImportDialog').then(module => ({ default: module.ImportDialog })))
const BatchReviewDialog = lazy(() => import('@/components/BatchReviewDialog').then(module => ({ default: module.BatchReviewDialog })))
const ConverterHost = lazy(() => import('@/converter/ConverterHost').then(module => ({ default: module.ConverterHost })))

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    try { onLogin(await login(username, password)) } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  return <main className="login-screen"><div className="login-header"><span className="brand-symbol">T</span><span>Test Atelier</span></div><motion.form className="login-form" onSubmit={submit} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -12 }}>
    <h1>登录 Test Atelier</h1><p>从需求到用例 让工作连续发生</p>
    <label>账号<Input autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} required /></label>
    <label>密码<Input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required /></label>
    {error && <div className="error-line" role="alert">{error}</div>}
    <Button type="submit" size="lg" disabled={busy}>{busy ? '登录中…' : '登录'}</Button>
  </motion.form></main>
}

function Workspace({ user, onLogout }: { user: User; onLogout: () => Promise<boolean> }) {
  const location = useLocation()
  const navigate = useNavigate()
  const route = location.pathname.match(/^\/projects\/(\d+)\/(home|cases|imports|conversion|requirements|authoring|taxonomy|team|smoke|skills|model-settings)$/)
  const view = route?.[2] ?? 'home'
  const filterParams = new URLSearchParams(location.search)
  const assigneeFilter = ['all', 'me', 'unassigned'].includes(filterParams.get('assignee') ?? '') ? filterParams.get('assignee')! : 'all'
  const statusFilter = ['all', 'pending', 'reviewed'].includes(filterParams.get('status') ?? '') ? filterParams.get('status')! : 'all'
  const suggestionFilter = ['all', 'pending'].includes(filterParams.get('suggestion') ?? '') ? filterParams.get('suggestion')! : 'all'
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState<number | null>(null)
  const activeRole = projects.find(item => item.id === projectId)?.role ?? (user.role === 'admin' ? 'admin' : 'reader')
  const [overview, setOverview] = useState<Overview | null>(null)
  const [items, setItems] = useState<Case[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState<number | null>(null)
  const [checked, setChecked] = useState<number[]>([])
  const [batchOpen, setBatchOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<number[] | null>(null)
  const [suggestionJob, setSuggestionJob] = useState<SuggestionJob | null>(null)
  const [suggestionBusy, setSuggestionBusy] = useState(false)
  const [suggestionRevision, setSuggestionRevision] = useState(0)
  const [detail, setDetail] = useState<CaseDetail | null>(null)
  const [imports, setImports] = useState<ImportRecord[]>([])
  const [importOpen, setImportOpen] = useState(false)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [working, setWorking] = useState(false)
  const [projectOpen, setProjectOpen] = useState(false)
  const [projectName, setProjectName] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const requestId = useRef(0)

  useEffect(() => { if (!notice) return; const timer = window.setTimeout(() => setNotice(''), 4000); return () => window.clearTimeout(timer) }, [notice])

  useEffect(() => { api<Project[]>('/projects').then(data => {
    setProjects(data)
    const requested = Number(window.location.pathname.match(/^\/projects\/(\d+)/)?.[1])
    setProjectId(data.find(item => item.id === requested)?.id ?? data[0]?.id ?? null)
    if (requested && !data.some(item => item.id === requested)) navigate('/', { replace: true })
  }).catch(cause => setError(cause.message)) }, [navigate])
  useEffect(() => {
    const requested = Number(route?.[1])
    if (requested && projects.some(item => item.id === requested) && requested !== projectId) {
      setProjectId(requested); selectCase(null); setPage(1); setQuery('')
    }
  }, [location.pathname, projects, projectId])
  useEffect(() => {
    if (!projectId || view !== 'home') return
    let active = true
    api<Overview>(`/projects/${projectId}/overview`)
      .then(data => { if (active) setOverview(data) }).catch(cause => { if (active) setError(cause.message) })
    return () => { active = false }
  }, [projectId, view])
  function go(next: string) { selectCase(null); setError(''); setChecked([]); setBatchOpen(false); navigate(projectId ? `/projects/${projectId}/${next}` : '/') }
  function changeCaseFilter(name: 'assignee' | 'status' | 'suggestion', value: string) {
    const params = new URLSearchParams(location.search)
    if (value === 'all') params.delete(name); else params.set(name, value)
    setPage(1); setChecked([]); selectCase(null)
    navigate({ pathname: location.pathname, search: params.toString() }, { replace: true })
  }
  useEffect(() => { const timer = setTimeout(() => setDebouncedQuery(query), 220); return () => clearTimeout(timer) }, [query])
  const reload = useCallback(async () => {
    if (!projectId) return
    const currentRequest = ++requestId.current
    setLoading(true)
    try {
      const [cases, records] = await Promise.all([
        api<{ items: Case[]; total: number }>(`/projects/${projectId}/cases?page=${page}&size=30&q=${encodeURIComponent(debouncedQuery)}&assignee=${assigneeFilter}&status=${statusFilter}&suggestion=${suggestionFilter}`),
        api<ImportRecord[]>(`/projects/${projectId}/imports`),
      ])
      if (currentRequest === requestId.current) { setItems(cases.items); setTotal(cases.total); setImports(records) }
    } catch (cause) { if (currentRequest === requestId.current) setError((cause as Error).message) } finally { if (currentRequest === requestId.current) setLoading(false) }
  }, [projectId, page, debouncedQuery, assigneeFilter, statusFilter, suggestionFilter])
  useEffect(() => {
    if (view !== 'cases' && view !== 'imports') return
    const timer = window.setTimeout(() => { void reload() }, 0)
    return () => window.clearTimeout(timer)
  }, [reload, view])
  useEffect(() => {
    if (!projectId || view !== 'cases') return
    let active = true
    void api<SuggestionJob[]>(`/projects/${projectId}/suggestion-jobs`).then(jobs => {
      if (active) setSuggestionJob(jobs.find(job => ['queued', 'running', 'partial', 'failed'].includes(job.status)) ?? null)
    }).catch(cause => { if (active) setError(cause.message) })
    return () => { active = false }
  }, [projectId, view])
  useEffect(() => {
    if (!projectId || !suggestionJob || suggestionJob.project_id !== projectId || !['queued', 'running'].includes(suggestionJob.status)) return
    const timer = window.setInterval(() => {
      void api<SuggestionJob>(`/projects/${projectId}/suggestion-jobs/${suggestionJob.id}`).then(job => {
        setSuggestionJob(job)
        if (!['queued', 'running'].includes(job.status)) { setSuggestionRevision(value => value + 1); void reload() }
      }).catch(cause => setError(cause.message))
    }, 2500)
    return () => window.clearInterval(timer)
  }, [projectId, suggestionJob, reload])
  useEffect(() => {
    if (!projectId || !selected) return
    let active = true
    api<CaseDetail>(`/projects/${projectId}/cases/${selected}`).then(result => { if (active) setDetail(result) }).catch(cause => { if (active) setError(cause.message) })
    return () => { active = false }
  }, [projectId, selected])
  function selectCase(id: number | null) { setDetail(null); setSelected(id) }
  function requestDelete(ids: number[]) { if (ids.length > 0) setDeleteTarget(ids) }
  async function confirmDelete() {
    if (!projectId || !deleteTarget?.length) return
    setWorking(true); setError('')
    try {
      const result = deleteTarget.length === 1
        ? await api<{ deleted: number }>(`/projects/${projectId}/cases/${deleteTarget[0]}`, { method: 'DELETE' })
        : await api<{ deleted: number }>(`/projects/${projectId}/cases/batch-delete`, { method: 'DELETE', body: JSON.stringify({ case_ids: deleteTarget }) })
      const count = result.deleted
      if (selected !== null && deleteTarget.includes(selected)) selectCase(null)
      setChecked(previous => previous.filter(id => !deleteTarget.includes(id)))
      setDeleteTarget(null); setNotice(`已删除 ${count} 条用例`); await reload()
    } catch (cause) { setError((cause as Error).message) } finally { setWorking(false) }
  }
  async function createProject(event: FormEvent) {
    event.preventDefault(); setError('')
    try {
      const project = await api<Project>('/projects', { method: 'POST', body: JSON.stringify({ name: projectName.trim() }) })
      setProjects(previous => [project, ...previous]); setProjectId(project.id); setProjectOpen(false); setProjectName(''); setNotice('项目已创建'); navigate(`/projects/${project.id}/home`)
    } catch (cause) { setError((cause as Error).message) }
  }
  async function onFile(file: File) {
    if (!projectId) return
    setWorking(true); setError('')
    try { setPreview(await upload(projectId, file)) } catch (cause) { setError((cause as Error).message); setImportOpen(false) } finally { setWorking(false) }
  }
  async function confirmImport() {
    if (!projectId || !preview) return
    setWorking(true); setError('')
    try {
      const result = await api<{ count: number }>(`/projects/${projectId}/imports/${preview.id}/confirm`, { method: 'POST' })
      setNotice(`已导入 ${result.count} 条用例`); setImportOpen(false); setPreview(null); go('cases'); setPage(1); await reload()
    } catch (cause) { setError((cause as Error).message); setImportOpen(false) } finally { setWorking(false) }
  }
  async function saveReview(level: string, smoke: string, reason: string) {
    if (!projectId || !detail) return
    setWorking(true); setError('')
    try {
      const result = await api<CaseDetail>(`/projects/${projectId}/cases/${detail.id}/review`, {
        method: 'PUT', body: JSON.stringify({ version: detail.version, final_level: level, final_smoke: smoke, reason }),
      })
      setDetail({ ...detail, ...result }); setNotice('审核已保存'); await reload()
    } catch (cause) {
      setError((cause as Error).message)
      if ((cause as Error).message.includes('刷新')) {
        try { setDetail(await api<CaseDetail>(`/projects/${projectId}/cases/${detail.id}`)); await reload() } catch { selectCase(null) }
      }
    } finally { setWorking(false) }
  }
  async function assignCase(assigneeId: number | null) {
    if (!projectId || !detail) return
    setWorking(true); setError('')
    try {
      const result = await api<Case>(`/projects/${projectId}/cases/${detail.id}/assignment`, {
        method: 'PUT', body: JSON.stringify({ version: detail.version, assignee_id: assigneeId }),
      })
      setDetail({ ...detail, ...result }); setNotice(result.assignee ? `已分派给 ${result.assignee}` : '负责人已清除'); await reload()
    } catch (cause) {
      setError((cause as Error).message)
      if ((cause as Error).message.includes('刷新')) {
        try { setDetail(await api<CaseDetail>(`/projects/${projectId}/cases/${detail.id}`)); await reload() } catch { selectCase(null) }
      }
    } finally { setWorking(false) }
  }
  async function afterBatch(result: BatchReviewResult) {
    setChecked(result.results.filter(entry => !['saved', 'unchanged'].includes(entry.status)).map(entry => entry.id))
    await reload()
  }
  async function generateRules() {
    if (!projectId || checked.length === 0) return
    setSuggestionBusy(true); setError('')
    try {
      const result = await api<{ count: number }>(`/projects/${projectId}/suggestions/rules`, {
        method: 'POST', body: JSON.stringify({ case_ids: checked }),
      })
      setNotice(`已生成 ${result.count} 条规则候选`); setSuggestionRevision(value => value + 1); await reload()
    } catch (cause) { setError((cause as Error).message) } finally { setSuggestionBusy(false) }
  }
  async function generateModel() {
    if (!projectId || checked.length === 0) return
    setSuggestionBusy(true); setError('')
    try {
      const job = await api<SuggestionJob>(`/projects/${projectId}/suggestions/model`, {
        method: 'POST', body: JSON.stringify({ case_ids: checked, provider: 'local' }),
      })
      setSuggestionJob(job); setNotice('AI 建议任务已创建')
    } catch (cause) { setError((cause as Error).message) } finally { setSuggestionBusy(false) }
  }
  async function retrySuggestionJob() {
    if (!projectId || !suggestionJob) return
    setSuggestionBusy(true); setError('')
    try { setSuggestionJob(await api<SuggestionJob>(`/projects/${projectId}/suggestion-jobs/${suggestionJob.id}/retry`, { method: 'POST' })) }
    catch (cause) { setError((cause as Error).message) } finally { setSuggestionBusy(false) }
  }
  async function afterSuggestion(value: CaseDetail) {
    setDetail(value); setChecked(previous => previous.filter(id => id !== value.id))
    setSuggestionRevision(previous => previous + 1); setNotice('建议决策已保存'); await reload()
  }
  return <div className="app-shell">
    <aside className="sidebar"><div className="sidebar-brand"><span className="brand-symbol">T</span><span>Test Atelier</span></div>
      <div className="sidebar-section">工作区</div>
      <button className={`nav-item ${view === 'home' ? 'active' : ''}`} onClick={() => go('home')}>{view === 'home' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<Home size={17} />首页</button>
      <div className="sidebar-section nav-group">AI 工具</div>
      <button className={`nav-item ${view === 'requirements' ? 'active' : ''}`} onClick={() => go('requirements')}>{view === 'requirements' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<FileText size={17} />需求分析</button>
      <button className={`nav-item ${view === 'authoring' ? 'active' : ''}`} onClick={() => go('authoring')}>{view === 'authoring' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<WandSparkles size={17} />用例编写</button>
      <button className={`nav-item ${view === 'skills' ? 'active' : ''}`} onClick={() => go('skills')}>{view === 'skills' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<BookOpen size={17} />项目技能</button>
      <button className={`nav-item ${view === 'conversion' ? 'active' : ''}`} onClick={() => go('conversion')}>{view === 'conversion' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<RefreshCw size={17} />用例转换</button>
      <div className="sidebar-section nav-group">用例管理</div>
      <button className={`nav-item ${view === 'cases' ? 'active' : ''}`} onClick={() => go('cases')}>{view === 'cases' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<FileSpreadsheet size={17} />用例库</button>
      <button className={`nav-item ${view === 'imports' ? 'active' : ''}`} onClick={() => go('imports')}>{view === 'imports' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<Archive size={17} />导入记录</button>
      <button className={`nav-item ${view === 'taxonomy' ? 'active' : ''}`} onClick={() => go('taxonomy')}>{view === 'taxonomy' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<Layers3 size={17} />分类规则</button>
      <button className={`nav-item ${view === 'smoke' ? 'active' : ''}`} onClick={() => go('smoke')}>{view === 'smoke' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<ListChecks size={17} />冒烟执行</button>
      <div className="sidebar-section nav-group">团队协作</div>
      <button className={`nav-item ${view === 'team' ? 'active' : ''}`} onClick={() => go('team')}>{view === 'team' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<UsersRound size={17} />项目成员</button>
      {user.role === 'admin' && <button className={`nav-item ${view === 'model-settings' ? 'active' : ''}`} onClick={() => go('model-settings')} disabled={!projectId} title={!projectId ? '请先创建项目' : undefined}>{view === 'model-settings' && <motion.span layoutId="active-nav" className="nav-active-bg" />}<Settings2 size={17} />模型配置</button>}
      <div className="sidebar-bottom"><div className="user-block"><span className="avatar">{user.username.slice(0, 1).toUpperCase()}</span><div><strong>{user.username}</strong><small>{({ admin: '全局管理员', manager: '项目管理员', reviewer: '评审员', reader: '只读成员' } as Record<string, string>)[activeRole]}</small></div></div><Button size="icon-sm" variant="ghost" aria-label="退出登录" title="退出登录" onClick={() => { void onLogout().then(ok => { if (ok) navigate('/', { replace: true }); else setError('退出失败，请稍后重试') }) }}><LogOut /></Button></div>
    </aside>
    <main className="main-area">
      <header className="topbar"><div className="project-selector"><span>当前项目</span>{projects.length ? <Select value={String(projectId ?? '')} onValueChange={value => { setProjectId(Number(value)); selectCase(null); setChecked([]); setBatchOpen(false); setPage(1); setQuery(''); navigate(`/projects/${value}/${view}`) }}><SelectTrigger aria-label="选择项目"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{projects.map(project => <SelectItem key={project.id} value={String(project.id)}>{project.name}</SelectItem>)}</SelectGroup></SelectContent></Select> : <strong>暂无项目</strong>}<Button variant="ghost" size="icon-sm" aria-label="新建项目" title="新建项目" onClick={() => setProjectOpen(true)}><FolderPlus /></Button></div>
        <div className="top-actions">{projectId && (view === 'cases' || view === 'imports') && <><Button variant="outline" onClick={() => window.location.assign(`/api/v1/projects/${projectId}/export`)}><Download data-icon="inline-start" />导出 Excel</Button>{activeRole !== 'reader' && <Button onClick={() => { setPreview(null); setImportOpen(true) }}><Upload data-icon="inline-start" />导入用例</Button>}</>}</div></header>
      <div className={`workspace ${view === 'conversion' ? 'converter-workspace' : ''}`}><AnimatePresence mode="wait"><motion.div key={view} className="workspace-screen" initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -7 }} transition={{ duration: .2 }}>
        {view !== 'conversion' && <div className="workspace-title"><h1>{({ home: '工作台', cases: '用例审核', imports: '导入记录', requirements: '需求分析', authoring: '用例编写', skills: '项目技能', taxonomy: '分类规则', team: '项目成员', smoke: '冒烟执行', 'model-settings': '模型配置' } as Record<string, string>)[view]}</h1><p>{({ home: '项目概览与最近工作', cases: '原值与审核结论独立保存', imports: '追踪文件预览与入库状态', requirements: '从需求材料提取规则与待确认项', authoring: '从分析结论编写可执行用例', skills: '管理当前项目启用的 qa-doc-skills', taxonomy: '项目分类版本与正式选项', team: '成员与项目权限', smoke: '组织核心链路并记录版本执行结果', 'model-settings': '配置本地与远程 AI 服务' } as Record<string, string>)[view]}</p></div>}
        {error && <div role="alert" className="feedback error"><span>{error}</span><button onClick={() => setError('')} aria-label="关闭错误"><X size={16} /></button></div>}
        {notice && <div role="status" className="feedback success"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="关闭提示"><X size={16} /></button></div>}
        {view === 'cases' && suggestionJob?.project_id === projectId && <div className={`suggestion-job-banner ${['failed', 'partial'].includes(suggestionJob.status) ? 'is-error' : ''}`} role="status"><span>{suggestionJob.status === 'failed' ? 'AI 建议失败' : suggestionJob.status === 'partial' ? 'AI 建议部分失败' : `AI 建议 ${suggestionJob.completed}/${suggestionJob.total}`} {suggestionJob.errors.slice(0, 2).map(item => ` · ${item.case_id === null ? '任务' : `用例 ${item.case_id}`}: ${item.message}`)}</span>{['failed', 'partial'].includes(suggestionJob.status) && <Button variant="outline" size="sm" disabled={suggestionBusy} onClick={() => void retrySuggestionJob()}><RefreshCw data-icon="inline-start" />重试</Button>}<Button variant="ghost" size="icon-sm" aria-label="关闭建议任务提示" onClick={() => setSuggestionJob(null)}><X /></Button></div>}
        {!projectId ? <div className="empty-workspace"><FileSpreadsheet size={30} /><h2>还没有项目</h2><p>创建项目后即可开始</p><Button onClick={() => setProjectOpen(true)}><Plus data-icon="inline-start" />新建项目</Button></div> : <Suspense fallback={<div className="boot-state" role="status">正在打开工作区模块…</div>}>{view === 'home' ? <HomePage overview={overview?.project.id === projectId ? overview : null} readonly={activeRole === 'reader'} onNavigate={go} onMyReviews={() => { setChecked([]); setPage(1); navigate(`/projects/${projectId}/cases?assignee=me&status=pending`) }} onPendingSuggestions={() => { setChecked([]); setPage(1); navigate(`/projects/${projectId}/cases?suggestion=pending`) }} onImport={() => { setPreview(null); setImportOpen(true) }} /> : view === 'conversion' ? <Suspense fallback={<div className="boot-state" role="status">正在打开用例转换…</div>}><ConverterHost key={projectId} projectId={projectId} readonly={activeRole === 'reader'} /></Suspense> : view === 'requirements' ? <RequirementsPage key={projectId} projectId={projectId} readonly={activeRole === 'reader'} onOpenAuthoring={() => go('authoring')} /> : view === 'authoring' ? <AuthoringPage key={projectId} projectId={projectId} readonly={activeRole === 'reader'} onOpenCases={() => go('cases')} /> : view === 'skills' ? <SkillsPage key={projectId} projectId={projectId} readonly={activeRole === 'reader'} /> : view === 'taxonomy' ? <TaxonomyPage key={projectId} projectId={projectId} role={activeRole} /> : view === 'team' ? <TeamPage key={projectId} projectId={projectId} role={activeRole} userId={user.id} /> : view === 'smoke' ? <SmokePage key={projectId} projectId={projectId} readonly={activeRole === 'reader'} onOpenCases={() => go('cases')} /> : view === 'model-settings' ? <ModelSettingsPage key={projectId} projectId={projectId} /> : view === 'cases' ? <div className={`work-layout ${detail ? 'has-detail' : ''}`}>
          <CaseTable items={items} total={total} page={page} selected={selected} loading={loading} query={query} readonly={activeRole === 'reader'} checked={checked}
            assigneeFilter={assigneeFilter} statusFilter={statusFilter} suggestionFilter={suggestionFilter} onAssigneeFilter={value => changeCaseFilter('assignee', value)} onStatusFilter={value => changeCaseFilter('status', value)} onSuggestionFilter={value => changeCaseFilter('suggestion', value)}
            onQuery={value => { setQuery(value); setPage(1); setChecked([]) }} onPage={value => { setPage(value); setChecked([]) }} onSelect={selectCase}
            onCheck={(id, value) => setChecked(previous => value ? [...new Set([...previous, id])] : previous.filter(item => item !== id))}
            onCheckPage={value => setChecked(previous => value ? [...new Set([...previous, ...items.map(item => item.id)])] : previous.filter(id => !items.some(item => item.id === id)))}
            onBatchOpen={() => setBatchOpen(true)} onDeleteBatch={() => requestDelete(checked)} onDelete={id => requestDelete([id])} suggestionBusy={suggestionBusy} onRules={() => void generateRules()} onModel={() => void generateModel()} />
          <AnimatePresence mode="wait">{detail && <motion.div key={detail.id} className="detail-motion" initial={{ x: 20 }} animate={{ x: 0 }} exit={{ x: 20 }} transition={{ duration: .22 }}><ReviewPane key={`${detail.id}-${detail.version}`} projectId={projectId} userId={user.id} role={activeRole} item={detail} readonly={activeRole === 'reader'} saving={working} onClose={() => selectCase(null)} onSave={saveReview} onAssign={assignCase} onDelete={() => requestDelete([detail.id])} onSuggestionDecided={afterSuggestion} suggestionRevision={suggestionRevision} /></motion.div>}</AnimatePresence>
        </div> : <div className="imports-list">{imports.map(record => <div key={record.id} className="import-line"><FileSpreadsheet size={18} /><div><strong>{record.filename}</strong><span>{record.created_at.replace('T', ' ').slice(0, 19)}</span></div><span>{record.count} 条</span><span className={record.status === 'done' ? 'reviewed-text' : 'pending-text'}>{record.status === 'done' ? '已入库' : '待确认'}</span></div>)}{imports.length === 0 && <div className="empty-list">尚无导入记录</div>}</div>}</Suspense>}
      </motion.div></AnimatePresence></div>
    </main>
    <Dialog open={projectOpen} onOpenChange={setProjectOpen}><DialogContent><DialogHeader><DialogTitle>新建项目</DialogTitle><DialogDescription>同一项目内的用例 ID 不可重复</DialogDescription></DialogHeader><form onSubmit={createProject} className="project-form"><label>项目名称<Input value={projectName} onChange={event => setProjectName(event.target.value)} maxLength={120} required autoFocus /></label><DialogFooter><Button type="submit">创建项目</Button></DialogFooter></form></DialogContent></Dialog>
    {importOpen && <Suspense fallback={null}><ImportDialog open preview={preview} working={working} onOpenChange={setImportOpen} onFile={onFile} onConfirm={confirmImport} /></Suspense>}
    {projectId && batchOpen && <Suspense fallback={null}><BatchReviewDialog projectId={projectId} open items={items.filter(item => checked.includes(item.id))} onOpenChange={setBatchOpen} onFinished={afterBatch} /></Suspense>}
    <Dialog open={deleteTarget !== null} onOpenChange={open => { if (!open && !working) setDeleteTarget(null) }}>
      <DialogContent>
        <DialogHeader><DialogTitle>确认删除用例</DialogTitle><DialogDescription>删除后原始信息、审核记录、建议与冒烟关联都会移除 此操作无法撤销</DialogDescription></DialogHeader>
        <p className="delete-confirm-copy">将删除 {deleteTarget?.length ?? 0} 条用例</p>
        <DialogFooter><Button variant="outline" disabled={working} onClick={() => setDeleteTarget(null)}>取消</Button><Button variant="destructive" disabled={working} onClick={() => void confirmDelete()}><Trash2 data-icon="inline-start" />{working ? '删除中…' : '确认删除'}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
}

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [checking, setChecking] = useState(true)
  const [startupError, setStartupError] = useState('')
  useEffect(() => { refreshCsrf().then(() => api<User>('/auth/me')).then(setUser).catch(cause => { if (!String(cause.message).includes('请先登录')) setStartupError(cause.message) }).finally(() => setChecking(false)) }, [])
  async function logout() { try { await api('/auth/logout', { method: 'POST' }); await refreshCsrf(); setUser(null); return true } catch (cause) { setStartupError((cause as Error).message); return false } }
  return <BrowserRouter><MotionConfig reducedMotion="user"><AnimatePresence mode="wait">{checking ? <div className="boot-state" role="status">正在连接 Test Atelier…</div> : user ? <motion.div key="workspace" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}><Workspace user={user} onLogout={logout} /></motion.div> : <div key="login"><Login onLogin={setUser} />{startupError && <div className="startup-error" role="alert">无法连接 API：{startupError}</div>}</div>}</AnimatePresence></MotionConfig></BrowserRouter>
}
