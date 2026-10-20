import { expect, test } from '@playwright/test'

test('冒烟执行模块已从工作区移除', async ({ page }) => {
  const password = process.env.AI_STUDIO_E2E_PASSWORD
  if (!password) test.skip(true, 'Set AI_STUDIO_E2E_PASSWORD')

  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工作台' })).toBeVisible()
  await expect(page.getByRole('button', { name: '冒烟执行' })).toHaveCount(0)
  await expect(page.getByText('冒烟集', { exact: true })).toHaveCount(0)
})
