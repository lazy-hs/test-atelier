import { expect, test } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { basename, dirname, relative, sep } from 'node:path'
import { DatabaseSync } from 'node:sqlite'

test('legacy analysis and drafts remain reviewable without appearing as Harness output', async ({ page }) => {
  const databaseFile = process.env.TEST_ATELIER_HARNESS_E2E_DB
  test.skip(process.env.TEST_ATELIER_HARNESS_E2E !== '1' || !databaseFile,
    'Requires the disposable Harness browser database')
  const temporaryParent = relative(tmpdir(), dirname(databaseFile!))
  if (basename(databaseFile!) !== 'test.db' || !temporaryParent.startsWith('test-atelier-harness-browser-')
    || temporaryParent.includes(sep)) throw new Error('Refusing to seed a non-disposable database')

  const password = process.env.AI_STUDIO_E2E_PASSWORD
  test.skip(!password, 'Set AI_STUDIO_E2E_PASSWORD for the disposable server')
  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工作台' })).toBeVisible()
  await page.getByRole('button', { name: '新建项目' }).first().click()
  await page.getByLabel('项目名称').fill(`历史分析迁移验收-${Date.now()}`)
  await page.getByRole('button', { name: '创建项目' }).click()
  await expect(page).toHaveURL(/\/projects\/\d+\/home/)
  const projectId = Number(page.url().match(/\/projects\/(\d+)/)?.[1])
  expect(projectId).toBeGreaterThan(0)

  await page.getByRole('button', { name: '需求分析' }).first().click()
  const upload = page.waitForResponse(response => response.url().endsWith('/documents') && response.request().method() === 'POST')
  await page.locator('input[type=file]').setInputFiles({
    name: 'historical-login.md', mimeType: 'text/markdown', buffer: Buffer.from('# 登录\n正确账号进入首页'),
  })
  const sourceId = (await (await upload).json()).id as string
  const analysisId = randomUUID().replaceAll('-', '')
  const draftId = randomUUID().replaceAll('-', '')
  const result = {
    requirements: [{ id: 'REQ-1', title: '历史登录', summary: '正确账号进入首页',
      source_ids: [sourceId], acceptance: ['正确账号进入首页'], methods: ['EP'] }],
    ambiguities: [], risks: [], report_markdown: '# 历史分析报告\n\n正确账号进入首页',
  }
  const draft = { module: ['登录'], title: '历史登录用例', priority: 'P1', precondition: '存在账号',
    steps: [{ action: '输入正确账号并登录', expected: '进入首页' }], requirement_ids: ['REQ-1'],
    method: 'EP', label: 'Web', remark: '', smoke: '' }
  const database = new DatabaseSync(databaseFile!)
  try {
    const user = database.prepare('SELECT id FROM users WHERE username = ?').get('admin') as { id: number }
    expect(user.id).toBeGreaterThan(0)
    const now = new Date().toISOString()
    database.prepare(`INSERT INTO analysis_runs
      (id, project_id, user_id, source_ids, status, provider, result, error, attempts, version, created_at, updated_at)
      VALUES (?, ?, ?, ?, 'ready', 'remote', ?, '', 0, 1, ?, ?)`).run(
      analysisId, projectId, user.id, JSON.stringify([sourceId]), JSON.stringify(result), now, now)
    database.prepare(`INSERT INTO case_drafts
      (id, project_id, analysis_id, payload, status, version, case_id, created_at)
      VALUES (?, ?, ?, ?, 'draft', 1, NULL, ?)`).run(draftId, projectId, analysisId, JSON.stringify(draft), now)
  } finally {
    database.close()
  }

  await page.reload()
  await expect(page.getByRole('heading', { name: '需求分析' })).toBeVisible()
  await expect(page.locator('.ai-analysis-select')).toHaveCount(0)
  await expect(page.getByLabel('分析模型')).toHaveCount(0)
  await page.getByRole('button', { name: '历史分析' }).click()
  await expect(page.getByRole('heading', { name: '历史分析' })).toBeVisible()
  await expect(page.getByText('正确账号进入首页').first()).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Harness 原始结果' })).toHaveCount(0)
  await page.getByRole('button', { name: '进入草稿审核' }).click()
  await expect(page.getByRole('heading', { name: '草稿审核', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /历史登录用例/ })).toBeVisible()
  await page.getByRole('textbox', { name: '标题' }).fill('人工复核历史登录')
  await page.getByRole('button', { name: '保存草稿' }).click()
  await expect(page.getByText('草稿已保存')).toBeVisible()
  page.once('dialog', dialog => void dialog.accept())
  await page.getByRole('button', { name: '发布到用例库' }).click()
  await expect(page.getByText('已发布到用例库')).toBeVisible()
  await page.getByRole('button', { name: '用例编写' }).first().click()
  await expect(page.getByRole('button', { name: '运行 Harness' })).toBeDisabled()
  await expect(page.getByText('人工复核历史登录')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'DeepSeek Harness 输出' })).toHaveCount(0)
})
