import { AlertTriangle, FileSpreadsheet, UploadCloud } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import type { Preview } from '@/lib/api'

type Props = {
  open: boolean; preview: Preview | null; working: boolean
  onOpenChange: (open: boolean) => void; onFile: (file: File) => void; onConfirm: () => void
}

export function ImportDialog({ open, preview, working, onOpenChange, onFile, onConfirm }: Props) {
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="import-dialog">
    <DialogHeader><DialogTitle>导入用例</DialogTitle><DialogDescription>先预览字段与原值，确认后才写入项目</DialogDescription></DialogHeader>
    {!preview ? <label className="upload-area"><UploadCloud size={28} aria-hidden="true" /><strong>{working ? '正在解析文件…' : '选择 Excel 或 XMind 文件'}</strong><span>支持 .xls、.xlsx、.xmind · 最大 10 MB</span><input type="file" accept=".xls,.xlsx,.xmind" disabled={working} onChange={event => { const file = event.target.files?.[0]; if (file) onFile(file); event.target.value = '' }} /></label> : <div className="preview-content">
      <div className="preview-file"><FileSpreadsheet size={18} /><div><strong>{preview.filename}</strong><span>{preview.count} 条用例 · {preview.headers.length} 个原始字段</span></div></div>
      {preview.warnings.map(warning => <Alert key={warning}><AlertTriangle /><AlertDescription>{warning}</AlertDescription></Alert>)}
      <div className="preview-caption">字段映射</div><div className="mapping-list">{preview.headers.map((header, index) => <div key={`${header}-${index}`}><span>{header || `未命名列 ${index + 1}`}</span><span>{index + 1} → 原始字段</span></div>)}</div>
      <div className="preview-caption">前 5 条用例</div><div className="sample-list">{preview.sample.map((row, index) => <div key={index}><code>{row[preview.headers.indexOf('测试用例ID')]}</code><span>{row[preview.headers.indexOf('用例名称')]}</span></div>)}</div>
    </div>}
    {preview && <DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button><Button disabled={working} onClick={onConfirm}>{working ? '正在导入…' : `确认导入 ${preview.count} 条`}</Button></DialogFooter>}
  </DialogContent></Dialog>
}
