import { motion } from 'motion/react'
import { ArrowRight, Archive, FileSpreadsheet, FileText, ListChecks, PlayCircle, Plus, RefreshCw, Upload, WandSparkles, type LucideIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'

export type HomeView = 'cases' | 'imports' | 'requirements' | 'authoring' | 'conversion' | 'smoke'
export type RecentActivity = {
  id: string
  kind: 'import' | 'analysis' | 'draft' | 'conversion' | 'document' | 'run'
  title: string
  status: string
  created_at: string
  view: HomeView
}
export type Overview = {
  project: { id: number; name: string }
  case_count: number
  pending_count: number
  my_pending_count: number
  pending_suggestion_count: number
  analysis_count: number
  draft_count: number
  conversion_count: number
  document_count: number
  import_count: number
  smoke_suite_count: number
  active_run_count: number
  recent_activity: RecentActivity[]
}

const tools: { label: string; detail: string; view: HomeView; icon: LucideIcon }[] = [
  { label: '需求分析', detail: '处理材料与待澄清项', view: 'requirements', icon: FileText },
  { label: '用例编写', detail: '编辑草稿并确认发布', view: 'authoring', icon: WandSparkles },
  { label: '用例转换', detail: '核对 Excel 与 XMind', view: 'conversion', icon: RefreshCw },
  { label: '用例库', detail: '检索与审核项目用例', view: 'cases', icon: ListChecks },
  { label: '冒烟执行', detail: '组织核心链路并记录结果', view: 'smoke', icon: PlayCircle },
]
const activityIcons: Record<RecentActivity['kind'], LucideIcon> = {
  import: Upload, analysis: FileText, draft: WandSparkles, conversion: RefreshCw, document: FileSpreadsheet, run: PlayCircle,
}
const activityLabels: Record<RecentActivity['kind'], string> = {
  import: '导入', analysis: '分析', draft: '编写', conversion: '转换', document: '材料', run: '执行',
}

function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value.replace('T', ' ').slice(0, 16)
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date)
}

export function HomePage({ overview, readonly, onNavigate, onMyReviews, onPendingSuggestions, onImport }: {
  overview: Overview | null
  readonly: boolean
  onNavigate: (view: HomeView) => void
  onMyReviews: () => void
  onPendingSuggestions: () => void
  onImport: () => void
}) {
  const metrics: { label: string; value: number | undefined; detail: string; action: () => void }[] = [
    { label: '用例总数', value: overview?.case_count, detail: '项目用例库', action: () => onNavigate('cases') },
    { label: '待审核', value: overview?.pending_count, detail: '项目待处理', action: () => onNavigate('cases') },
    { label: '待我审核', value: overview?.my_pending_count, detail: '分派给我的用例', action: onMyReviews },
    { label: '待发布草稿', value: overview?.draft_count, detail: '继续编写用例', action: () => onNavigate('authoring') },
  ]
  const assets: { label: string; count: number | undefined; view: HomeView; icon: LucideIcon }[] = [
    { label: '需求材料', count: overview?.document_count, view: 'requirements', icon: FileText },
    { label: '分析记录', count: overview?.analysis_count, view: 'requirements', icon: FileText },
    { label: '导入批次', count: overview?.import_count, view: 'imports', icon: Archive },
    { label: '转换结果', count: overview?.conversion_count, view: 'conversion', icon: RefreshCw },
    { label: '冒烟集', count: overview?.smoke_suite_count, view: 'smoke', icon: PlayCircle },
    { label: '执行中', count: overview?.active_run_count, view: 'smoke', icon: PlayCircle },
  ]

  return <div className="home-view">
    <section className="home-launch" aria-labelledby="home-launch-title">
      <div><h2 id="home-launch-title">从这里开始</h2><p>继续分析需求、整理用例或核对转换结果</p></div>
      <div className="home-launch-actions">
        <Button onClick={() => onNavigate('requirements')}><FileText data-icon="inline-start" />分析需求</Button>
        {!readonly && <Button variant="outline" onClick={onImport}><Upload data-icon="inline-start" />导入用例</Button>}
      </div>
    </section>

    <section className="home-metrics" aria-label="项目概况">
      {metrics.map((metric, index) => <motion.button key={metric.label} type="button" onClick={metric.action}
        initial={{ opacity: 0, y: 9 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .23, delay: index * .045 }} whileTap={{ scale: .985 }}>
        <span>{metric.label}</span><strong>{metric.value ?? '—'}</strong><small>{metric.detail}</small>
        <ArrowRight aria-hidden="true" />
      </motion.button>)}
    </section>

    {overview && overview.pending_suggestion_count > 0 && <button className="home-suggestion-queue" onClick={onPendingSuggestions}><span>待处理建议</span><strong>{overview.pending_suggestion_count}</strong><ArrowRight size={16} /></button>}

    <div className="home-content-grid">
      <section className="home-section home-tools" aria-labelledby="home-tools-title">
        <div className="home-section-heading"><h2 id="home-tools-title">快捷入口</h2><span>选择一项工作继续</span></div>
        <div className="home-tool-grid">{tools.map(({ label, detail, view, icon: Icon }, index) => <motion.button
          key={view} type="button" onClick={() => onNavigate(view)} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
          transition={{ duration: .24, delay: .08 + index * .045 }} whileHover={{ y: -2 }} whileTap={{ scale: .985 }}>
          <span className="home-tool-icon"><Icon size={19} /></span><span className="home-tool-copy"><strong>{label}</strong><small>{detail}</small></span><ArrowRight aria-hidden="true" size={17} />
        </motion.button>)}</div>
      </section>
      <section className="home-section home-assets" aria-labelledby="home-assets-title">
        <div className="home-section-heading"><h2 id="home-assets-title">数据资产</h2><span>当前项目</span></div>
        <div className="home-asset-list">{assets.map(({ label, count, view, icon: Icon }) => <button key={label} type="button" onClick={() => onNavigate(view)}>
          <Icon size={17} /><span>{label}</span><strong>{count ?? '—'}</strong><ArrowRight aria-hidden="true" size={15} />
        </button>)}</div>
        {!readonly && <button className="home-asset-create" type="button" onClick={onImport}><Plus size={16} />导入一份已有用例</button>}
      </section>
    </div>

    <section className="home-section home-activity" aria-labelledby="home-activity-title">
      <div className="home-section-heading"><h2 id="home-activity-title">最近工作</h2><span>按时间排序</span></div>
      <div className="home-activity-header" aria-hidden="true"><span>事项</span><span>状态</span><span>时间</span></div>
      {overview?.recent_activity.length ? overview.recent_activity.map(item => {
        const Icon = activityIcons[item.kind]
        return <button className="home-activity-row" type="button" key={item.id} onClick={() => onNavigate(item.view)}>
          <span className="home-activity-title"><Icon size={16} /><span><strong>{item.title}</strong><small>{activityLabels[item.kind]}</small></span></span>
          <span className={`home-activity-status ${item.status === '失败' ? 'is-error' : ''}`}>{item.status}</span>
          <time dateTime={item.created_at}>{formatTime(item.created_at)}</time>
        </button>
      }) : <div className="home-activity-empty">{overview ? '还没有工作记录' : '正在读取最近工作'}</div>}
    </section>
  </div>
}
