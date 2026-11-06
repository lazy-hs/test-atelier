# Harness 真实模型质量验收

协议、Schema、浏览器和回环假模型测试只证明链路可运行，不证明模型理解了真实需求。
Phase 2.8 的 Host 检查可拒绝缺少检查项、低于声明覆盖门槛或报告与结构化结果矛盾的任务；
它仍依赖模型对源材料的自评，不能替代本文件的人工 Gold 语义评审。
发布前在 macOS、Windows、Linux 各自的目标运行方式执行真实模型任务，由测试人员
独立评审需求分析和用例结果；此前在对话中暴露的模型凭据必须先轮换，不能用于验收。

## 评审准备

1. 选取获授权的真实材料，预先由人工列出需求、关键需求、阻断歧义和验收条件的
   Gold ID 与原文定位；保留正常、缺失信息、边界、跨文档冲突及长材料样本。
2. 在 Test Atelier 中执行 Harness 需求分析，核对并下载原始 Markdown 报告，人工处理阻断歧义，
   再执行用例编写。旧结果若没有原始报告，须重新执行分析，不能用本地兼容报告代替。
   仅在任务 `status=completed` 后保存两次任务的完整 `/result` API 响应（保留外层
   `task_id`、`project_id`、`analysis_run_id`、`source_document_ids`、`status`、`result_schema`
   和 `result`，不可只保存内部结果），以及材料版本、模型版本、Profile、执行时间、
   Session ID、运行平台和评审人；必须记录完整的 Harness/扩展 Git SHA（脏工作区不能
   作为可复现发布基线）。文件放在受控的 checkout 外目录，勿提交仓库。
   需求分析、用例编写和项目技能页面的「下载原始 JSON」直接保存受权限保护的完整
   `/result` 响应；按钮会再次读取任务且只在响应确认 `completed` 和任务归属一致时
   下载，不从页面显示的内部 JSON 拼装外层字段。它不自动填写模型/Profile/平台、
   人工 Gold 或 Git SHA；下载后的敏感材料仍应保存在受控目录并禁止提交仓库。
3. 对每条 Gold 逐项判定 `correct`、`partial` 或 `missing`，记录模型结果 ID 与简短
   来源定位。逐项分类所有额外的模型需求、歧义和用例；不能略过看似合理的额外输出。
   `partial` 不计入完整覆盖，原始文本及人工判定应由授权人员另行保留供复核。
4. 在运行前由验收负责人确定 `requirements_min`、`acceptance_min` 两个比例门槛。
   工具无默认门槛；无论比例如何，关键需求和阻断歧义都必须全部正确，且不能有
   未支撑的需求/歧义或不可用用例。不能用结构化 Schema 通过代替语义判定。

## 评审文件

`harness-quality-review.v3` 只记录 ID、判断、定位和报告 SHA-256，不复制原文。旧版 v1
缺少验收条件追溯绑定，v2 未评审 Harness 原始报告，均不再作为发布评分输入。`observed_ids`
必须来自相应 Harness 原始结果，`missing` 必须是空列表。下例仅示意文件结构，
Gold 内容、比例门槛和评审判定必须按真实材料重新填写：

```json
{
  "schema": "harness-quality-review.v3",
  "reviewer": "reviewer-id",
  "execution": {
    "platform": "linux",
    "harness_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "extension_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "profile": "test-atelier",
    "model": "reviewed-model-id"
  },
  "analysis_task_id": "analysis-task-id",
  "cases_task_id": "cases-task-id",
  "analysis_run_id": "analysis-run-id",
  "gold": {
    "requirements": [
      {"id": "G-REQ-1", "critical": true, "observed_ids": ["REQ-1"], "verdict": "correct", "evidence": "source:section-1"}
    ],
    "blocking_ambiguities": [
      {"id": "G-AMB-1", "critical": true, "observed_ids": ["AMB-1"], "verdict": "correct", "evidence": "source:section-2"}
    ],
    "acceptance_criteria": [
      {"id": "G-AC-1", "critical": true, "requirement_gold_id": "G-REQ-1", "observed_criterion_ids": ["AC-1"], "observed_ids": ["TC-1"], "verdict": "correct", "evidence": "source:section-3"}
    ]
  },
  "additional_supported_requirements": [],
  "additional_ambiguities": [],
  "additional_usable_cases": [],
  "unsupported_requirement_ids": [],
  "unsupported_ambiguity_ids": [],
  "unusable_case_ids": [],
  "report_review": {
    "sha256": "<分析结果中 report_markdown 的 UTF-8 原始字节 SHA-256>",
    "dimensions": [
      {"name": "requirement_completeness", "verdict": "pass", "evidence": "report:section-1"},
      {"name": "module_coverage", "verdict": "pass", "evidence": "report:section-2"},
      {"name": "acceptance_testability", "verdict": "pass", "evidence": "report:section-3"},
      {"name": "rules_and_boundaries", "verdict": "pass", "evidence": "report:section-4"},
      {"name": "method_planning", "verdict": "pass", "evidence": "report:section-5"},
      {"name": "risk_categories", "verdict": "pass", "evidence": "report:section-6"},
      {"name": "traceability", "verdict": "pass", "evidence": "report:section-7"},
      {"name": "source_grounding", "verdict": "pass", "evidence": "report:section-8"},
      {"name": "multi_document_coverage", "verdict": "pass", "evidence": "report:section-9"},
      {"name": "multi_document_deduplication", "verdict": "pass", "evidence": "report:section-10"},
      {"name": "usability_review", "verdict": "pass", "evidence": "report:section-11"}
    ],
    "source_coverage": [
      {"document_id": "<实际来源文档 ID>", "verdict": "covered", "evidence": "report:source-coverage"}
    ]
  },
  "limits": {"requirements_min": 0.9, "acceptance_min": 0.9}
}
```

没有 Gold 阻断歧义时，`blocking_ambiguities` 可为空列表。额外的合法结果以
`{"id": "REQ-2", "evidence": "source:section-4"}` 等形式放入对应的
`additional_*` 列表；被判为无依据或不可用的 ID 放入对应的否定列表。
所有模型需求、歧义和用例必须且只能归入一种分类。对于 Gold 阻断歧义，评为
`correct` 的模型歧义还必须维持 `severity=blocking` 和 `resolution=null`。
同一 Gold 阻断歧义映射多个模型 ID 时，每个 ID 均须保持阻断未决；不能用其中
一条合格记录掩盖另一条已经解决或不再阻断的记录。
每条 Gold 验收条件还要以 `requirement_gold_id` 指向本次 Gold 需求行，
`observed_criterion_ids` 列出对应分析结果中的验收条件 ID。评为 `correct` 时不能为空，
且每条对应的模型用例都必须通过 `traceability` 指向该需求下的验收条件；
每个被映射的模型验收条件也都必须由至少一条对应的模型用例追溯，不能仅用
命中其中一项的用例把多项映射整体判为已覆盖。
`partial` 允许用例只追溯到相应需求，不计入完整覆盖；`missing` 不列出用例 ID。
模型用例若只追溯到另一需求或另一验收条件，不能用人工映射误报为覆盖。
报告检查必须逐项填写 11 个维度及每一份任务来源文档的覆盖结论；缺项、错绑报告哈希
或缺少原始 Markdown 均不能评分。维度与真实需求分析技能的完整性、模块、可测性、
规则、方法、风险、追溯、来源、多文档覆盖/去重和可用性检查对应；即使单文档也需对
多文档两项明确记录本任务不适用的证据，而非略去。任一维度 `fail` 或任一来源
`missing` 时发布评分不通过。`evidence` 只能放简短定位，不复制报告或需求正文。
CLI 不判断人工结论是否真实，也不能以这里的逐项评分替代技能 Phase 2.8 自检。
评审工具会对两次响应的外层任务 ID、项目 ID、来源文档 ID、分析运行 ID、完成状态
及内部用例分析 ID 做联合核对；同一批材料的不同分析运行不能混作一次验收。

在三个系统均可运行以下只读命令，将参数替换为受控目录内的实际绝对路径：

```text
python scripts/evaluate_harness_quality.py --analysis /secure/analysis.json --cases /secure/cases.json --review /secure/review.json
```

退出码 `0` 表示已提交的人工评审通过其明确门槛，`1` 表示不通过，`78` 表示输入
不完整或不可信。标准输出仅包含汇总比例、失败类别和三个输入文件的 SHA-256，
不回显原始需求、模型文本或人工定位；留存 SHA-256 可与受控原件对账。
两份完整 `/result` 响应各最多 128 MiB（包含 JSON 包络及转义开销），人工评审文件
最多 16 MiB；超过上限、符号链接、非普通文件、打开前后路径被替换或读取期间
文件大小/修改时间改变均拒绝，不截断后继续计分。受控原件在人工评分期间仍应
保持停写；这些身份和元数据检查不能代替不可变存储或人工来源核验。该预算
用于有界离线核验，不提高 Host/Bridge 的输出及传输上限。

## 验收边界

- 人工 Gold 本身必须经过评审；脚本只能验证记录完整、一致并计算比率，不能
  判断评审人是否正确阅读材料，也不能证明没有遗漏的 Gold 项。
- 结构化追溯只证明模型 ID 之间的链接成立；即使链接正确，也不证明用例步骤、
  断言与原始验收条件在语义上一致，仍须人工逐项判断。
- 固定假模型的示例文件只验证脚本和链路，不可填入发布报告作为真实模型质量。
- 跨平台验收须在目标系统运行真实服务并记录任务、恢复、退出和模型表现；
  CI 配置、macOS 结果或单个正例不能代替 Windows/Linux 实机证据。
- 报告需同时记录分窗成本、耗时、失败/取消、Session 回放与权限、人工修正量；
  不通过时保留 Harness 原始结果和 Session，不能自动发布草稿。
