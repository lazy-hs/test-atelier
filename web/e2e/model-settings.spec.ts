import { expect, test } from '@playwright/test'

test('管理员配置本地与远程模型', async ({ page }) => {
  const password = process.env.AI_STUDIO_E2E_PASSWORD
  if (!password) test.skip(true, 'Set AI_STUDIO_E2E_PASSWORD')
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))

  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工作台' })).toBeVisible()
  await page.getByRole('button', { name: '模型配置' }).click()
  await expect(page.getByRole('heading', { name: '模型配置' })).toBeVisible()
  await expect(page.getByText('仅全局管理员可见')).toBeVisible()
  await page.getByLabel('本地模型名称').fill('qwen3:8b')
  await page.getByLabel('远程接口地址').fill('https://example.com/v1')
  await page.getByLabel('远程模型名称').fill('test-model')
  await page.getByRole('checkbox', { name: /启用远程模型/ }).check()
  await page.getByLabel('远程 API Key').fill('sk-e2e-secret')
  await page.getByRole('button', { name: '保存配置' }).click()
  await expect(page.getByText('模型配置已保存并立即生效')).toBeVisible()
  await expect(page.getByLabel('远程 API Key')).toHaveValue('')
  await expect(page.getByLabel('远程 API Key')).toHaveAttribute('placeholder', /已配置/)
  await page.getByRole('button', { name: '测试连接' }).click()
  await expect(page.getByText(/无法连接|模型服务无法连接/)).toBeVisible({ timeout: 10000 })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('heading', { name: '模型配置' })).toBeVisible()
  await page.screenshot({ path: '/tmp/test-atelier-model-settings-mobile.png' })
  expect(errors).toEqual([])
})
