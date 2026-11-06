import { useCallback, useEffect, useState } from 'react'
import { motion } from 'motion/react'
import { useNavigate } from 'react-router'
import { AlertCircle, BookOpen, Download, FileCheck2, FileWarning, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { api, rawApi, type ExecutionStatistics, type SmokeSuite } from '@/lib/api'
import { ExecutionDefectsDialog } from './ExecutionDefectsDialog'

function minutes(seconds: number) { return `${(seconds / 60).toFixed(1)} 分钟` }
function date(value: string) { return new Date(value).toLocaleString('zh-CN') }

export function ExecutionReportsPage({ projectId, readonly }: { projectId: number; readonly: boolean }) {
  const navigate = useNavigate()
  const base = `/projects/${projectId}`
  const [suites, setSuites] = useState<SmokeSuite[]>([])
  const [suiteId, setSuiteId] = useState('all')
  const [targetVersion, setTargetVersion] = useState('')
  const [environment, setEnvironment] = useState('')
  const [reportId, setReportId] = useState('')
  const [stats, setStats] = useState<ExecutionStatistics | null>(null)
  const [loadedKey, setLoadedKey] = useState('')
  const [refreshId, setRefreshId] = useState(0)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [defectRunId, setDefectRunId] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const filterKey = JSON.stringify([suiteId, targetVersion.trim(), environment.trim(), refreshId])
  const currentStats = loadedKey === filterKey ? stats : null

  useEffect(() => {
    let active = true
    void api<SmokeSuite[]>(`${base}/smoke-suites`).then(value => { if (active) setSuites(value) })
      .catch(cause => { if (active) setError(cause.message) })
    return () => { active = false }
  }, [base])
  const load = useCallback(async (params: URLSearchParams) => {
    return api<ExecutionStatistics>(`${base}/execution-statistics?${params}`)
  }, [base])
  useEffect(() => {
    let active = true
    const params = new URLSearchParams()
    if (suiteId !== 'all') params.set('suite_id', suiteId)
    if (targetVersion.trim()) params.set('target_version', targetVersion.trim())
    if (environment.trim()) params.set('environment', environment.trim())
    const timer = window.setTimeout(() => {
      void load(params).then(value => { if (active) { setStats(value); setLoadedKey(filterKey); setError('') } })
        .catch(cause => { if (active) { setStats(null); setLoadedKey(filterKey); setError(cause.message) } })
    }, 220)
    return () => { active = false; window.clearTimeout(timer) }
  }, [load, suiteId, targetVersion, environment, refreshId, filterKey])

  async function download(runId: number) {
    setBusyId(runId); setError(''); setNotice('')
    try {
      const response = await rawApi(`${base}/execution-runs/${runId}/report`)
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = url; link.download = `execution-report-${runId}.json`
      document.body.appendChild(link); link.click(); link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      setNotice(`执行 #${runId} 报告已下载，可用文件内的 SHA-256 摘要复核`)
    } catch (cause) { setError((cause as Error).message) } finally { setBusyId(null) }
  }

  return <motion.section className="execution-reports" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}>
    <div className="execution-reports-intro"><div><span className="execution-reports-kicker">QUALITY / EXECUTION</span><h2>完成执行的证据</h2><p>按项目核对结果与历史快照 不将进行中的记录写入通过率</p></div><Button size="sm" variant="outline" onClick={() => setRefreshId(value => value + 1)}><RefreshCw data-icon="inline-start" />刷新数据</Button></div>
    {error && loadedKey === filterKey && <div className="ai-alert error" role="alert"><AlertCircle size={16} />{error}</div>}
    {notice && <div className="ai-alert" role="status">{notice}</div>}
    <div className="execution-reports-filters"><Select value={suiteId} onValueChange={setSuiteId}><SelectTrigger aria-label="按冒烟集筛选"><SelectValue placeholder="全部冒烟集" /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="all">全部冒烟集</SelectItem>{suites.map(item => <SelectItem value={String(item.id)} key={item.id}>{item.title}</SelectItem>)}</SelectGroup></SelectContent></Select>
      <Input aria-label="按目标版本筛选" value={targetVersion} maxLength={80} onChange={event => setTargetVersion(event.target.value)} placeholder="目标版本 精确匹配" />
      <Input aria-label="按环境筛选" value={environment} maxLength={80} onChange={event => setEnvironment(event.target.value)} placeholder="环境 精确匹配" />
      <Button variant="ghost" size="sm" onClick={() => { setSuiteId('all'); setTargetVersion(''); setEnvironment('') }}>清除筛选</Button></div>
    {!currentStats ? !error || loadedKey !== filterKey ? <div className="execution-reports-empty" role="status">正在读取已完成的执行记录</div> : null : <>
      <div className="execution-reports-metrics"><div><span>完成执行</span><strong>{currentStats.run_count}</strong><small>次</small></div><div><span>用例执行</span><strong>{currentStats.case_executions}</strong><small>次 非去重</small></div><div><span>通过率</span><strong>{currentStats.pass_rate === null ? '—' : `${currentStats.pass_rate}%`}</strong><small>不含跳过与未执行</small></div></div>
      {currentStats.unavailable_runs > 0 && <div className="ai-alert warning" role="status"><AlertCircle size={16} />另有 {currentStats.unavailable_runs} 次历史执行未能证明快照完整 未纳入统计；如仍有可下载记录 文件内会标记未验证</div>}
      <div className="execution-reports-breakdown"><p>{currentStats.scope}</p><div>{(['passed', 'failed', 'blocked', 'skipped'] as const).map(key => <span key={key}>{({ passed: '通过', failed: '失败', blocked: '阻塞', skipped: '跳过' })[key]} <strong>{currentStats.results[key]}</strong></span>)}</div><small>预计 {minutes(currentStats.estimated_seconds)} / 实际 {minutes(currentStats.actual_seconds)}</small></div>
      <div className="execution-reports-list-title"><div><h3>最近完成</h3><p>最近 20 次 按完成记录查看可复核的 JSON 报告</p></div><div className="execution-reports-direct"><Input type="number" min="1" step="1" aria-label="执行记录 ID" value={reportId} onChange={event => setReportId(event.target.value)} placeholder="更早的执行 ID" /><Button variant="outline" size="sm" disabled={!/^\d+$/.test(reportId) || !Number.isSafeInteger(Number(reportId)) || Number(reportId) < 1 || busyId !== null} onClick={() => void download(Number(reportId))}><Download data-icon="inline-start" />按 ID 下载</Button><Button variant="ghost" size="sm" disabled={!/^\d+$/.test(reportId) || !Number.isSafeInteger(Number(reportId)) || Number(reportId) < 1} onClick={() => setDefectRunId(Number(reportId))}><FileWarning data-icon="inline-start" />按 ID 查缺陷</Button>{!readonly && <Button variant="ghost" size="sm" disabled={!/^\d+$/.test(reportId) || !Number.isSafeInteger(Number(reportId)) || Number(reportId) < 1} onClick={() => navigate(`/projects/${projectId}/knowledge?source=execution_report&id=${Number(reportId)}`)}><BookOpen data-icon="inline-start" />按 ID 沉淀</Button>}</div></div>
      <div className="execution-reports-list">{currentStats.recent_runs.map((item, index) => <motion.div key={item.run_id} className="execution-reports-row" initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: Math.min(index, 12) * .025 }}><div className="execution-reports-icon"><FileCheck2 size={17} /></div><div className="execution-reports-row-copy"><strong>{item.title}</strong><span>#{item.run_id} · 修订 {item.suite_revision} · {item.target_version} / {item.environment}</span></div><div className="execution-reports-row-results"><strong>{item.results.passed} 通过</strong><span>{item.results.failed} 失败 · {item.results.blocked} 阻塞 · {item.results.skipped} 跳过</span></div><time>{date(item.completed_at)}</time><div className="execution-reports-row-actions"><Button size="sm" variant="outline" disabled={busyId !== null} onClick={() => void download(item.run_id)}><Download data-icon="inline-start" />下载报告</Button><Button size="sm" variant="ghost" onClick={() => setDefectRunId(item.run_id)}><FileWarning data-icon="inline-start" />关联缺陷</Button>{!readonly && <Button size="sm" variant="ghost" onClick={() => navigate(`/projects/${projectId}/knowledge?source=execution_report&id=${item.run_id}`)}><BookOpen data-icon="inline-start" />沉淀知识</Button>}</div></motion.div>)}</div>
      {currentStats.run_count === 0 && <div className="execution-reports-empty">当前筛选条件下没有已完成的执行{!readonly && <Button size="sm" variant="outline" onClick={() => navigate(`/projects/${projectId}/smoke`)}>打开冒烟执行</Button>}</div>}
    </>}
    {defectRunId !== null && <ExecutionDefectsDialog key={defectRunId} projectId={projectId} runId={defectRunId} readonly={readonly} onClose={() => setDefectRunId(null)} />}
  </motion.section>
}
