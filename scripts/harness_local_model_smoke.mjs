#!/usr/bin/env node
// Exercise the real API -> Host -> dsh -> skill path without an external key.
// The deterministic model proves wiring, not the quality of generated content.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { access } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { localSmokeEnvironment } from './harness_local_smoke_environment.mjs'

const projectRoot = fileURLToPath(new URL('../', import.meta.url))
const caseTagException = process.argv.includes('--browser-case-exception')
const caseUserException = process.argv.includes('--browser-case-user-exception')
const browserCaseCheckpoint = process.argv.includes('--browser-case-checkpoint') || caseTagException || caseUserException
const browserCheckpoint = process.argv.includes('--browser-checkpoint') || browserCaseCheckpoint
const browser = browserCheckpoint || process.argv.includes('--browser')
const chunked = process.argv.includes('--chunked')
const staged = process.argv.includes('--staged')
const stagedCaseCheckpoint = process.argv.includes('--staged-cases-checkpoint')
const stagedCases = process.argv.includes('--staged-cases') || stagedCaseCheckpoint
const fragmentCases = process.argv.includes('--fragment-cases')
const allGeneric = process.argv.includes('--all-generic')
const caseCheckpoint = browserCaseCheckpoint || stagedCaseCheckpoint || process.argv.includes('--case-checkpoint')
const analysisCheckpoint = browserCheckpoint || process.argv.includes('--analysis-checkpoint')
assert.ok([browserCheckpoint, process.argv.includes('--browser'), chunked, staged, stagedCases, fragmentCases,
  allGeneric, process.argv.includes('--analysis-checkpoint'), process.argv.includes('--case-checkpoint')].filter(Boolean).length <= 1, 'Choose one smoke mode')
const genericSkills = ['api-parsing', 'dev-doc-analysis', 'figma-to-prd']
const skillRoot = process.env.TEST_ATELIER_SKILL_ROOT || join(projectRoot, '../qa-doc-skills')
const harnessRoot = process.env.HARNESS_ROOT || join(projectRoot, '../deepseek-harness')
const extensionsRoot = process.env.HARNESS_EXTENSIONS_ROOT || join(projectRoot, '../test-atelier-harness-extensions')

function reply(response, delta) {
  response.writeHead(200, { 'content-type': 'text/event-stream' })
  response.end([
    `data: ${JSON.stringify({ choices: [{ delta }] })}`,
    `data: ${JSON.stringify({ choices: [{ delta: {}, finish_reason: delta.tool_calls ? 'tool_calls' : 'stop' }], usage: { prompt_tokens: 3, completion_tokens: 1 } })}`,
    'data: [DONE]', '',
  ].join('\n\n'))
}

let taskType = 'requirement-analysis'
let sourceId = ''
const calledSkills = new Set()
const qualityMarkers = [
  ['unique_requirement_ids', '## 3. 需求清单'], ['module_tree', '## 2. 信息架构与模块'],
  ['traceability_coverage', '## 7. 追溯矩阵'], ['method_precoverage', '## 6. 测试设计预覆盖矩阵'],
  ['risk_registry', '## 8. 风险与待澄清'], ['critical_ambiguities', '### 8.2 待澄清项'],
  ['source_grounding', '来源文档'], ['applicable_platforms', '适用端'],
  ['extended_dimensions', '## 9. 非功能与扩展维度评审'],
  ['nielsen_h1_h10', '## 10. 可用性评审（尼尔森十大原则）'],
  ['nielsen_subchecks', '### 10.2 子检查项缺口汇总'],
  ['usability_review', '### 10.1 原则级结论'],
  ['multi_document_coverage', ''], ['multi_document_deduplication', ''],
]
const qualityReportEvidence = [
  '## 6. 测试设计预覆盖矩阵',
  '| 需求 ID | EP | BVA | ST | EG | 覆盖要点 |',
  '| REQ-1 | ✓ | — | — | ✓ | 登录与错误分支 |',
  '## 7. 追溯矩阵',
  '| 需求 ID | 模块 | 风险类别 | 状态 |',
  '| REQ-1 | 登录 | 账号安全 | 已映射 |',
  '**覆盖率**：100%',
  '## 10. 可用性评审（尼尔森十大原则）',
  '| ID | 原则 | 结论 | 关联需求 ID | 缺口/证据 | 建议验证方向 |',
  ...Array.from({ length: 10 }, (_, index) => `| H${index + 1} | 原则 ${index + 1} | ${index === 0 || index === 8 ? '部分' : '满足'} | REQ-1 | 回环夹具不做语义判断 | 人工核对 |`),
  '### 10.2 子检查项缺口汇总',
  '**子项核对**：已核对 10 项；未满足 2 项',
  '未核对子项：0',
].join('\n')
let mapCalls = 0
let reduceCalls = 0
let genericMapCalls = 0
let genericReduceCalls = 0
let caseMapCalls = 0
let caseReduceCalls = 0
let sawConfirmedDecision = false
let sawCaseDecision = false
let caseQualityCalls = 0
const caseChecks = [
  'requirement_coverage', 'method_coverage', 'priority_distribution', 'executable_steps',
  'source_grounding', 'terminology', 'deduplication', 'module_paths', 'risk_categories',
  'functional_dimensions', 'nonfunctional_security', 'exploratory_dimensions',
  'automation_observability', 'testability', 'extended_dimensions',
]
const server = createServer((request, response) => {
  let body = ''
  request.on('data', chunk => { body += chunk })
  request.on('end', () => {
    try {
      assert.equal(request.url, '/v1/chat/completions')
      const payload = JSON.parse(body)
      const text = JSON.stringify(payload.messages)
      // DSH adds skill-loading user messages after the ACP prompt. Select the
      // last checkpoint-bearing user turn rather than the last user message.
      const caseTurn = payload.messages.filter(message => message.role === 'user').map(message => JSON.stringify(message))
        .filter(message => ['In this first turn, perform the loaded test-cases skill',
          'Continue at the loaded test-cases skill Phase 2.6',
          'This is one explicit human choice for the test-cases skill',
          'Perform the loaded test-cases skill Phase 2.8 quality pre-review',
          'Phase 2.8 passed. Continue Phase 3',
          'The user explicitly requests ONE applicable-platform exception'].some(marker => message.includes(marker))).at(-1) ?? ''
      if (text.includes('Generate the session title')) return reply(response, { content: 'Local smoke' })
      assert.deepEqual(payload.tools?.map(tool => tool.function.name), ['skill'])
      if (!payload.messages.some(message => message.role === 'tool')) {
        taskType = text.includes('skill-execution.v1')
          ? text.match(/exact name (api-parsing|dev-doc-analysis|figma-to-prd)/)?.[1] || 'api-parsing'
          : text.includes('test-cases.v1') ? 'test-cases' : 'requirement-analysis'
        const reduce = text.includes('This is the final reduce pass') || text.includes('This is an intermediate reduce pass')
        const map = text.includes('This is one bounded map window')
          || text.includes('This is one bounded confirmed-analysis window')
        if (map) mapCalls += 1
        if (reduce) reduceCalls += 1
        if (taskType === 'api-parsing' && map) genericMapCalls += 1
        if (taskType === 'api-parsing' && reduce) genericReduceCalls += 1
        if (taskType === 'test-cases' && map) caseMapCalls += 1
        if (taskType === 'test-cases' && reduce) caseReduceCalls += 1
        if (taskType === 'test-cases' && !browser) {
          if (text.includes('AMB-1') || !(stagedCases || fragmentCases)) {
            assert.ok(text.includes('按系统默认规则确认'), 'confirmed ambiguity decision was not sent to Harness')
            sawConfirmedDecision = true
          }
          assert.ok(!text.includes('report_markdown'), 'duplicate analysis report was sent to Harness')
        }
        const marker = text.lastIndexOf('source_documents')
        const found = marker < 0 ? null : text.slice(marker, marker + 1600).match(/[a-f0-9]{32}/i)
        assert.ok(found || (reduce && sourceId), 'source ID was not present in the authorized task input')
        if (!reduce) sourceId = found[0]
        return reply(response, { tool_calls: [{
          index: 0, id: `call_${taskType}`, type: 'function',
          function: { name: 'skill', arguments: JSON.stringify({ name: taskType }) },
        }] })
      }
      assert.ok(text.includes(`qa-doc-skills/skills/${taskType}/SKILL.md`))
      calledSkills.add(taskType)
      if (analysisCheckpoint && taskType === 'requirement-analysis' && text.includes('For this first turn')
          && !text.includes('The Phase 2.5 checkpoint has been reviewed')) {
        return reply(response, { content: JSON.stringify({ schema: 'skill-checkpoint.v1', phase: '2.5',
          summary: '## 解析完成\n模块 1 个 需求 1 条\n适用端待确认', applicable_platforms: '', warnings: ['适用端待确认'] }) })
      }
      if (analysisCheckpoint && taskType === 'requirement-analysis') {
        assert.ok(text.includes('仅 Web 适用'), 'ACP did not forward the Phase 2.5 decision in the same Session')
        if (!text.includes('self-check passed') && text.includes('Perform the loaded requirement-analysis skill Phase 2.8 pre-delivery self-check now')) {
          return reply(response, { content: JSON.stringify({ schema: 'skill-checkpoint.v1', phase: '2.8', status: 'pass',
            checks: qualityMarkers.map(([id, evidence_marker]) => ({ id, outcome: id.startsWith('multi_document_') ? 'not_applicable' : 'pass', evidence_marker })),
            metrics: { source_document_count: 1, requirement_count: 1, traceability_coverage_percent: 100, unmapped_source_count: 0 }, warnings: [] }) })
        }
        if (!text.includes('self-check passed')) {
          assert.ok(text.includes('Continue the loaded requirement-analysis skill at Phase 2.6'))
          const browserItem = browserCheckpoint && text.includes('连续失败五次后锁定账号')
          const apiItem = !browserCheckpoint && text.includes('系统支持用户使用正确账号登录。')
          if ((browserItem || apiItem)
              && !text.includes('This is an explicit user decision about one critical ambiguity')) {
            return reply(response, { content: JSON.stringify({ schema: 'skill-checkpoint.v1', phase: '2.6',
              status: 'item', ambiguity_id: 'AMB-1', requirement_id: 'REQ-1', source_document_id: sourceId,
              source_text: browserItem ? '连续失败五次后锁定账号' : '系统支持用户使用正确账号登录。', ambiguity_type: '边界不明确',
              interpretation: browserItem ? '连续第五次失败即锁定' : '使用正确账号即可登录' }) })
          }
          if (text.includes('This is an explicit user decision about one critical ambiguity')) {
            assert.ok(text.includes('mark_pending'), 'Phase 2.6 decision was not sent in the same ACP Session')
          }
          return reply(response, { content: JSON.stringify({ schema: 'skill-checkpoint.v1', phase: '2.6', status: 'complete' }) })
        }
        assert.ok(text.includes('Phase 2.8 pre-delivery self-check passed'))
      }
      if (caseCheckpoint && taskType === 'test-cases' && caseTurn) {
        if (caseTurn.includes('In this first turn, perform the loaded test-cases skill Phase 1 and 2')) {
          return reply(response, { content: JSON.stringify({ schema: 'case-checkpoint.v1', phase: '2.5',
            summary: `## 用例解析完成\n模块 1 个 需求 ${stagedCaseCheckpoint ? 90 : 1} 条\n适用端待确认`,
            applicable_platforms: '', warnings: ['适用端待确认'] }) })
        }
        if (caseTurn.includes('Continue at the loaded test-cases skill Phase 2.6')) {
          assert.ok(caseTurn.includes('applicable_platforms') && caseTurn.includes('Web'),
            'Case tag was not passed to the same ACP Session')
          return reply(response, { content: JSON.stringify({ schema: 'skill-checkpoint.v1', phase: '2.6',
            status: 'item', ambiguity_id: 'CASE-AMB-1', requirement_id: 'REQ-1', source_document_id: sourceId,
            source_text: stagedCaseCheckpoint ? '系统支持用户登录。' : '用户可以使用账号登录',
            ambiguity_type: '边界不明确', interpretation: '正确账号可以登录' }) })
        }
        if (caseTurn.includes('This is one explicit human choice for the test-cases skill')) {
          assert.ok(caseTurn.includes('accept_suggestion'), 'Case decision was not sent to the same ACP Session')
          sawCaseDecision = true
          return reply(response, { content: JSON.stringify({ schema: 'skill-checkpoint.v1', phase: '2.6', status: 'complete' }) })
        }
        if (caseTurn.includes('Perform the loaded test-cases skill Phase 2.8 quality pre-review')) {
          caseQualityCalls += 1
          return reply(response, { content: JSON.stringify({ schema: 'case-checkpoint.v2', phase: '2.8',
            status: 'pass', checks: caseChecks.map(id => ({ id, outcome: id === 'priority_distribution' && !stagedCaseCheckpoint ? 'not_applicable' : 'pass', evidence: `reviewed ${id}` })),
            metrics: { requirement_count: stagedCaseCheckpoint ? 90 : 1,
              planned_case_count: stagedCaseCheckpoint ? 90 : 2, coverage_percent: 100,
              p0_percent: stagedCaseCheckpoint ? 13.33 : 0,
              p1_percent: stagedCaseCheckpoint ? 35.56 : 50,
              p2_percent: stagedCaseCheckpoint ? 35.56 : 50,
              p3_percent: stagedCaseCheckpoint ? 15.56 : 0,
              risk_category_count: 1, risk_categories: [stagedCaseCheckpoint ? '安全' : '网络异常'], extended_pending_count: 0 }, warnings: [] }) })
        }
        assert.ok(caseTurn.includes('Phase 2.8 passed. Continue Phase 3')
          || caseTurn.includes('The user explicitly requests ONE applicable-platform exception'),
        'Cases were requested before Phase 2.8 passed')
      }
      const refs = [{ document_id: sourceId, locator: 'req.md:1' }]
      const reviewedAmbiguity = analysisCheckpoint && text.includes('This is an explicit user decision about one critical ambiguity')
      let caseInputs
      if ((stagedCases || fragmentCases) && taskType === 'test-cases') {
        const userPrompt = payload.messages.find(message => typeof message.content === 'string'
          && message.content.includes('"source_documents"'))?.content
        assert.ok(userPrompt, 'Cannot locate bounded case prompt')
        const input = JSON.parse(userPrompt.slice(userPrompt.lastIndexOf('\n\n') + 2))
        caseInputs = input.analysis?.partial_results
          ? input.analysis.partial_results.flatMap(item => item.data.cases)
          : input.analysis.requirements.map(item => ({
            id: `TC-${item.id}-${item.fragment?.start ?? 0}`, title: `验证 ${item.id}`,
            requirement_ids: [item.id], preconditions: [],
            steps: [{ order: 1, action: '执行操作', expected: '完成' }], expected: '完成',
            method: 'EP', priority: 'medium', labels: ['Web'],
            traceability: [{ requirement_id: item.id, acceptance_criterion_id: `AC-${item.id.slice(4)}` }],
            quality_warnings: [],
          }))
      }
      const result = {
        schema: taskType === 'test-cases' ? 'test-cases.v1'
          : genericSkills.includes(taskType) ? 'skill-execution.v1' : 'requirement-analysis.v1',
        result_id: 'local-result', task_id: 'host-owned', session_id: 'host-owned',
        source_document_ids: [sourceId], generated_at: '2026-10-25T00:00:00Z', warnings: [],
        data: genericSkills.includes(taskType)
          ? { output: taskType === 'figma-to-prd'
            ? '# 产品需求文档（PRD）：回环夹具设计证据\n\n待确认：真实 Figma 读稿未执行'
            : 'local generic skill result', items: [], metadata: {} }
          : taskType === 'test-cases'
          ? { cases: stagedCaseCheckpoint && caseTurn.includes('Phase 2.8 passed. Continue Phase 3')
            ? caseInputs.map((item, index) => ({ ...item,
              module_path: index === 89 ? ['风险点', '安全'] : ['登录'],
              skill_priority: index < 12 ? 'P0' : index < 44 ? 'P1' : index < 76 ? 'P2' : 'P3',
              labels: ['Web'],
            }))
            : stagedCases || fragmentCases ? caseInputs : [{
            id: 'TC-1', title: '有效账号登录', module_path: ['登录', '主路径'], skill_priority: 'P1', requirement_ids: ['REQ-1'], preconditions: ['存在账号'],
            steps: [{ order: 1, action: '输入账号密码并登录', expected: '登录成功' }],
            expected: '进入首页', method: 'EP', priority: 'medium', labels: ['Web'],
            traceability: [{ requirement_id: 'REQ-1', acceptance_criterion_id: 'AC-1' }], quality_warnings: [],
          }, {
            id: 'TC-2', title: '登录请求中断', module_path: ['风险点', '网络异常'], skill_priority: 'P2', requirement_ids: ['REQ-1'], preconditions: ['存在账号'],
            steps: [{ order: 1, action: '登录时中断网络', expected: '显示可恢复提示' }],
            expected: '不重复创建登录态', method: 'EG', priority: 'medium', labels: caseTagException || caseUserException && caseTurn.includes('The user explicitly requests ONE applicable-platform exception') ? ['Pc'] : ['Web'],
            traceability: [{ requirement_id: 'REQ-1', acceptance_criterion_id: 'AC-1' }], quality_warnings: [],
          }] }
          : {
            requirements: fragmentCases ? [{
              id: 'REQ-1', title: '长需求', description: '需要覆盖全部内容。' + 'x'.repeat(70000),
              priority: 'medium', source_refs: refs,
              acceptance_criteria: [{ id: 'AC-1', text: '可登录', source_refs: refs }],
            }] : stagedCases ? Array.from({ length: 90 }, (_, index) => ({
              id: `REQ-${index + 1}`, title: `需求 ${index + 1}`, description: '系统支持用户登录。'.repeat(80),
              priority: 'medium', source_refs: refs,
              acceptance_criteria: [{ id: `AC-${index + 1}`, text: '可登录', source_refs: refs }],
            })) : [{
              id: 'REQ-1', title: '登录', description: '用户可以使用账号登录', priority: 'medium',
              source_refs: refs, acceptance_criteria: [{ id: 'AC-1', text: '正确账号可以登录', source_refs: refs }],
            }],
            ambiguities: analysisCheckpoint ? reviewedAmbiguity ? [{
              id: 'AMB-1', question: '主路径边界待确认', severity: 'blocking',
              related_requirement_ids: ['REQ-1'], source_refs: refs, resolution: null,
            }] : [] : browser ? [] : [{
              id: 'AMB-1', question: '错误密码的提示是什么', severity: 'blocking',
              related_requirement_ids: ['REQ-1'], source_refs: refs, resolution: null,
            }],
            quality_warnings: [],
          },
      }
      if (taskType === 'test-cases') result.analysis_run_id = 'host-owned'
      if (genericSkills.includes(taskType)) result.skill_id = taskType
      if (taskType === 'requirement-analysis') result.report_markdown = analysisCheckpoint
        ? '# 登录需求分析\n\n来源文档：req.md\n适用端 Web\n\n'
          + qualityMarkers.filter(([, marker]) => marker.startsWith('#')).map(([, marker]) => marker).join('\n')
          + '\n\nREQ-1 来源材料中的账号登录要求\n' + qualityReportEvidence + '\n'
          + (reviewedAmbiguity ? 'AMB-1 待确认\n' : '')
        : '# 登录需求分析\n\n来源材料中的账号登录要求\n'
      reply(response, { content: JSON.stringify(result) })
    } catch (error) {
      if (stagedCases || fragmentCases || caseCheckpoint) console.error('local smoke model rejected request:', taskType, error.name,
        error.code || '', error.name === 'AssertionError' ? error.message : 'invalid request shape')
      response.writeHead(500)
      response.end('invalid local model request')
    }
  })
})

try {
  await access(join(skillRoot, 'skills/requirement-analysis/SKILL.md'))
  await access(join(harnessRoot, 'package.json'))
  await access(join(extensionsRoot, 'host/server.mjs'))
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const env = {
    ...localSmokeEnvironment(),
    TEST_ATELIER_SKILL_ROOT: skillRoot,
    HARNESS_ROOT: harnessRoot,
    HARNESS_EXTENSIONS_ROOT: extensionsRoot,
    OPENAI_API_KEY: 'local-mock-model-only',
    OPENAI_API_BASE: `http://127.0.0.1:${server.address().port}/v1`,
    OPENAI_MODEL: 'ugreen-ai-model',
  }
  const child = spawn('python', [browser ? 'scripts/harness_browser_smoke.py' : 'scripts/harness_real_smoke.py'], {
    cwd: projectRoot,
    env: {
      ...env,
      ...(browser ? { TEST_ATELIER_HARNESS_BROWSER_REAL_DSH: '1' } : {}),
      ...(chunked ? { TA_LOCAL_SMOKE_CHUNKED: '1', TEST_ATELIER_HARNESS_CHUNK_CHARS: '1000',
        TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS: '50' } : {}),
      ...(staged ? { TA_LOCAL_SMOKE_STAGED: '1', TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES: '65536' } : {}),
      ...(allGeneric ? { TA_LOCAL_SMOKE_ALL_GENERIC: '1' } : {}),
      ...(stagedCases ? { TA_LOCAL_SMOKE_STAGED_CASES: '1', TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES: '65536' } : {}),
      ...(fragmentCases ? { TA_LOCAL_SMOKE_FRAGMENT_CASES: '1', TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES: '65536' } : {}),
      ...(analysisCheckpoint || caseCheckpoint ? { HARNESS_DSH_PROTOCOL: 'acp' } : {}),
      ...(analysisCheckpoint ? { HARNESS_ANALYSIS_CHECKPOINTS: '1', TA_LOCAL_SMOKE_CHECKPOINT: '1' } : {}),
      ...(caseCheckpoint ? { HARNESS_CASE_CHECKPOINTS: '1', TA_LOCAL_SMOKE_CASE_CHECKPOINT: '1' } : {}),
      ...(caseTagException || caseUserException ? { TA_LOCAL_SMOKE_CASE_EXCEPTION: '1' } : {}),
      ...(caseUserException ? { TA_LOCAL_SMOKE_CASE_USER_EXCEPTION: '1' } : {}),
    },
    stdio: ['ignore', 'pipe', 'pipe'], detached: process.platform !== 'win32',
  })
  let stdout = ''
  let stderr = ''
  child.stdout.on('data', chunk => { stdout += chunk })
  child.stderr.on('data', chunk => { stderr += chunk })
  const timeout = setTimeout(() => {
    try {
      if (process.platform === 'win32') child.kill('SIGTERM')
      else process.kill(-child.pid, 'SIGTERM')
    } catch { /* already exited */ }
  }, browser ? 240_000 : chunked || staged || stagedCases || fragmentCases || allGeneric ? 180_000 : 120_000)
  try {
    const code = await new Promise((done, reject) => {
      child.once('error', reject)
      child.once('exit', done)
    })
    const failure = stderr.match(/(?:AssertionError|TimeoutError|RuntimeError):[^\n]*/g)?.at(-1)
      || (browser ? (stdout.slice(-2400) || stderr.slice(-800)) : 'check the local smoke log')
    if (code !== 0 && (browser || stagedCases || fragmentCases || caseCheckpoint || allGeneric)) {
      console.error(stdout.slice(0, 2600))
      console.error(stderr.slice(-1400))
    }
    assert.equal(code, 0, `Real Harness bridge smoke failed: ${failure}`)
    assert.ok(stdout.includes(browser
      ? 'Stopped backup/restore smoke: Harness Sessions, evidence, frozen reports and active execution recovery verified'
      : 'real Harness bridge smoke: analysis, ambiguity confirmation, cases, draft save and publish passed'))
    if (browser) assert.ok(stdout.includes('Recovered Harness analysis deletion: Host outage retained intent, restart removed tasks and Sessions'))
    assert.deepEqual([...calledSkills].sort(), allGeneric || browser
      ? [...genericSkills, 'requirement-analysis', 'test-cases'].sort()
      : staged ? ['api-parsing', 'requirement-analysis', 'test-cases'] : ['requirement-analysis', 'test-cases'])
    if (chunked || staged) assert.ok(mapCalls >= 2 && reduceCalls >= 1, 'Real dsh map/reduce did not execute')
    if (staged) assert.ok(genericMapCalls >= 2 && genericReduceCalls >= 1, 'Generic skill map/reduce did not execute')
    if (stagedCases || fragmentCases) assert.ok(caseMapCalls >= 2 && caseReduceCalls >= 1,
      `Case analysis map/reduce did not execute (${caseMapCalls} map, ${caseReduceCalls} reduce)`)
    if (stagedCases || fragmentCases) assert.ok(sawConfirmedDecision, 'No case window received the human-confirmed decision')
    if (caseCheckpoint) assert.ok(sawCaseDecision && caseQualityCalls >= 1, 'Real dsh did not traverse the case Phase 2.6/2.8 checkpoints')
    console.log(browser
      ? `Real dsh browser + local model: ${browserCheckpoint ? 'Phase 2.5/2.6/2.8 checkpoints, ' : ''}${caseCheckpoint ? 'case checkpoints, ' : ''}analysis, cases, project skills, Sessions and backup recovery passed`
      : allGeneric ? 'Real dsh + local model: all three project skills, Sessions and publish passed'
      : staged ? 'Real dsh staged input + local model: analysis, generic skill, map/reduce, Sessions and publish passed'
        : stagedCaseCheckpoint ? 'Real dsh staged case context + local model: 90 requirements, map/reduce and Phase 2.5/2.6/2.8 passed'
        : stagedCases ? 'Real dsh staged case context + local model: full confirmed analysis, bounded cases and publish passed'
          : fragmentCases ? 'Real dsh fragmented case context + local model: long requirement, bounded cases and publish passed'
        : chunked ? 'Real dsh map/reduce + local model: document chunks, Sessions, confirmation and publish passed'
          : caseCheckpoint ? 'Real dsh ACP + local model: case Phase 2.5/2.6/2.8, original cases and publish passed'
          : analysisCheckpoint ? 'Real dsh ACP + local model: Phase 2.5 confirmation, Phase 2.6 decision, Phase 2.8 self-check and publish passed'
        : 'Real Harness API bridge + local model: analysis, ambiguity confirmation, cases and publish passed')
  } finally {
    clearTimeout(timeout)
  }
} finally {
  if (server.listening) await new Promise(resolve => server.close(resolve))
}
