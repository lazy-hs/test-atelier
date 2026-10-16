import { expect, test } from '@playwright/test'
import path from 'node:path'

test('分类版本、规则候选、人工修改与离线 AI 任务', async ({ page }) => {
  const password = process.env.AI_STUDIO_E2E_PASSWORD
  if (!password) test.skip(true, 'Set AI_STUDIO_E2E_PASSWORD')
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('dialog', dialog => void dialog.accept())
  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('button', { name: '新建项目' }).first().click()
  await page.getByLabel('项目名称').fill(`分类验收-${Date.now()}`)
  await page.getByRole('button', { name: '创建项目' }).click()
  await page.getByRole('button', { name: '分类规则' }).click()
  await expect(page.getByRole('heading', { name: '分类规则' })).toBeVisible()
  await page.getByRole('button', { name: '新建版本' }).click()
  for (const [dimension, value] of [
    ['业务模块', '监控中心模块用例'], ['业务场景', '兼容性'],
    ['测试维度', '业务功能用例'], ['端类型', 'Web'],
  ]) {
    const region = page.getByRole('region', { name: dimension })
    await region.getByRole('button', { name: '添加' }).click()
    await region.getByRole('textbox', { name: `${dimension}名称 1` }).fill(value)
    await region.getByRole('textbox', { name: `${dimension}说明 1` }).fill('验收分类')
  }
  await page.getByRole('button', { name: '保存草稿' }).click()
  await expect(page.getByText('分类草稿 v1 已保存')).toBeVisible()
  await page.getByRole('button', { name: '发布版本' }).click()
  await expect(page.getByText('当前发布 v1')).toBeVisible()
  await page.locator('.taxonomy-page').evaluate(element => { element.scrollTop = 0 })
  await page.waitForTimeout(250)
  await page.screenshot({ path: '/tmp/ai-studio-taxonomy-desktop.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: '/tmp/ai-studio-taxonomy-mobile.png' })
  await page.setViewportSize({ width: 1280, height: 720 })

  await page.getByRole('button', { name: '用例库' }).first().click()
  await page.getByRole('button', { name: '导入用例' }).click()
  await page.locator('input[type=file]').setInputFiles(path.resolve(process.env.AI_STUDIO_E2E_SAMPLE || '/Users/lazy/Downloads/UGOS_PRO_测试用例_20261015141029.xls'))
  await expect(page.getByText('100 条用例 · 13 个原始字段')).toBeVisible()
  await page.getByRole('button', { name: '确认导入 100 条' }).click()
  await page.getByRole('checkbox', { name: '选择用例 001252040' }).check()
  await page.getByRole('button', { name: '规则建议' }).click()
  await expect(page.getByText('已生成 1 条规则候选')).toBeVisible()
  await page.getByText('001252040').first().click()
  await page.getByRole('tab', { name: '建议' }).click()
  await expect(page.getByText('规则候选', { exact: true })).toBeVisible()
  await expect(page.getByText('原始冒烟为空，保持待判定')).toBeVisible()
  await expect(page.getByRole('combobox', { name: '业务模块建议' })).toContainText('监控中心模块用例')
  await page.getByRole('combobox', { name: '修改建议冒烟' }).click()
  await page.getByRole('option', { name: '是', exact: true }).click()
  await page.getByRole('textbox', { name: '建议决策理由' }).fill('人工确认这是核心链路')
  await page.getByRole('button', { name: '修改并接受' }).click()
  await expect(page.getByText('建议决策已保存')).toBeVisible()
  await expect(page.getByRole('combobox', { name: '最终冒烟' })).toContainText('是')
  await page.screenshot({ path: '/tmp/ai-studio-suggestion-accepted.png' })

  await page.getByRole('button', { name: '关闭详情' }).click()
  await page.locator('.case-row').nth(1).getByRole('checkbox').check()
  await page.getByRole('button', { name: 'AI 建议' }).click()
  await expect(page.getByText('AI 建议失败')).toBeVisible({ timeout: 15000 })
  await expect(page.getByRole('button', { name: '重试' })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: '/tmp/ai-studio-suggestion-mobile.png' })
  expect(errors).toEqual([])
})
