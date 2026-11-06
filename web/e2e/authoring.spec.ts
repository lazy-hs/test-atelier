import { expect, test } from '@playwright/test'

test('历史模型草稿仍可在独立入口审核、导出与发布', async ({ page }) => {
  if (!process.env.AI_STUDIO_E2E_MODEL_STUB) test.skip(true, 'Set AI_STUDIO_E2E_MODEL_STUB for the cloud API test stub')
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('dialog', dialog => void dialog.accept())
  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(process.env.AI_STUDIO_E2E_PASSWORD!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工作台' })).toBeVisible()
  await page.getByRole('button', { name: '新建项目' }).first().click()
  await page.getByLabel('项目名称').fill(`用例编写-${Date.now()}`)
  await page.getByRole('button', { name: '创建项目' }).click()
  await page.getByRole('button', { name: '需求分析' }).first().click()
  const projectId = page.url().match(/\/projects\/(\d+)/)?.[1]
  expect(projectId).toBeTruthy()
  const csrf = (await (await page.request.get('/api/v1/auth/csrf')).json()).token as string
  const upload = page.waitForResponse(response => response.url().endsWith('/documents') && response.request().method() === 'POST')
  await page.locator('input[type=file]').setInputFiles({ name: 'login.md', mimeType: 'text/markdown', buffer: Buffer.from('# 登录\n有效账号进入首页') })
  await expect(page.getByText('提取内容预览')).toBeVisible()
  const documentId = (await (await upload).json()).id as string
  await expect(page.getByRole('button', { name: '开始分析' })).toBeDisabled()
  const created = await page.request.post(`/api/v1/projects/${projectId}/analyses`, {
    data: { source_ids: [documentId], provider: 'remote' }, headers: { 'x-csrf-token': csrf },
  })
  expect(created.ok()).toBeTruthy()
  await page.getByRole('button', { name: '历史分析' }).click()
  await expect(page.getByRole('heading', { name: '历史分析' })).toBeVisible()
  await expect(page.getByText('REQ-1 登录')).toBeVisible({ timeout: 15000 })
  await page.getByPlaceholder('填写确认结论').fill('连续失败五次锁定账号')
  await page.getByRole('button', { name: '确认', exact: true }).click()
  await expect(page.getByText('澄清结论已记录')).toBeVisible()
  const analyses = await (await page.request.get(`/api/v1/projects/${projectId}/analyses`)).json() as { id: string; status: string }[]
  const analysisId = analyses.find(item => item.status === 'ready')?.id
  expect(analysisId).toBeTruthy()
  const legacyJob = await page.request.post(`/api/v1/projects/${projectId}/analyses/${analysisId}/generate`, {
    data: { provider: 'remote' }, headers: { 'x-csrf-token': csrf },
  })
  expect(legacyJob.ok()).toBeTruthy()
  await page.getByRole('button', { name: '用例编写' }).first().click()
  await expect(page.getByLabel('生成模型')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '生成草稿' })).toHaveCount(0)
  await page.getByRole('button', { name: '草稿审核' }).first().click()
  await expect(page.getByRole('heading', { name: '草稿审核', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /有效账号登录/ })).toBeVisible({ timeout: 15000 })
  await page.getByRole('button', { name: /有效账号登录/ }).click()
  await page.getByRole('textbox', { name: '标题' }).fill('人工确认登录')
  await page.getByRole('button', { name: '保存草稿' }).click()
  await expect(page.getByText('草稿已保存')).toBeVisible()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Excel' }).click()
  expect((await download).suggestedFilename()).toMatch(/xlsx$/)
  await page.getByRole('button', { name: '发布到用例库' }).click()
  await expect(page.getByText('已发布到用例库')).toBeVisible()
  await page.getByRole('button', { name: '用例库' }).first().click()
  await expect(page.getByRole('heading', { name: '用例审核' })).toBeVisible()
  await expect(page.getByText('人工确认登录').first()).toBeVisible()
  await page.screenshot({ path: '/tmp/ai-studio-workstation-authored.png' })
  expect(errors).toEqual([])
})
