import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import { createHash } from 'node:crypto'
import { open, readFile } from 'node:fs/promises'

async function downloadCompletedResult(page: Page, schema: string) {
  const pending = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载原始 JSON' }).click()
  const file = await pending
  const result = JSON.parse(await readFile(await file.path(), 'utf8')) as {
    task_id: string; project_id: number; analysis_run_id: string | null; source_document_ids: string[];
    status: string; result_schema: string; result: { schema: string; task_id: string }
  }
  expect(file.suggestedFilename()).toBe(`Harness-result-${result.task_id}.json`)
  expect(result).toEqual(expect.objectContaining({
    task_id: expect.any(String), project_id: expect.any(Number), status: 'completed',
    result_schema: schema, source_document_ids: expect.any(Array), result: expect.any(Object),
  }))
  expect(Object.hasOwn(result, 'analysis_run_id')).toBe(true)
  expect(result.result.schema).toBe(schema)
  expect(result.result.task_id).toBe(result.task_id)
  return result
}

async function confirmAnalysisCheckpoint(page: Page, expectsAmbiguity = false) {
  if (process.env.TEST_ATELIER_HARNESS_CHECKPOINT_E2E !== '1') return
  const checkpoint = page.getByRole('dialog').filter({ hasText: 'Phase 2.5 解析确认' })
  await expect(checkpoint).toContainText('解析完成', { timeout: 30_000 })
  await expect(checkpoint).toContainText('适用端待确认')
  await page.getByLabel('解析澄清或适用端').fill('仅 Web 适用')
  await checkpoint.getByRole('button', { name: '确认解析后继续' }).click()
  if (expectsAmbiguity) {
    const ambiguity = page.getByRole('dialog').filter({ hasText: 'Phase 2.6 关键歧义' })
    await expect(ambiguity).toContainText('连续失败五次后锁定账号', { timeout: 30_000 })
    await expect(page.locator('.harness-stream-panel')).toHaveCSS('opacity', '1')
    await expect(ambiguity).toHaveCSS('opacity', '1')
    await page.screenshot({ path: '/tmp/test-atelier-ambiguity-checkpoint.png' })
    await ambiguity.getByRole('button', { name: '标记待确认' }).click()
    await expect(ambiguity).toHaveCount(0)
  }
}

/**
 * Run this against a disposable Test Atelier database and a fake Harness Host
 * (or a dedicated staging Host), never against a production project. The
 * runner must set TEST_ATELIER_HARNESS_E2E=1 after starting both processes.
 */
test('Harness analysis and authoring survive refresh with Session replay', async ({ page }) => {
  test.skip(process.env.TEST_ATELIER_HARNESS_E2E !== '1', 'Set TEST_ATELIER_HARNESS_E2E=1 for the disposable Harness Host flow')
  const password = process.env.AI_STUDIO_E2E_PASSWORD
  test.skip(!password, 'Set AI_STUDIO_E2E_PASSWORD for the local server')

  const errors: string[] = []
  let unsolicitedImageRequests = 0
  await page.route('**/harness-asset-probe.invalid/**', route => {
    unsolicitedImageRequests += 1
    return route.fulfill({ status: 204 })
  })
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工作台' })).toBeVisible()

  await page.getByRole('button', { name: '新建项目' }).first().click()
  await page.getByLabel('项目名称').fill(`Harness 浏览器验收-${Date.now()}`)
  await page.getByRole('button', { name: '创建项目' }).click()

  await page.getByRole('button', { name: '需求分析' }).first().click()
  await expect(page.getByLabel('分析模型')).toHaveCount(0)
  await page.locator('input[type=file]').setInputFiles({
    name: 'harness-e2e.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('# 登录\n正确账号可以进入首页\n连续失败五次后锁定账号'),
  })
  await expect(page.getByText('提取内容预览')).toBeVisible()
  const refusedKeys: Record<string, string[]> = {}
  const acceptedKeys: Record<string, string> = {}
  await page.route('**/api/v1/projects/*/harness/tasks', async route => {
    const request = route.request()
    if (request.method() !== 'POST') return route.continue()
    const payload = JSON.parse(request.postData() ?? '{}') as { task_type?: string; idempotency_key?: string }
    const kind = payload.task_type ?? ''
    if (!['requirement-analysis', 'test-cases'].includes(kind)) return route.continue()
    const refused = refusedKeys[kind] ?? (refusedKeys[kind] = [])
    if (refused.length < (kind === 'requirement-analysis' ? 2 : 1)) {
      refused.push(payload.idempotency_key ?? '')
      const detail = kind === 'test-cases' ? 'input_timeout'
        : refused.length === 1 ? 'window_policy_mismatch' : 'queue_full'
      return route.fulfill({ status: detail === 'queue_full' ? 503 : 409, contentType: 'application/json',
        body: JSON.stringify({ detail }) })
    }
    if (!acceptedKeys[kind]) acceptedKeys[kind] = payload.idempotency_key ?? ''
    return route.continue()
  })
  // Simulate a dropped first browser subscription. The next request must
  // replay durable events instead of starting another Harness task.
  let interrupted = false
  await page.route('**/api/v1/projects/*/harness/tasks/*/events?after=*', async route => {
    if (interrupted) return route.continue()
    interrupted = true
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: 'event: task.transport_error\ndata: {"error":{"message":"Harness 连接中断，正在等待恢复"}}\n\n',
    })
  })
  await page.getByRole('button', { name: '开始分析' }).click()
  await expect(page.getByText('Harness 与 Test Atelier 的分窗配置不一致，请联系管理员核对')).toBeVisible()
  await page.getByRole('button', { name: '开始分析' }).click()
  await expect(page.getByText('Harness 当前并发容量已满，请稍后重试')).toBeVisible()
  await page.getByRole('button', { name: '开始分析' }).click()
  await expect(page.getByText('DeepSeek Harness 输出')).toBeVisible({ timeout: 30_000 })
  expect(new Set([...refusedKeys['requirement-analysis'], acceptedKeys['requirement-analysis']]).size).toBe(3)
  await confirmAnalysisCheckpoint(page, process.env.TEST_ATELIER_HARNESS_REAL_DSH_E2E === '1')
  await expect(page.getByTestId('harness-analysis-result')).toContainText('"title": "登录"', { timeout: 30_000 })
  const analysisEnvelope = await downloadCompletedResult(page, 'requirement-analysis.v1')
  expect(typeof analysisEnvelope.analysis_run_id).toBe('string')
  if (process.env.TEST_ATELIER_HARNESS_REAL_DSH_E2E !== '1') {
    const routePattern = `**/harness/tasks/${analysisEnvelope.task_id}/result`
    await page.route(routePattern, route => route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ...analysisEnvelope, status: 'running', result: {} }) }))
    let prematureDownloads = 0
    page.on('download', () => { prematureDownloads += 1 })
    await page.getByRole('button', { name: '下载原始 JSON' }).click()
    await expect(page.getByRole('alert')).toContainText('Harness 结果尚未完成或与任务不匹配，无法导出')
    expect(prematureDownloads).toBe(0)
    await page.unroute(routePattern)
    await page.route(routePattern, route => route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ...analysisEnvelope, result_schema: 'test-cases.v1',
        result: { ...analysisEnvelope.result, schema: 'test-cases.v1' } }) }))
    await page.getByRole('button', { name: '下载原始 JSON' }).click()
    await expect(page.getByRole('alert')).toContainText('Harness 结果尚未完成或与任务不匹配，无法导出')
    expect(prematureDownloads).toBe(0)
    await page.unroute(routePattern)
    await downloadCompletedResult(page, 'requirement-analysis.v1')
    // A completed task must still be rejected in the browser when a stale
    // response crosses the task boundary; the UI must not render it as raw
    // Harness output merely because the envelope says "completed".
    await page.route(routePattern, route => route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ...analysisEnvelope, status: 'completed', result: { ...analysisEnvelope.result, task_id: 'stale-task' } }) }))
    await page.reload()
    await expect(page.getByRole('alert')).toContainText('Harness 完成结果校验失败，未显示原始结果')
    await expect(page.locator('pre[aria-label="Harness 完成结果"]')).toContainText('任务完成后显示 Harness 原始结果')
    await page.unroute(routePattern)
    await page.reload()
    await expect(page.getByTestId('harness-analysis-result')).toContainText('"title": "登录"', { timeout: 15_000 })
  }
  if (process.env.TEST_ATELIER_HARNESS_CHECKPOINT_E2E === '1'
    && process.env.TEST_ATELIER_HARNESS_REAL_DSH_E2E === '1') {
    const humanDecision = page.locator('.ambiguity-row').filter({ hasText: 'AMB-1' })
    await expect(humanDecision).toBeVisible()
    await humanDecision.getByPlaceholder('填写确认结论').fill('验收前按产品最终规则确认')
    await humanDecision.getByRole('button', { name: '确认' }).click()
  }
  if (process.env.TEST_ATELIER_HARNESS_REAL_DSH_E2E !== '1') {
    await expect(page.getByTestId('harness-analysis-report')).toContainText('正确账号可以进入首页')
    await expect(page.getByTestId('harness-analysis-report')).toContainText('外链图示')
    await expect(page.getByTestId('harness-analysis-report').locator('img')).toHaveCount(0)
    expect(unsolicitedImageRequests).toBe(0)
    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('button', { name: '下载 Markdown' }).click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toMatch(/^Harness-analysis-.+\.md$/)
    expect(await readFile(await download.path(), 'utf8')).toBe('# 登录需求分析\n\n正确账号可以进入首页\n\n![外链图示](https://harness-asset-probe.invalid/image.png?fixture=report)\n')
  }
  await expect(page.getByRole('heading', { name: '报告正文' })).toHaveCount(0)
  await page.screenshot({ path: '/tmp/test-atelier-harness-desktop.png' })
  expect(interrupted).toBe(true)

  const sessionButton = page.getByRole('button', { name: '查看 Session' }).first()
  await expect(sessionButton).toBeVisible()
  await sessionButton.click()
  const sessionDialog = page.getByRole('dialog')
  await expect(sessionDialog).toContainText('Session')
  // Regression guard: the replay list must retain its intrinsic row height.
  // A CSS grid previously collapsed every <details> item to a 2px rule, so
  // the Session appeared to contain only blank horizontal lines.
  await expect(sessionDialog.locator('.harness-session-events details').first()).toBeVisible()
  await expect(sessionDialog.locator('.harness-session-events summary').first()).toContainText('事件 1')
  if (process.env.TEST_ATELIER_HARNESS_REAL_DSH_E2E === '1') {
    await expect(page.getByRole('dialog')).toContainText('v4')
    await expect(page.getByRole('dialog')).toContainText('个事件')
    await expect(page.getByRole('dialog').getByRole('alert')).toHaveCount(0)
  }
  await page.keyboard.press('Escape')

  // Pretend this task fell outside the recent-20 window; the analysis-bound
  // lookup must still restore its Session and raw result after navigation.
  const recentAnalysisTasks = '**/harness/tasks?task_type=requirement-analysis&limit=20'
  let filteredAnalysisHistory = false
  await page.route(recentAnalysisTasks, async route => {
    filteredAnalysisHistory = true
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  })
  await page.reload()
  await expect(page.getByRole('heading', { name: '需求分析' })).toBeVisible()
  await expect(page.getByTestId('harness-analysis-result')).toContainText('"title": "登录"', { timeout: 15_000 })
  await expect(page.getByRole('button', { name: '查看 Session' }).first()).toBeVisible()
  expect(filteredAnalysisHistory).toBe(true)
  await page.unroute(recentAnalysisTasks)
  await page.getByRole('button', { name: '历史分析' }).click()
  await expect(page.getByRole('heading', { name: '历史分析' })).toBeVisible()
  await expect(page.getByTestId('harness-analysis-result')).toHaveCount(0)
  await page.getByRole('button', { name: '需求分析' }).first().click()
  await expect(page.getByTestId('harness-analysis-result')).toContainText('"title": "登录"')
  await page.getByRole('button', { name: '进入用例编写' }).click()
  await expect(page.getByRole('heading', { name: '用例编写' })).toBeVisible()
  await expect(page.getByLabel('生成模型')).toHaveCount(0)
  await page.getByRole('button', { name: '运行 Harness' }).click()
  await expect(page.getByText('Harness 分段上传超时，请重新发起任务')).toBeVisible()
  await page.getByRole('button', { name: '运行 Harness' }).click()
  await expect(page.getByText('DeepSeek Harness 输出')).toBeVisible({ timeout: 30_000 })
  expect(acceptedKeys['test-cases']).not.toBe(refusedKeys['test-cases'][0])
  if (process.env.TEST_ATELIER_HARNESS_CASE_CHECKPOINT_E2E === '1') {
    const checkpoint = page.getByRole('dialog', { name: 'Harness 用例确认' })
    await expect(checkpoint).toContainText('Phase 2.5 用例解析确认', { timeout: 30_000 })
    await page.reload()
    await expect(checkpoint).toContainText('Phase 2.5 用例解析确认', { timeout: 30_000 })
    if (await checkpoint.getByText('适用端：待选择').count()) await page.getByLabel('用例适用端或解析澄清').fill('Web')
    await checkpoint.getByRole('button', { name: '确认用例解析后继续' }).click()
    if (process.env.TEST_ATELIER_HARNESS_CASE_AMBIGUITY_E2E === '1') {
      const ambiguity = page.getByRole('dialog', { name: '用例关键歧义逐项决定' })
      await expect(ambiguity).toContainText('Phase 2.6 用例关键歧义', { timeout: 30_000 })
      await ambiguity.getByRole('button', { name: '接受建议' }).click()
    }
    const selection = page.getByRole('dialog', { name: '指定单条用例适用端' })
    await expect(selection).toContainText('用例适用端逐条指定', { timeout: 30_000 })
    await page.reload()
    await expect(selection).toBeVisible({ timeout: 30_000 })
    if (process.env.TEST_ATELIER_HARNESS_CASE_USER_EXCEPTION_E2E === '1') {
      await page.screenshot({ path: '/tmp/test-atelier-case-exception-selection-desktop.png' })
      await page.setViewportSize({ width: 390, height: 844 })
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
      await page.screenshot({ path: '/tmp/test-atelier-case-exception-selection-mobile.png' })
      await page.setViewportSize({ width: 1280, height: 720 })
      await selection.getByLabel('指定例外用例 ID').fill('TC-2')
      await selection.getByLabel('指定例外适用端').fill('Pc')
      await selection.getByRole('button', { name: '提交单条例外' }).click()
    } else {
      await selection.getByRole('button', { name: '无额外例外 继续' }).click()
    }
  }
  if (process.env.TEST_ATELIER_HARNESS_CASE_EXCEPTION_E2E === '1') {
    const exception = page.getByRole('dialog', { name: '用例适用端例外确认' })
    await expect(exception).toContainText('TC-2', { timeout: 30_000 })
    await expect(exception).toContainText(/本次适用端\s*Web/)
    await expect(exception).toContainText(/例外适用端\s*Pc/)
    await page.reload()
    await expect(exception).toContainText('TC-2', { timeout: 30_000 })
    await page.screenshot({ path: '/tmp/test-atelier-case-platform-exception-desktop.png' })
    await page.setViewportSize({ width: 390, height: 844 })
    await expect(exception).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
    await page.screenshot({ path: '/tmp/test-atelier-case-platform-exception-mobile.png' })
    await page.setViewportSize({ width: 1280, height: 720 })
    await page.getByLabel('确认单条例外适用端').fill('Pc')
    await exception.getByRole('button', { name: '批准单条例外' }).click()
  }
  await expect(page.getByTestId('harness-cases-result')).toContainText('"title": "有效账号登录"', { timeout: 30_000 })
  const casesEnvelope = await downloadCompletedResult(page, 'test-cases.v1')
  expect(casesEnvelope.project_id).toBe(analysisEnvelope.project_id)
  expect(casesEnvelope.analysis_run_id).toBe(analysisEnvelope.analysis_run_id)
  expect(casesEnvelope.source_document_ids).toEqual(analysisEnvelope.source_document_ids)
  if (process.env.TEST_ATELIER_HARNESS_CASE_EXCEPTION_E2E === '1') {
    await expect(page.getByTestId('harness-cases-result')).toContainText(/"labels": \[\s*"Pc"/)
  }
  await expect(page.getByRole('button', { name: /有效账号登录/ })).toHaveCount(0)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByTestId('harness-cases-result')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await page.screenshot({ path: '/tmp/test-atelier-harness-cases-mobile.png' })
  await page.setViewportSize({ width: 1280, height: 720 })
  const skillXmind = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Harness 结果导出 XMind' }).click()
  const artifact = await skillXmind
  expect(artifact.suggestedFilename()).toMatch(/^Harness-cases-[a-z0-9_-]+\.xmind$/i)
  const handle = await open((await artifact.path())!)
  try {
    const signature = Buffer.alloc(2)
    await handle.read(signature, 0, 2, 0)
    expect(signature.toString()).toBe('PK')
  } finally { await handle.close() }
  await page.screenshot({ path: '/tmp/test-atelier-harness-xmind.png' })

  await page.getByRole('button', { name: '草稿审核' }).first().click()
  await expect(page.getByRole('heading', { name: '草稿审核', exact: true })).toBeVisible()
  await expect(page.getByText('非 Harness 原始结果')).toBeVisible()
  await expect(page.getByTestId('harness-cases-result')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /有效账号登录/ })).toBeVisible({ timeout: 15_000 })
  await page.getByRole('button', { name: /有效账号登录/ }).click()
  await page.getByRole('textbox', { name: '标题' }).fill('人工确认登录')
  await page.getByRole('button', { name: '保存草稿' }).click()
  await expect(page.getByText('草稿已保存')).toBeVisible()
  page.once('dialog', dialog => void dialog.accept())
  await page.getByRole('button', { name: '发布到用例库' }).click()
  await expect(page.getByText('已发布到用例库')).toBeVisible()
  await page.getByRole('button', { name: '查看 Harness 结果' }).click()
  await expect(page.getByRole('heading', { name: '用例编写' })).toBeVisible()
  await expect(page.getByText('人工确认登录')).toHaveCount(0)

  const recentCaseTasks = '**/harness/tasks?task_type=test-cases&limit=20'
  let filteredCaseHistory = false
  await page.route(recentCaseTasks, async route => {
    filteredCaseHistory = true
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  })
  await page.reload()
  await expect(page.getByRole('heading', { name: '用例编写' })).toBeVisible()
  await expect(page.getByTestId('harness-cases-result')).toContainText('"title": "有效账号登录"', { timeout: 15_000 })
  await expect(page.getByRole('button', { name: /有效账号登录/ })).toHaveCount(0)
  expect(filteredCaseHistory).toBe(true)
  await page.unroute(recentCaseTasks)

  await page.getByRole('button', { name: '需求分析' }).first().click()
  const analysisResult = page.getByTestId('harness-analysis-result')
  await expect(analysisResult).toContainText('"title": "登录"')
  const firstTaskId = JSON.parse((await analysisResult.textContent()) ?? '{}').task_id as string
  await page.locator('input[type=file]').setInputFiles({
    name: 'another-requirement.md', mimeType: 'text/markdown',
    buffer: Buffer.from('# 第二份需求\n检查任务归属'),
  })
  await page.getByRole('button', { name: '开始分析' }).click()
  await confirmAnalysisCheckpoint(page)
  await expect(analysisResult).toContainText('"title": "登录"', { timeout: 30_000 })
  await expect.poll(async () => JSON.parse((await analysisResult.textContent()) ?? '{}').task_id,
    { timeout: 30_000 }).not.toBe(firstTaskId)
  const secondTaskId = JSON.parse((await analysisResult.textContent()) ?? '{}').task_id as string
  await page.locator('.ai-analysis-select').nth(1).click()
  await expect(analysisResult).toContainText(firstTaskId)
  await expect(page.locator('.harness-stream-panel .harness-stream-output')).toContainText(firstTaskId)
  await page.locator('.ai-analysis-select').first().click()
  await expect(analysisResult).toContainText(secondTaskId)
  const secondAnalysisId = await page.locator('.ai-analysis-select').first().getAttribute('data-analysis-id')
  await page.getByRole('button', { name: '进入用例编写' }).click()
  await expect(page.locator('#analysis-select')).toHaveValue(secondAnalysisId!)
  await page.reload()
  await expect(page.locator('#analysis-select')).toHaveValue(secondAnalysisId!)

  const apiSpec = JSON.stringify({ openapi: '3.1.0', info: { title: 'Health API', version: '1.0' }, paths: { '/health': { get: { responses: { '200': { description: 'OK' } } } } } })
  await page.getByRole('button', { name: '需求分析' }).first().click()
  const specUpload = page.waitForResponse(response => response.url().endsWith('/documents') && response.request().method() === 'POST')
  await page.locator('input[type=file]').setInputFiles({
    name: 'openapi.json', mimeType: 'application/json', buffer: Buffer.from(apiSpec),
  })
  const uploadedSpec = await specUpload
  expect(uploadedSpec.status()).toBe(200)
  const specDocumentId = (await uploadedSpec.json()).id as string
  await expect(page.locator('.document-preview')).toContainText('"openapi":"3.1.0"')
  await expect(page.locator('.ai-report')).toHaveCSS('opacity', '1')
  await page.screenshot({ path: '/tmp/test-atelier-harness-api-spec-preview.png' })

  const capabilityRoute = '**/api/v1/projects/*/harness/config'
  let capabilityMasked = false
  await page.route(capabilityRoute, async route => {
    capabilityMasked = true
    const response = await route.fetch()
    const config = await response.json() as { skills: { skill_id: string }[] }
    await route.fulfill({ response, json: { ...config, skills: config.skills.filter(skill => skill.skill_id !== 'api-parsing') } })
  })
  await page.getByRole('button', { name: '项目技能' }).click()
  await expect(page.getByRole('heading', { name: '项目技能' })).toBeVisible()
  await expect(page.getByText('Harness 已配置')).toBeVisible()
  await expect(page.getByText('技能输入材料')).toBeVisible()
  const apiSkill = page.locator('.skill-card').filter({ hasText: '接口解析' })
  await expect(apiSkill).toBeVisible()
  expect(capabilityMasked).toBe(true)
  await expect(apiSkill.getByRole('button', { name: '执行' })).toHaveCount(0)
  await page.waitForTimeout(700)
  await page.screenshot({ path: '/tmp/test-atelier-harness-skill-capability-masked.png' })
  await page.unroute(capabilityRoute)
  await page.locator('.skills-toolbar').first().getByRole('button', { name: '刷新' }).click()
  await expect(apiSkill.getByRole('button', { name: '执行' })).toBeVisible()
  await page.waitForTimeout(700)
  await page.screenshot({ path: '/tmp/test-atelier-harness-skill-capability-restored.png' })
  const desktopViewport = page.viewportSize()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.waitForTimeout(700)
  await page.screenshot({ path: '/tmp/test-atelier-harness-skills-mobile.png' })
  if (desktopViewport) await page.setViewportSize(desktopViewport)
  await page.locator('.harness-skill-doc').filter({ hasText: 'openapi.json' }).getByRole('checkbox').check()
  await apiSkill.getByRole('button', { name: '执行' }).click()
  const skillRun = page.locator('.harness-skill-run').filter({ hasText: 'api-parsing' })
  await expect(skillRun).toContainText('已完成', { timeout: 30_000 })
  await skillRun.getByRole('button', { name: '查看结果' }).click()
  await expect(page.getByRole('dialog', { name: 'Harness 结果' })).toContainText(
    process.env.TEST_ATELIER_HARNESS_REAL_DSH_E2E === '1' ? 'local generic skill result' : 'fake generic skill result',
  )
  await expect(page.getByRole('dialog', { name: 'Harness 结果' })).toContainText(specDocumentId)
  const skillEnvelope = await downloadCompletedResult(page, 'skill-execution.v1')
  expect(skillEnvelope.project_id).toBe(analysisEnvelope.project_id)
  expect(skillEnvelope.source_document_ids).toEqual([specDocumentId])
  await expect(page.getByRole('dialog', { name: 'Harness 结果' })).toHaveCSS('opacity', '1')
  await page.screenshot({ path: '/tmp/test-atelier-harness-api-spec.png' })

  await page.getByRole('dialog', { name: 'Harness 结果' }).getByRole('button', { name: '关闭' }).click()
  for (const [label, skillId] of [['开发文档分析', 'dev-doc-analysis'], ['Figma 转 PRD', 'figma-to-prd']]) {
    const skillCard = page.locator('.skill-card').filter({ hasText: label })
    if (skillId === 'figma-to-prd') {
      await expect(skillCard).toContainText('不会打开 Figma 链接')
      await expect(skillCard).toContainText('原稿来源需人工核对')
    }
    await expect(skillCard.getByRole('button', { name: '执行' })).toBeVisible()
    await skillCard.getByRole('button', { name: '执行' }).click()
    const run = page.locator('.harness-skill-run').filter({ hasText: skillId }).first()
    await expect(run).toContainText('已完成', { timeout: 30_000 })
    await run.getByRole('button', { name: '查看结果' }).click()
    const result = page.getByRole('dialog', { name: 'Harness 结果' })
    await expect(result).toContainText(`"skill_id": "${skillId}"`)
    await expect(result).toContainText(specDocumentId)
    await result.getByRole('button', { name: '关闭' }).click()
  }
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '需求分析' }).first().click()
  await expect(page.getByTestId('harness-analysis-result')).toContainText(secondTaskId)
  await page.screenshot({ path: '/tmp/test-atelier-harness-mobile.png' })
  const pageWidth = await page.evaluate(() => document.documentElement.scrollWidth)
  expect(pageWidth).toBeLessThanOrEqual(391)

  await page.getByRole('button', { name: '证据关系' }).click()
  await expect(page.getByRole('heading', { name: '沿着证据往回看' })).toBeVisible()
  const typesToggle = page.getByRole('button', { name: /产物目录/ })
  await expect(typesToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(page.locator('.artifact-explorer')).toHaveCSS('opacity', '1')
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: '/tmp/test-atelier-evidence-relations-mobile-entry.png' })
  await typesToggle.click()
  await expect(typesToggle).toHaveAttribute('aria-expanded', 'true')
  await page.getByRole('button', { name: /分析结果/ }).click()
  await expect(typesToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(page.locator('.artifact-explorer-list-row').first()).toBeVisible()
  await page.locator('.artifact-explorer-list-row').first().click()
  await expect(page.locator('.artifact-explorer-edge').filter({ hasText: '用于分析' }).first()).toBeVisible()
  await page.locator('.artifact-explorer-edge').filter({ hasText: '用于分析' }).first().click()
  await expect(page).toHaveURL(/kind=document/)
  await page.getByRole('button', { name: '返回上一层' }).click()
  await expect(page).toHaveURL(/kind=analysis/)
  await expect(page.locator('.artifact-explorer')).toHaveCSS('opacity', '1')
  await expect(page.locator('.artifact-explorer-edge').first()).toHaveCSS('opacity', '1')
  await page.screenshot({ path: '/tmp/test-atelier-evidence-relations-mobile.png' })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await page.setViewportSize({ width: 1280, height: 900 })
  await expect(page.getByRole('button', { name: /分析结果/ }).first()).toBeVisible()
  await expect(page.locator('.artifact-explorer-types')).toHaveCSS('opacity', '1')
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: '/tmp/test-atelier-evidence-relations-desktop.png' })

  // A project snapshot must keep search and expansion inside one response.
  let snapshotRequests = 0
  await page.route('**/artifacts/snapshot', async route => {
    snapshotRequests += 1
    await route.continue()
  })
  await page.getByRole('button', { name: '建立项目快照' }).click()
  await expect(page.locator('.artifact-graph-toolbar')).toContainText('同一次数据库读取')
  expect(snapshotRequests).toBe(1)
  const snapshotGraph = page.locator('.artifact-project-snapshot')
  const relationRequests: string[] = []
  page.on('request', request => {
    if (request.url().includes('/artifacts/') && request.url().includes('/relations')) relationRequests.push(request.url())
  })
  await snapshotGraph.getByRole('textbox', { name: '查找快照产物' }).fill('document')
  await expect(snapshotGraph.locator('.artifact-graph-snapshot-results button').first()).toBeVisible()
  await snapshotGraph.locator('.artifact-graph-snapshot-results button').first().click()
  await expect(snapshotGraph.locator('.artifact-graph-inspector')).toContainText('document /')
  await snapshotGraph.getByRole('button', { name: '展开关联' }).click()
  await expect(snapshotGraph.locator('.react-flow__node').nth(1)).toBeVisible()
  expect(relationRequests).toEqual([])
  await snapshotGraph.getByRole('button', { name: '刷新图谱' }).click()
  await expect.poll(() => snapshotRequests).toBe(2)
  await expect(snapshotGraph.locator('.artifact-graph-inspector')).toContainText('document /')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(snapshotGraph.locator('.artifact-graph-stage')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await snapshotGraph.locator('.artifact-graph-stage').scrollIntoViewIfNeeded()
  await expect.poll(async () => snapshotGraph.locator('.artifact-graph-stage').evaluate(stage => {
    const bounds = stage.getBoundingClientRect()
    return [...stage.querySelectorAll('.react-flow__node')].every(node => {
      const box = node.getBoundingClientRect()
      return box.left >= bounds.left + 2 && box.right <= bounds.right - 2
    })
  })).toBe(true)
  await page.screenshot({ path: '/tmp/test-atelier-project-snapshot-mobile.png' })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.getByRole('button', { name: '返回逐项关系' }).click()
  await page.unroute('**/artifacts/snapshot')

  await page.route('**/artifacts/snapshot', route => route.fulfill({ status: 413,
    json: { detail: '项目关系超出单次快照上限' } }))
  await page.getByRole('button', { name: '建立项目快照' }).click()
  await expect(snapshotGraph.getByRole('alert')).toContainText('项目关系超出单次快照上限')
  await expect(snapshotGraph.locator('.react-flow__node')).toHaveCount(0)
  await page.unroute('**/artifacts/snapshot')
  await snapshotGraph.getByRole('button', { name: '重试建立快照' }).click()
  await expect(snapshotGraph.locator('.artifact-graph-stage')).toBeVisible()
  await page.getByRole('button', { name: '返回逐项关系' }).click()

  await page.route('**/artifacts/snapshot', route => route.fulfill({ status: 503,
    headers: { 'Retry-After': '5' }, json: { detail: '项目关系快照服务忙，请稍后重试' } }))
  await page.getByRole('button', { name: '建立项目快照' }).click()
  await expect(snapshotGraph.getByRole('alert')).toContainText('项目关系快照服务忙')
  await expect(snapshotGraph.locator('.react-flow__node')).toHaveCount(0)
  await page.unroute('**/artifacts/snapshot')
  await snapshotGraph.getByRole('button', { name: '重试建立快照' }).click()
  await expect(snapshotGraph.locator('.artifact-graph-stage')).toBeVisible()
  await page.getByRole('button', { name: '返回逐项关系' }).click()

  await page.getByRole('button', { name: '关系图', exact: true }).click()
  await expect(page.locator('.artifact-graph-stage .react-flow__node').first()).toBeVisible()
  await expect(page.locator('.artifact-graph-stage .react-flow__node').nth(1)).toBeVisible()
  await page.locator('.artifact-graph-stage .react-flow__node').nth(1).click()
  const graphSource = await page.locator('.artifact-graph-inspector span').textContent()
  expect(graphSource).toContain('document / ')
  const graphDocumentId = graphSource!.split('document / ')[1].split(' · ')[0]
  let graphRevision = ''
  let graphConflict = false
  await page.route(`**/artifacts/document/${graphDocumentId}/relations*`, async route => {
    if (route.request().url().includes('offset=100')) {
      graphConflict = true
      expect(new URL(route.request().url()).searchParams.get('revision')).toBe(graphRevision)
      await route.fulfill({ status: 409, json: { detail: '关联在翻页期间发生变化，请从头刷新' } })
      return
    }
    const response = await route.fetch()
    const body = await response.json()
    graphRevision = body.revision
    await route.fulfill({ response, json: { ...body, next_offset: 100, truncated: true } })
  })
  await expect(page.getByRole('button', { name: '展开关联' })).toBeEnabled()
  await page.getByRole('button', { name: '展开关联' }).click()
  await expect(page.locator('.artifact-graph-toolbar')).toContainText('个产物')
  await page.getByRole('button', { name: '展开关联' }).click()
  await expect(page.locator('.artifact-graph-warning')).toContainText('关联在翻页期间发生变化')
  expect(graphConflict).toBe(true)
  await page.unroute(`**/artifacts/document/${graphDocumentId}/relations*`)
  await page.getByRole('button', { name: '重新核对' }).click()
  await expect(page.locator('.artifact-graph-warning')).toHaveCount(0)
  await page.screenshot({ path: '/tmp/test-atelier-evidence-graph-desktop.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('.artifact-graph-stage')).toBeVisible()
  await page.locator('.artifact-graph-stage').scrollIntoViewIfNeeded()
  await expect.poll(async () => page.locator('.artifact-graph-stage').evaluate(stage => {
    const bounds = stage.getBoundingClientRect()
    return [...stage.querySelectorAll('.react-flow__node')].every(node => {
      const box = node.getBoundingClientRect()
      return box.left >= bounds.left + 2 && box.right <= bounds.right - 2
    })
  })).toBe(true)
  await page.screenshot({ path: '/tmp/test-atelier-evidence-graph-mobile.png' })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.getByRole('button', { name: '关联列表' }).click()

  // The API test exercises real 100+ references; this checks that the page
  // requests and appends a subsequent page without losing its existing nodes.
  const selectedAnalysisId = new URL(page.url()).searchParams.get('id')!
  let firstEdges = 0
  let laterRequested = false
  let staleOnce = true
  let firstPage: Record<string, unknown> = {}
  await page.route(`**/artifacts/analysis/${selectedAnalysisId}/relations*`, async route => {
    if (route.request().url().includes('offset=100')) {
      laterRequested = true
      expect(new URL(route.request().url()).searchParams.get('revision')).toBe(firstPage.revision)
      if (staleOnce) {
        staleOnce = false
        await route.fulfill({ status: 409, json: { detail: '关联在翻页期间发生变化，请从头刷新' } })
        return
      }
      const edges = firstPage.edges as { from: { type: string; id: string }; to: { type: string; id: string }; relation: string }[]
      await route.fulfill({ status: 200, json: { ...firstPage,
        edges: [edges[0] ?? { from: { type: 'analysis', id: selectedAnalysisId }, to: { type: 'analysis', id: selectedAnalysisId }, relation: 'test' }],
        next_offset: null, truncated: false } })
      return
    }
    const response = await route.fetch()
    const body = await response.json()
    firstEdges = body.edges.length
    firstPage = body
    await route.fulfill({ response, json: { ...body, next_offset: 100, truncated: true } })
  })
  await page.reload()
  await expect(page.getByRole('button', { name: '加载后续关联' })).toBeVisible()
  await page.getByRole('button', { name: '加载后续关联' }).click()
  await expect(page.getByRole('alert')).toContainText('关联在翻页期间发生变化')
  await page.screenshot({ path: '/tmp/test-atelier-evidence-checkpoint-conflict.png' })
  await page.getByRole('button', { name: '从头刷新关联' }).click()
  await expect(page.locator('.artifact-explorer-edge')).toHaveCount(firstEdges)
  await page.getByRole('button', { name: '加载后续关联' }).click()
  await expect(page.locator('.artifact-explorer-edge')).toHaveCount(firstEdges + 1)
  expect(laterRequested).toBe(true)
  await expect(page.getByRole('button', { name: '加载后续关联' })).toHaveCount(0)
  await page.unroute(`**/artifacts/analysis/${selectedAnalysisId}/relations*`)

  let liveFirstPage: Record<string, unknown> = {}
  let liveConflict = true
  let livePages = 0
  await page.route(`**/artifacts/analysis/${selectedAnalysisId}/relations*`, async route => {
    const url = new URL(route.request().url())
    if (url.searchParams.get('consistency') === 'verified') {
      return route.fulfill({ status: 413, json: { detail: '关系超过一致性游标上限' } })
    }
    expect(url.searchParams.get('consistency')).toBe('live')
    if (url.searchParams.get('offset') === '100') {
      livePages += 1
      expect(url.searchParams.get('previous_page')).toBe(liveFirstPage.page_checkpoint)
      if (liveConflict) {
        liveConflict = false
        return route.fulfill({ status: 409, json: { detail: '关联在翻页期间发生变化，请从头刷新' } })
      }
      return route.fulfill({ status: 200, json: { ...liveFirstPage,
        edges: [(liveFirstPage.edges as unknown[])[0]], next_offset: null, truncated: false,
        page_checkpoint: 'b'.repeat(64) } })
    }
    const response = await route.fetch()
    liveFirstPage = await response.json()
    return route.fulfill({ response, json: { ...liveFirstPage, next_offset: 100, truncated: true } })
  })
  await page.reload()
  await expect(page.getByRole('button', { name: '使用实时关系列表' })).toBeVisible()
  await page.getByRole('button', { name: '使用实时关系列表' }).click()
  await expect(page.locator('.artifact-explorer-detail .artifact-explorer-limit')).toContainText('仅校验相邻页')
  await expect(page.getByRole('button', { name: '关系图', exact: true })).toBeDisabled()
  const liveEdges = await page.locator('.artifact-explorer-edge').count()
  await page.getByRole('button', { name: '加载后续关联' }).click()
  await expect(page.getByRole('alert')).toContainText('关联在翻页期间发生变化')
  await expect(page.locator('.artifact-explorer-edge')).toHaveCount(liveEdges)
  await page.getByRole('button', { name: '从头刷新关联' }).click()
  await page.getByRole('button', { name: '加载后续关联' }).click()
  await expect(page.locator('.artifact-explorer-edge')).toHaveCount(liveEdges + 1)
  expect(livePages).toBe(2)
  await page.screenshot({ path: '/tmp/test-atelier-live-relations-desktop.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('.artifact-explorer-detail .artifact-explorer-limit')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await page.locator('.artifact-explorer-detail .artifact-explorer-limit').scrollIntoViewIfNeeded()
  await page.screenshot({ path: '/tmp/test-atelier-live-relations-mobile.png' })
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.unroute(`**/artifacts/analysis/${selectedAnalysisId}/relations*`)

  await page.getByRole('button', { name: /治理目录/ }).click()
  await expect(page).toHaveURL(/kind=governance_directory/)
  await expect(page.locator('.artifact-explorer-list-head')).toContainText('治理目录')
  await page.getByRole('button', { name: /分析结果/ }).first().click()
  await expect(page.locator('.artifact-explorer-list-row').first()).toBeVisible()

  let rejectNextPage = true
  let rejectWholeCatalog = false
  const directory = { type: 'governance_directory', id: 'dir_YQ', title: '目录A', version: 1 }
  await page.route('**/api/v1/projects/*/artifacts?*', async route => {
    const url = new URL(route.request().url())
    if (url.searchParams.get('kind') !== 'governance_directory') return route.continue()
    if (url.searchParams.get('consistency') === 'live') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [directory], next_before_id: null }) })
    }
    if (rejectWholeCatalog) return route.fulfill({ status: 413, contentType: 'application/json', body: JSON.stringify({ detail: '目录超过一致性游标上限' }) })
    if (url.searchParams.has('before_id') && rejectNextPage) {
      rejectNextPage = false
      return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: '目录在翻页期间发生变化' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      items: url.searchParams.has('before_id') ? [] : [directory],
      next_before_id: url.searchParams.has('before_id') ? null : directory.id, revision: 'a'.repeat(64),
    }) })
  })
  await page.getByRole('button', { name: /治理目录/ }).click()
  await expect(page.getByRole('button', { name: '加载更多' })).toBeVisible()
  await page.getByRole('button', { name: '加载更多' }).click()
  await expect(page.locator('.artifact-explorer-error')).toContainText('目录在翻页期间发生变化')
  await expect(page.locator('.artifact-explorer-list-row')).toHaveCount(0)
  await page.getByRole('button', { name: '从头刷新目录' }).click()
  await expect(page.locator('.artifact-explorer-list-row')).toHaveCount(1)
  rejectWholeCatalog = true
  await page.getByRole('button', { name: /分析结果/ }).first().click()
  await page.getByRole('button', { name: /治理目录/ }).click()
  await expect(page.getByRole('button', { name: '使用实时目录' })).toBeVisible()
  await page.getByRole('button', { name: '使用实时目录' }).click()
  await expect(page.locator('.artifact-explorer-limit')).toContainText('实时目录')
  await expect(page.locator('.artifact-explorer-list-row')).toHaveCount(1)
  await page.unroute('**/api/v1/projects/*/artifacts?*')

  await page.getByRole('button', { name: '项目知识' }).first().click()
  await page.getByLabel('来源类型').click()
  await page.getByRole('option', { name: 'Harness 任务' }).click()
  await page.getByLabel('来源 ID').fill(skillEnvelope.task_id)
  await page.getByLabel('知识标题').fill('Harness 技能结论')
  await page.getByLabel('人工结论').fill('保留结果版本与 Session 归属')
  await page.getByRole('button', { name: '建立知识条目' }).click()
  await expect(page.getByText('持久化结构化结果的规范化 SHA-256', { exact: false })).toBeVisible()
  await expect(page.locator('.knowledge-source code').last()).toHaveText(/[a-f0-9]{64}/)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.knowledge-source code').last().scrollIntoViewIfNeeded()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await page.screenshot({ path: '/tmp/test-atelier-harness-knowledge-mobile.png' })
  await page.getByRole('button', { name: '新建知识' }).click()
  await page.getByLabel('来源类型').click()
  await page.getByRole('option', { name: '需求分析' }).click()
  await page.getByLabel('来源 ID').fill(analysisEnvelope.analysis_run_id!)
  await page.getByLabel('知识标题').fill('需求分析追溯')
  await page.getByLabel('人工结论').fill('保留确认版本和原始任务关联')
  await page.getByRole('button', { name: '建立知识条目' }).click()
  await expect(page.getByText('业务分析持久化结果的规范化 SHA-256', { exact: false })).toBeVisible()
  await expect(page.locator('.knowledge-source')).toContainText(analysisEnvelope.task_id)
  await expect(page.locator('.knowledge-source code').last()).toHaveText(/[a-f0-9]{64}/)
  await page.locator('.knowledge-source code').last().scrollIntoViewIfNeeded()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await page.screenshot({ path: '/tmp/test-atelier-analysis-knowledge-mobile.png' })
  await page.getByRole('button', { name: '新建知识' }).click()
  await page.getByLabel('来源类型').click()
  await page.getByRole('option', { name: 'Harness 任务' }).click()
  await page.getByLabel('来源 ID').fill(casesEnvelope.task_id)
  await page.getByLabel('知识标题').fill('用例来源追溯')
  await page.getByLabel('人工结论').fill('保留原始分析与用例结果版本')
  await page.getByRole('button', { name: '建立知识条目' }).click()
  await expect(page.locator('.knowledge-source')).toContainText(`关联分析 ${casesEnvelope.analysis_run_id}`)
  await expect(page.locator('.knowledge-source code').last()).toHaveText(/[a-f0-9]{64}/)
  await page.locator('.knowledge-source code').last().scrollIntoViewIfNeeded()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await page.screenshot({ path: '/tmp/test-atelier-cases-knowledge-mobile.png' })

  expect(errors).toEqual([])
})

test('analysis does not display a staged result before terminal success', async ({ page }) => {
  test.skip(process.env.TEST_ATELIER_HARNESS_E2E !== '1', 'Requires the disposable Harness browser server')
  const password = process.env.AI_STUDIO_E2E_PASSWORD
  test.skip(!password, 'Set AI_STUDIO_E2E_PASSWORD for the local server')
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))

  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('button', { name: '新建项目' }).first().click()
  await page.getByLabel('项目名称').fill(`暂存结果验收-${Date.now()}`)
  await page.getByRole('button', { name: '创建项目' }).click()
  await page.getByRole('button', { name: '需求分析' }).first().click()
  await page.locator('input[type=file]').setInputFiles({
    name: 'result-boundary.md', mimeType: 'text/markdown', buffer: Buffer.from('# 登录\n验证结果边界'),
  })
  await expect(page.getByText('提取内容预览')).toBeVisible()

  let streamCount = 0
  let releaseFailure!: () => void
  const failureGate = new Promise<void>(resolve => { releaseFailure = resolve })
  await page.route('**/api/v1/projects/*/harness/tasks/*/events?after=*', async route => {
    streamCount += 1
    if (streamCount === 1) {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: [
        'id: 2\nevent: task.progress\ndata: {"kind":"session_bound","session_id":"bound-for-render-test"}\n\n',
        'id: 3\nevent: task.progress\ndata: {"text":"unverified-analysis-progress-must-not-render"}\n\n',
        'id: 4\nevent: task.result\ndata: {"session_id":"bound-for-render-test","result":{"title":"staged-result-must-not-render"}}\n\n',
      ].join('') })
      return
    }
    await failureGate
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body:
      'id: 5\nevent: task.failed\ndata: {"error":{"code":"runner_failed","message":"任务失败"}}\n\n' })
  })
  try {
    await page.getByRole('button', { name: '开始分析' }).click()
    const panel = page.locator('.harness-stream-panel')
    await expect(panel.getByRole('button', { name: '查看 Session' })).toBeVisible()
    await expect(panel).not.toContainText('staged-result-must-not-render')
    await expect(panel).not.toContainText('unverified-analysis-progress-must-not-render')
    await expect(panel).toHaveCSS('opacity', '1')
    await page.screenshot({ path: '/tmp/test-atelier-harness-staged-result.png' })
    releaseFailure()
    await expect(panel).toContainText('任务未完成，没有可用结果')
    await page.screenshot({ path: '/tmp/test-atelier-harness-failed-result.png' })
    expect(errors).toEqual([])
  } finally {
    releaseFailure()
    await page.unroute('**/api/v1/projects/*/harness/tasks/*/events?after=*')
  }
})

test('case authoring does not display a staged result before terminal success', async ({ page }) => {
  test.skip(process.env.TEST_ATELIER_HARNESS_E2E !== '1', 'Requires the disposable Harness browser server')
  const password = process.env.AI_STUDIO_E2E_PASSWORD
  test.skip(!password, 'Set AI_STUDIO_E2E_PASSWORD for the local server')

  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('button', { name: '新建项目' }).first().click()
  await page.getByLabel('项目名称').fill(`用例暂存结果验收-${Date.now()}`)
  await page.getByRole('button', { name: '创建项目' }).click()
  await page.getByRole('button', { name: '需求分析' }).first().click()
  await page.locator('input[type=file]').setInputFiles({
    name: 'case-result-boundary.md', mimeType: 'text/markdown', buffer: Buffer.from('# 登录\n有效账号进入首页'),
  })
  await expect(page.getByText('提取内容预览')).toBeVisible()
  await page.getByRole('button', { name: '开始分析' }).click()
  await confirmAnalysisCheckpoint(page)
  await expect(page.getByTestId('harness-analysis-result')).toContainText('登录', { timeout: 30_000 })
  await page.getByRole('button', { name: '进入用例编写' }).click()
  await expect(page.getByRole('heading', { name: '用例编写' })).toBeVisible()

  let streamCount = 0
  let releaseFailure!: () => void
  const failureGate = new Promise<void>(resolve => { releaseFailure = resolve })
  await page.route('**/api/v1/projects/*/harness/tasks/*/events?after=*', async route => {
    streamCount += 1
    if (streamCount === 1) {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: [
        'id: 2\nevent: task.progress\ndata: {"kind":"session_bound","session_id":"bound-for-cases-render"}\n\n',
        'id: 3\nevent: task.progress\ndata: {"text":"unverified-cases-progress-must-not-render"}\n\n',
        'id: 4\nevent: task.result\ndata: {"session_id":"bound-for-cases-render","result":{"title":"staged-cases-must-not-render"}}\n\n',
      ].join('') })
      return
    }
    await failureGate
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body:
      'id: 5\nevent: task.failed\ndata: {"error":{"code":"analysis_changed","message":"分析已更新，请基于最新分析重试用例任务","retryable":true}}\n\n' })
  })
  try {
    await page.getByRole('button', { name: '运行 Harness' }).click()
    const panel = page.locator('.harness-stream-panel')
    await expect(panel.getByRole('button', { name: '查看 Session' })).toBeVisible()
    await expect(panel).not.toContainText('staged-cases-must-not-render')
    await expect(panel).not.toContainText('unverified-cases-progress-must-not-render')
    releaseFailure()
    await expect(panel).toContainText('任务未完成，没有可用结果')
    await expect(page.locator('.ai-alert.error')).toContainText('分析已更新，请基于最新分析重试用例任务')
    await expect(panel.getByRole('button', { name: '重试' })).toBeVisible()
    await expect(panel).not.toContainText('staged-cases-must-not-render')
  } finally {
    releaseFailure()
    await page.unroute('**/api/v1/projects/*/harness/tasks/*/events?after=*')
  }
})

test('conversion history keeps its uploaded input and exposes explicit evidence', async ({ page }) => {
  test.skip(process.env.TEST_ATELIER_HARNESS_E2E !== '1', 'Requires the disposable Harness browser server')
  const password = process.env.AI_STUDIO_E2E_PASSWORD
  test.skip(!password, 'Set AI_STUDIO_E2E_PASSWORD for the local server')

  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  const loginResponse = page.waitForResponse(response => response.url().endsWith('/auth/login') && response.request().method() === 'POST')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  const csrf = (await (await loginResponse).json()).csrf as string
  await page.getByRole('button', { name: '新建项目' }).first().click()
  await page.getByLabel('项目名称').fill(`转换来源验收-${Date.now()}`)
  const created = page.waitForResponse(response => response.url().endsWith('/projects') && response.request().method() === 'POST')
  await page.getByRole('button', { name: '创建项目' }).click()
  const projectId = (await (await created).json()).id as number
  const source = Buffer.from('用例名称,前置条件,用例步骤,预期结果\n登录,已有账号,输入账号,进入首页\n')
  const converted = await page.request.post(`/api/v1/projects/${projectId}/conversion/export`, {
    headers: { 'x-csrf-token': csrf },
    multipart: { file: { name: 'uploaded.csv', mimeType: 'text/csv', buffer: source },
      conversion_mode: 'excel_to_excel', output_mode: 'after', prefix_path: '迭代',
      case_type: '业务功能用例', case_status: '正常' },
  })
  expect(converted.status()).toBe(200)
  const outputDigest = createHash('sha256').update(await converted.body()).digest('hex')
  const history = await page.request.get(`/api/v1/projects/${projectId}/conversion/history`)
  expect(history.status()).toBe(200)
  const recordId = ((await history.json()) as { records: { id: string }[] }).records[0].id
  await page.getByRole('button', { name: '用例转换' }).first().click()
  await page.locator('.history-trigger').click()
  await expect(page.getByText('原始文件已留存')).toBeVisible()
  await page.screenshot({ path: '/tmp/test-atelier-conversion-source-desktop.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '下载原始上传文件：uploaded.csv' }).scrollIntoViewIfNeeded()
  await page.screenshot({ path: '/tmp/test-atelier-conversion-source-mobile.png' })
  await page.setViewportSize({ width: 1280, height: 720 })
  const originalRequest = page.waitForResponse(response => /\/conversion\/history\/[^/]+\/source$/.test(response.url()))
  await page.getByRole('button', { name: '下载原始上传文件：uploaded.csv' }).click()
  const originalResponse = await originalRequest
  expect(originalResponse.status()).toBe(200)
  await expect(page.getByText('原始上传文件已下载')).toBeVisible()
  await page.getByRole('button', { name: '证据关系' }).click()
  await page.getByRole('button', { name: /转换结果/ }).click()
  await page.locator('.artifact-explorer-list-row').first().click()
  await page.locator('.artifact-explorer-edge').filter({ hasText: '转换为' }).click()
  await expect(page.getByRole('link', { name: '下载原始上传文件' })).toBeVisible()
  await expect(page.locator('.artifact-explorer-metadata')).toContainText('SHA-256')
  await page.getByRole('button', { name: '项目知识' }).first().click()
  await page.getByLabel('来源类型').click()
  await page.getByRole('option', { name: '转换产物' }).click()
  await page.getByLabel('来源 ID').fill(recordId)
  await page.getByLabel('知识标题').fill('转换结果追溯')
  await page.getByLabel('人工结论').fill('保留转换结果身份')
  await page.getByRole('button', { name: '建立知识条目' }).click()
  await expect(page.getByText('创建条目时记录的转换结果 SHA-256', { exact: false })).toBeVisible()
  await expect(page.locator('.knowledge-source code').last()).toHaveText(outputDigest)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.knowledge-source code').last().scrollIntoViewIfNeeded()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await page.screenshot({ path: '/tmp/test-atelier-conversion-knowledge-mobile.png' })
})

test('project manager exposes deletion recovery and keeps the retry action visible', async ({ page }) => {
  test.skip(process.env.TEST_ATELIER_HARNESS_E2E !== '1', 'Requires the disposable Harness browser server')
  const password = process.env.AI_STUDIO_E2E_PASSWORD
  test.skip(!password, 'Set AI_STUDIO_E2E_PASSWORD for the local server')

  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('button', { name: '新建项目' }).first().click()
  const name = `删除恢复验收-${Date.now()}`
  await page.getByLabel('项目名称').fill(name)
  await page.getByRole('button', { name: '创建项目' }).click()
  await expect(page).toHaveURL(/\/projects\/\d+\/home/)
  const projectId = Number(page.url().match(/\/projects\/(\d+)/)?.[1])
  expect(projectId).toBeGreaterThan(0)

  let refused = false
  await page.route(`**/api/v1/projects/${projectId}`, async route => {
    if (route.request().method() === 'DELETE' && !refused) {
      refused = true
      return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Harness Host 不可用 请稍后重试"}' })
    }
    return route.continue()
  })
  await page.route('**/api/v1/projects', async route => {
    if (!refused || route.request().method() !== 'GET') return route.continue()
    const response = await route.fetch()
    const projects = await response.json()
    return route.fulfill({ response, json: projects.map((project: { id: number }) => project.id === projectId
      ? { ...project, deletion_pending_at: '2026-10-25T00:00:00Z' } : project) })
  })
  await page.getByRole('button', { name: '管理项目' }).click()
  const manager = page.getByRole('dialog', { name: '项目管理' })
  await manager.locator('.project-manager-row').filter({ hasText: name }).getByRole('button', { name: '删除项目' }).click()
  const confirmation = page.getByRole('dialog', { name: '删除项目' })
  await confirmation.getByRole('button', { name: '确认删除' }).click()
  const recovery = page.getByRole('dialog', { name: '继续清理项目' })
  await expect(recovery.getByRole('alert')).toContainText('Harness Host 不可用')
  await page.screenshot({ path: '/tmp/test-atelier-project-delete-retry-desktop.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(recovery.getByRole('button', { name: '重试清理' })).toBeVisible()
  await page.screenshot({ path: '/tmp/test-atelier-project-delete-retry-mobile.png' })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await recovery.getByRole('button', { name: '取消' }).click()
  const row = manager.locator('.project-manager-row').filter({ hasText: name })
  await expect(row).toContainText('删除待完成')
  await expect(row.getByRole('button', { name: /正在等待删除重试/ })).toBeDisabled()
  await page.unroute('**/api/v1/projects')
  await page.unroute(`**/api/v1/projects/${projectId}`)
  await row.getByRole('button', { name: '重试清理项目' }).click()
  await page.getByRole('dialog', { name: '继续清理项目' }).getByRole('button', { name: '重试清理' }).click()
  await expect(row).toHaveCount(0)
})

test('knowledge page streams a project-scoped evidence archive on desktop and mobile', async ({ page }) => {
  test.skip(process.env.TEST_ATELIER_HARNESS_E2E !== '1', 'Requires the disposable Harness browser server')
  const password = process.env.AI_STUDIO_E2E_PASSWORD
  test.skip(!password, 'Set AI_STUDIO_E2E_PASSWORD for the local server')

  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('button', { name: '新建项目' }).first().click()
  await page.getByLabel('项目名称').fill(`证据导出-${Date.now()}`)
  await page.getByRole('button', { name: '创建项目' }).click()
  await page.getByRole('button', { name: '需求分析' }).first().click()
  const source = Buffer.from('# 知识来源\n冻结原件摘要')
  const uploaded = page.waitForResponse(response => response.url().includes('/documents')
    && response.request().method() === 'POST')
  await page.locator('input[type=file]').setInputFiles({
    name: 'knowledge.md', mimeType: 'text/markdown', buffer: source,
  })
  const sourceId = (await (await uploaded).json()).id as string
  await page.getByRole('button', { name: '项目知识' }).click()
  await expect(page.getByRole('heading', { name: '把结论连回证据' })).toBeVisible()
  await page.getByLabel('来源 ID').fill(sourceId)
  await page.getByLabel('知识标题').fill('来源完整性')
  await page.getByLabel('人工结论').fill('以原件摘要关联知识')
  await page.getByRole('button', { name: '建立知识条目' }).click()
  await expect(page.getByText('创建条目时记录的原件 SHA-256', { exact: false })).toBeVisible()
  await expect(page.locator('.knowledge-source code').last()).toHaveText(createHash('sha256').update(source).digest('hex'))
  await page.locator('.knowledge-source code').last().scrollIntoViewIfNeeded()
  await page.locator('.knowledge-source').screenshot({ path: '/tmp/test-atelier-knowledge-source-desktop.png' })
  const link = page.getByRole('link', { name: '下载项目证据' })
  await expect(link).toBeVisible()
  const downloadPromise = page.waitForEvent('download')
  await link.click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/^project-\d+-evidence\.zip$/)
  const downloadedPath = await download.path()
  expect(downloadedPath).toBeTruthy()
  const file = await open(downloadedPath!, 'r')
  try {
    const header = Buffer.alloc(4)
    await file.read(header, 0, 4, 0)
    expect(header.toString()).toBe('PK\u0003\u0004')
  } finally { await file.close() }
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(link).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await page.screenshot({ path: '/tmp/test-atelier-project-evidence-mobile.png' })
  await page.locator('.knowledge-source code').last().scrollIntoViewIfNeeded()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391)
  await page.screenshot({ path: '/tmp/test-atelier-knowledge-source-mobile.png' })
})
