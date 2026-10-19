import { expect, test } from '@playwright/test'
import path from 'node:path'

test('冒烟集预算提示与执行结果闭环', async ({ page }) => {
  const password = process.env.AI_STUDIO_E2E_PASSWORD
  const sample = process.env.AI_STUDIO_E2E_SAMPLE
  if (!password || !sample) test.skip(true, 'Set AI_STUDIO_E2E_PASSWORD and AI_STUDIO_E2E_SAMPLE')

  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('dialog', dialog => void dialog.accept())

  await page.goto('/')
  await page.getByLabel('账号').fill('admin')
  await page.getByLabel('密码').fill(password!)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工作台' })).toBeVisible()

  await page.getByRole('button', { name: '新建项目' }).first().click()
  await page.getByLabel('项目名称').fill(`冒烟执行-${Date.now()}`)
  await page.getByRole('button', { name: '创建项目' }).click()
  await page.getByRole('button', { name: '用例库' }).first().click()
  await page.locator('header.topbar').getByRole('button', { name: '导入用例' }).click()
  await page.locator('input[type=file]').setInputFiles(path.resolve(sample!))
  await expect(page.getByRole('button', { name: /确认导入/ })).toBeVisible()
  await page.getByRole('button', { name: /确认导入/ }).click()
  await expect(page.getByRole('heading', { name: '用例审核' })).toBeVisible()

  for (const index of [0, 1]) {
    await page.locator('.case-row').nth(index).click()
    await expect(page.getByRole('complementary', { name: '用例审核详情' })).toBeVisible()
    await page.getByRole('combobox', { name: '最终等级' }).click()
    await page.getByRole('option', { name: '等级 2' }).click()
    await page.getByRole('combobox', { name: '最终冒烟' }).click()
    await page.getByRole('option', { name: '是' }).click()
    await page.getByPlaceholder('说明判断依据或修改原因').fill('核心链路人工确认')
    await page.getByRole('button', { name: '保存审核' }).click()
    await expect(page.getByText('审核已保存')).toBeVisible()
    await page.getByRole('button', { name: '关闭详情' }).click()
  }

  await page.getByRole('button', { name: '冒烟执行' }).click()
  await expect(page.getByRole('heading', { name: '冒烟执行' })).toBeVisible()
  await page.getByRole('button', { name: '新建冒烟集' }).click()
  await page.getByLabel('名称').fill('预发布核心链路')
  await page.getByLabel('目标版本').fill('v1.0.0')
  await page.getByLabel('环境').fill('预发布')
  await page.getByLabel('核心链路').fill('登录\n退出')
  await page.getByRole('button', { name: '创建冒烟集' }).click()
  await expect(page.getByText('冒烟集已创建')).toBeVisible()

  // The trigger's accessible name is its aria-label; the placeholder is
  // visible text only and is not used for role queries.
  const candidateSelect = page.getByRole('combobox', { name: '选择冒烟用例' })
  await candidateSelect.click()
  await page.getByRole('option').first().click()
  await page.getByRole('combobox', { name: '选择核心链路' }).click()
  await page.getByRole('option', { name: '登录' }).click()
  await page.getByLabel('预计执行分钟').fill('20')
  await page.getByRole('button', { name: '确认加入' }).click()
  await expect(page.getByText('用例已由人工确认并加入冒烟集')).toBeVisible()

  await candidateSelect.click()
  await page.getByRole('option').first().click()
  await page.getByRole('combobox', { name: '选择核心链路' }).click()
  await page.getByRole('option', { name: '退出' }).click()
  await page.getByLabel('预计执行分钟').fill('20')
  await page.getByRole('button', { name: '确认加入' }).click()
  await expect(page.getByText('预计超过 30 分钟')).toBeVisible()
  await page.getByRole('button', { name: '开始执行' }).click()

  await expect(page.getByRole('tab', { name: '执行记录' })).toBeVisible()
  await expect(page.getByText('本次快照预计超过 30 分钟')).toBeVisible()
  const result = page.getByRole('combobox', { name: /执行结果/ }).first()
  await result.click()
  await page.getByRole('option', { name: '失败' }).click()
  await expect(page.getByRole('button', { name: /保存 .* 执行结果/ }).first()).toBeDisabled()
  await page.getByPlaceholder('必填说明').first().fill('预发布环境缺少测试账号')
  await page.getByRole('button', { name: /保存 .* 执行结果/ }).first().click()
  await expect(page.getByText('执行结果已保存')).toBeVisible()

  const secondResult = page.getByRole('combobox', { name: /执行结果/ }).last()
  await secondResult.click()
  await page.getByRole('option', { name: '跳过' }).click()
  await page.getByPlaceholder('必填说明').last().fill('暂不执行退出链路')
  await page.getByRole('button', { name: /保存 .* 执行结果/ }).last().click()
  await expect(page.getByText('执行结果已保存')).toBeVisible()
  await page.getByRole('button', { name: '结束执行' }).click()
  await expect(page.getByText(/执行已结束/)).toBeVisible()
  await page.screenshot({ path: '/tmp/ai-studio-smoke-desktop.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: '/tmp/ai-studio-smoke-mobile.png' })
  expect(errors).toEqual([])
})
