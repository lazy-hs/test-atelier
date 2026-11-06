import { rawApi, type HarnessTaskResult } from '@/lib/api'
import { validateHarnessResultEnvelope } from '@/lib/harnessResult'

export async function downloadHarnessResult(
  projectId: number, taskId: string, expectedSchema: HarnessTaskResult['result_schema'],
): Promise<void> {
  const response = await rawApi(`/projects/${projectId}/harness/tasks/${encodeURIComponent(taskId)}/result`)
  const body = await response.text()
  let envelope: unknown
  try { envelope = JSON.parse(body) } catch { throw new Error('Harness 结果尚未完成或与任务不匹配，无法导出') }
  try {
    validateHarnessResultEnvelope(envelope, { taskId, projectId, expectedSchema })
  } catch {
    throw new Error('Harness 结果尚未完成或与任务不匹配，无法导出')
  }
  const url = URL.createObjectURL(new Blob([body], { type: 'application/json;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `Harness-result-${taskId.replace(/[^a-zA-Z0-9_-]/g, '_')}.json`
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
