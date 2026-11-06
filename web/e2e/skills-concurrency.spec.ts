import { expect, test } from '@playwright/test'

test('project skills recover two concurrent confirmations independently', async ({ page }) => {
  test.skip(process.env.TEST_ATELIER_HARNESS_E2E !== '1' || !process.env.AI_STUDIO_E2E_PASSWORD,
    'Requires the disposable Test Atelier browser smoke server')

  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(process.env.AI_STUDIO_E2E_PASSWORD!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('button', { name: '新建项目' }).first().click()
  await page.getByLabel('项目名称').fill(`并发技能验收-${Date.now()}`)
  await page.getByRole('button', { name: '创建项目' }).click()

  const ids = ['concurrent-a', 'concurrent-b'] as const
  const confirmed = new Set<string>()
  const subscriptions = new Set<string>()
  const task = (id: string) => ({
    task_id: id, task_type: 'skill-execution', skill_id: id, task_version: 'v1',
    status: confirmed.has(id) ? 'completed' : 'waiting_confirmation',
    result_schema: 'skill-execution.v1', session_id: null, session_ids: [],
    source_document_ids: [], retry_of: null, analysis_run_id: null,
    sequence: confirmed.has(id) ? 3 : 1, created_at: '2026-10-26T00:00:00Z', updated_at: '2026-10-26T00:00:00Z',
  })
  await page.route('**/api/v1/projects/*/harness/config', route => route.fulfill({ json: {
    enabled: true, task_types: ['skill-execution'], result_schemas: ['skill-execution.v1'], skills: [],
  } }))
  await page.route('**/api/v1/projects/*/harness/tasks?limit=100', route => route.fulfill({ json: ids.map(task) }))
  await page.route('**/api/v1/projects/*/harness/tasks/*/events?after=*', route => {
    const url = new URL(route.request().url())
    const id = url.pathname.split('/').at(-2)!
    const after = Number(url.searchParams.get('after'))
    subscriptions.add(id)
    const frame = (sequence: number, event: string, data: Record<string, unknown>) =>
      `id: ${sequence}\nevent: ${event}\ndata: ${JSON.stringify({ task_id: id, sequence, ...data })}\n\n`
    const body = after === 0
      ? frame(1, 'task.confirmation_required', { confirmation: { confirmation_id: `confirm-${id}`, prompt: `确认 ${id}` } })
      : confirmed.has(id) && after < 3
        ? frame(2, 'task.progress', { kind: 'confirmation_submitted', status: 'running' }) + frame(3, 'task.completed', {})
        : ''
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body })
  })
  await page.route('**/api/v1/projects/*/harness/tasks/*', route => {
    const url = new URL(route.request().url())
    const id = url.pathname.split('/').at(-1)!
    if (!ids.includes(id as typeof ids[number])) return route.continue()
    return route.fulfill({ json: task(id) })
  })
  await page.route('**/api/v1/projects/*/harness/tasks/*/confirm', route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2)!
    const payload = route.request().postDataJSON() as { confirmation_id: string; decision: string }
    expect(payload).toEqual({ confirmation_id: `confirm-${id}`, decision: 'approve' })
    confirmed.add(id)
    return route.fulfill({ json: { ...task(id), status: 'running' } })
  })

  await page.getByRole('button', { name: '项目技能' }).click()
  const dialog = page.getByRole('dialog', { name: 'Harness 请求确认' })
  await expect(dialog).toContainText('待处理 2 项')
  expect([...subscriptions].sort()).toEqual([...ids])
  await page.reload()
  await expect(dialog).toContainText('待处理 2 项')
  await page.screenshot({ path: '/tmp/test-atelier-concurrent-confirmations.png' })
  for (let index = 0; index < ids.length; index++) {
    const visibleId = (await dialog.innerText()).match(/确认 (concurrent-[ab])/)?.[1]
    expect(visibleId).toBeTruthy()
    await dialog.getByRole('button', { name: '允许继续' }).click()
    await expect(page.locator('.harness-skill-run').filter({ hasText: visibleId! })).toContainText('已完成')
    if (index === 0) {
      await expect(dialog).toBeVisible()
      await expect(dialog).not.toContainText(`确认 ${visibleId}`)
    }
  }
  await expect(dialog).toHaveCount(0)
  expect([...confirmed].sort()).toEqual([...ids])
  const resultTaskId = ids[0]
  const resultPath = `**/api/v1/projects/*/harness/tasks/${resultTaskId}/result`
  await page.route(resultPath, route => {
    const url = new URL(route.request().url())
    const projectId = Number(url.pathname.split('/').at(4))
    return route.fulfill({ json: {
      task_id: resultTaskId, project_id: projectId, analysis_run_id: null,
      source_document_ids: [], status: 'completed', result_schema: 'skill-execution.v1', error: {},
      result: { schema: 'skill-execution.v1', task_id: 'stale-task', session_id: `dsh-${resultTaskId}` },
    } })
  })
  await page.getByRole('button', { name: '查看结果' }).first().click()
  await expect(page.getByRole('alert')).toContainText('Harness 完成结果校验失败，未显示原始结果')
  await page.unroute(resultPath)
  await page.route(resultPath, route => {
    const url = new URL(route.request().url())
    const projectId = Number(url.pathname.split('/').at(4))
    return route.fulfill({ json: {
      task_id: resultTaskId, project_id: projectId, analysis_run_id: null,
      source_document_ids: [], status: 'completed', result_schema: 'skill-execution.v1', error: {},
      result: { schema: 'skill-execution.v1', task_id: resultTaskId, session_id: `dsh-${resultTaskId}`,
        result_id: 'skill-result', generated_at: '2026-10-27T00:00:00Z', skill_id: resultTaskId,
        source_document_ids: [], warnings: [], data: { output: 'validated skill result' } },
    } })
  })
  await page.getByRole('button', { name: '查看结果' }).first().click()
  await expect(page.getByRole('dialog', { name: 'Harness 结果' })).toContainText('validated skill result')
  await page.getByRole('button', { name: '关闭' }).click()
  await page.unroute(resultPath)
  expect(errors).toEqual([])
})
