import { AnimatePresence, motion } from 'motion/react'
import { CheckCheck, ChevronLeft, ChevronRight, ListChecks, Search, Sparkles, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type { Case } from '@/lib/api'

type Props = {
  items: Case[]; total: number; page: number; selected: number | null; loading: boolean; query: string
  onQuery: (value: string) => void; onPage: (page: number) => void; onSelect: (id: number) => void
  readonly: boolean; checked: number[]; onCheck: (id: number, value: boolean) => void; onCheckPage: (value: boolean) => void
  onBatchOpen: () => void; onDeleteBatch: () => void; onDelete: (id: number) => void; assigneeFilter: string; statusFilter: string
  onAssigneeFilter: (value: string) => void; onStatusFilter: (value: string) => void
  suggestionFilter: string; onSuggestionFilter: (value: string) => void
  suggestionBusy: boolean; onRules: () => void; onModel: () => void
}

export function CaseTable({ items, total, page, selected, loading, query, onQuery, onPage, onSelect,
  readonly, checked, onCheck, onCheckPage, onBatchOpen, onDeleteBatch, onDelete, assigneeFilter, statusFilter,
  onAssigneeFilter, onStatusFilter, suggestionFilter, onSuggestionFilter, suggestionBusy, onRules, onModel }: Props) {
  const allChecked = items.length > 0 && items.every(item => checked.includes(item.id))
  const someChecked = items.some(item => checked.includes(item.id))
  return <section className="case-list" aria-label="用例列表">
    <div className="list-heading">
      <div><h2>用例库</h2><span>{total} 条用例</span></div>
      <div className="list-tools"><div className="search-wrap"><Search size={16} aria-hidden="true" /><Input aria-label="搜索 ID、名称或目录" placeholder="搜索 ID 名称或目录" value={query} onChange={event => onQuery(event.target.value)} /></div>
        <Select value={assigneeFilter} onValueChange={onAssigneeFilter}><SelectTrigger aria-label="负责人筛选"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
          <SelectItem value="all">全部负责人</SelectItem><SelectItem value="me">分派给我</SelectItem><SelectItem value="unassigned">未分派</SelectItem>
        </SelectGroup></SelectContent></Select>
        <Select value={statusFilter} onValueChange={onStatusFilter}><SelectTrigger aria-label="审核状态筛选"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
          <SelectItem value="all">全部状态</SelectItem><SelectItem value="pending">待审核</SelectItem><SelectItem value="reviewed">已审核</SelectItem>
        </SelectGroup></SelectContent></Select>
        <Select value={suggestionFilter} onValueChange={onSuggestionFilter}><SelectTrigger aria-label="建议状态筛选"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
          <SelectItem value="all">全部建议</SelectItem><SelectItem value="pending">待处理建议</SelectItem>
        </SelectGroup></SelectContent></Select></div>
    </div>
    <AnimatePresence>{!readonly && checked.length > 0 && <motion.div className="batch-toolbar" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
      <span>已选择 {checked.length} 条</span><div><Button variant="outline" size="sm" onClick={onRules} disabled={suggestionBusy}><ListChecks data-icon="inline-start" />规则建议</Button><Button variant="outline" size="sm" onClick={onModel} disabled={suggestionBusy}><Sparkles data-icon="inline-start" />AI 建议</Button><Button size="sm" onClick={onBatchOpen}><CheckCheck data-icon="inline-start" />批量审核</Button><Button variant="destructive" size="sm" onClick={onDeleteBatch}><Trash2 data-icon="inline-start" />批量删除</Button></div>
    </motion.div>}</AnimatePresence>
    <div className="table-scroll">
      <Table>
        <TableHeader><TableRow>{!readonly && <TableHead className="w-[42px]"><Checkbox aria-label="选择当前页" checked={allChecked ? true : someChecked ? 'indeterminate' : false} onCheckedChange={value => onCheckPage(value === true)} /></TableHead>}<TableHead className="w-[106px]">用例 ID</TableHead><TableHead>用例名称</TableHead><TableHead className="w-[70px]">等级</TableHead><TableHead className="w-[86px]">冒烟</TableHead><TableHead className="w-[105px]">负责人</TableHead><TableHead className="w-[65px]">建议</TableHead><TableHead className="w-[86px]">审核</TableHead>{!readonly && <TableHead className="w-[54px]">操作</TableHead>}</TableRow></TableHeader>
        <TableBody>
          {items.map(item => <motion.tr key={item.id} layout initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }} transition={{ duration: .2 }}
            className={`case-row ${selected === item.id ? 'selected' : ''}`} tabIndex={0} aria-selected={selected === item.id} onClick={() => onSelect(item.id)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(item.id) } }}>
            {!readonly && <TableCell onClick={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()}><Checkbox aria-label={`选择用例 ${item.original_id}`} checked={checked.includes(item.id)} onCheckedChange={value => onCheck(item.id, value === true)} /></TableCell>}
            <TableCell className="id-cell">{item.original_id}</TableCell><TableCell><div className="case-title">{item.title}</div><div className="case-directory">{item.directory || '未设置目录'}</div></TableCell>
            <TableCell><span className="level-mark">{item.final_level || item.level || '—'}</span></TableCell>
            <TableCell><span className={(item.final_smoke ?? item.smoke) ? '' : 'pending-text'}>{(item.final_smoke ?? item.smoke) || '待判定'}</span></TableCell>
            <TableCell><span className={item.assignee ? '' : 'pending-text'} title={item.assignee ?? ''}>{item.assignee ?? '未分派'}</span></TableCell>
            <TableCell>{item.pending_suggestion_count > 0 ? <span className="suggestion-count">{item.pending_suggestion_count} 条</span> : '—'}</TableCell>
            <TableCell><span className={item.review_status === 'reviewed' ? 'reviewed-text' : 'pending-text'}>{item.review_status === 'reviewed' ? '已审核' : '待审核'}</span></TableCell>
            {!readonly && <TableCell onClick={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()}><Button variant="destructive" size="icon-sm" aria-label={`删除用例 ${item.original_id}`} title="删除用例" onClick={() => onDelete(item.id)}><Trash2 /></Button></TableCell>}
          </motion.tr>)}
        </TableBody>
      </Table>
      {!loading && items.length === 0 && <div className="empty-list">{query || assigneeFilter !== 'all' || statusFilter !== 'all' || suggestionFilter !== 'all' ? '没有匹配的用例' : '尚无用例  请先导入文件'}</div>}
      {loading && <div className="empty-list" role="status">正在读取用例…</div>}
    </div>
    <div className="list-footer"><span>第 {page} 页 · 每页 30 条</span><div className="footer-actions"><Button size="icon-sm" variant="ghost" aria-label="上一页" title="上一页" disabled={page === 1} onClick={() => onPage(page - 1)}><ChevronLeft /></Button><Button size="icon-sm" variant="ghost" aria-label="下一页" title="下一页" disabled={page * 30 >= total} onClick={() => onPage(page + 1)}><ChevronRight /></Button></div></div>
  </section>
}
