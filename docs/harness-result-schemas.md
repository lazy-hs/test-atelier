# Harness 结果 Schema

需求分析、用例编写和受限项目技能的结果必须经过 Test Atelier 服务端的版本化 JSON 校验。Harness 可以生成文本和结构化候选，但只有通过 Schema、来源追溯和业务质量门禁的结果才允许进入业务表。

## 1. 通用包络

```json
{
  "schema": "requirement-analysis.v1",
  "result_id": "result_01",
  "task_id": "task_01",
  "session_id": "session_01",
  "source_document_ids": ["src_01"],
  "generated_at": "2026-10-23T10:01:00Z",
  "warnings": [],
  "data": {}
}
```

`schema` 是不可省略的判别字段。未知版本必须拒绝落库并保留原始 Session；不能静默按旧版本解析。新增可选字段不改变版本，改变字段含义、必填性、枚举或嵌套结构时创建新版本。
`source_document_ids` 必须无重复且恰好覆盖任务输入的所有文档；遗漏输入文档或添加任务外文档都会拒绝投影。包络、分析及用例中的质量告警若填写 `source_refs`，也必须只引用本任务文档。

## 2. `requirement-analysis.v1`

```json
{
  "schema": "requirement-analysis.v1",
  "result_id": "result_01",
  "task_id": "task_01",
  "session_id": "session_01",
  "source_document_ids": ["src_01"],
  "generated_at": "2026-10-23T10:01:00Z",
  "warnings": [
    {"code": "missing_acceptance_detail", "message": "边界条件需要确认", "source_refs": [{"document_id": "src_01", "locator": "paragraph:13"}]}
  ],
  "report_markdown": "# 需求分析：上传文件\n\n模型原始报告正文",
  "data": {
    "requirements": [
      {
        "id": "REQ-001",
        "title": "用户上传文件",
        "description": "用户可以上传受支持的需求材料",
        "priority": "high",
        "source_refs": [{"document_id": "src_01", "locator": "paragraph:12"}],
        "acceptance_criteria": [
          {"id": "AC-001", "text": "超过大小限制时返回明确错误", "source_refs": [{"document_id": "src_01", "locator": "paragraph:13"}]}
        ]
      }
    ],
    "ambiguities": [
      {
        "id": "AMB-001",
        "question": "是否允许同名文件覆盖",
        "severity": "blocking",
        "related_requirement_ids": ["REQ-001"],
        "source_refs": [{"document_id": "src_01", "locator": "paragraph:14"}],
        "resolution": null
      }
    ],
    "quality_warnings": []
  }
}
```

服务端校验规则：

- `requirements[].id`、`acceptance_criteria[].id` 和 `ambiguities[].id` 在结果内唯一且稳定。
- 每个需求和验收条件至少有一个 `source_refs`，引用的 `document_id` 必须属于任务输入。
- `severity=blocking` 的歧义必须在业务确认前保持 `resolution=null`，不能生成可发布用例。
- `priority`、`severity` 等枚举未知时拒绝结果，不自动降级为普通文本。
- `warnings` 是诊断信息，不得替代来源追溯或人工确认。
- `report_markdown` 为可选字段，旧结果继续有效。存在时保留模型的原始 Markdown 文本，
  首个非空白内容须为一级标题，最长 1 MiB；结构化结果和报告相互独立，禁止用
  Test Atelier 的兼容视图伪装为原始技能报告。
- 新版独立 Host 执行需求分析时，无论单窗口或最终聚合，都必须收到可交付的原始报告；
  缺失、非一级 Markdown 标题或 UTF-8 超过 1 MiB 时直接发出 `report_missing` 失败事件，
  不发 `task.result`/`task.completed`。旧版已完成任务的 Schema 仍允许该字段缺省，
  供受权读取历史结果；缺报告不能通过新版离线发布质量门禁。
- `GET /harness/tasks/{task_id}/report.md` 只向已授权项目提供已完成且确实含原文的结果；
  未完成和历史缺字段结果返回 409。下载内容与 `/result` 中原字段一致，不修改原结果。
  该接口只证明原文交付链路，不能代替技能 Phase、来源零遗漏或质量门禁的完整验收。

确认后 Test Atelier 在 `analysis_runs.result` 中保存原始结构化结果和确认状态；人工编辑记录在独立审计字段中，不覆盖 Harness 原始结果。

## 3. `test-cases.v1`

```json
{
  "schema": "test-cases.v1",
  "result_id": "result_02",
  "task_id": "task_02",
  "session_id": "session_02",
  "source_document_ids": ["src_01"],
  "analysis_run_id": "analysis_01",
  "generated_at": "2026-10-23T10:05:00Z",
  "warnings": [],
  "data": {
    "cases": [
      {
        "id": "TC-001",
        "title": "上传超限文件失败",
        "module_path": ["上传", "文件校验"],
        "skill_priority": "P1",
        "requirement_ids": ["REQ-001"],
        "preconditions": ["用户已登录"],
        "steps": [
          {"order": 1, "action": "选择超过限制的文件", "expected": "页面显示大小限制错误"}
        ],
        "expected": "文件不进入需求材料列表",
        "method": "BVA",
        "priority": "high",
        "labels": ["Web"],
        "traceability": [
          {"requirement_id": "REQ-001", "acceptance_criterion_id": "AC-001"}
        ],
        "quality_warnings": []
      }
    ]
  }
}
```

服务端校验规则：

- `analysis_run_id` 必须指向同一项目内已确认且未被撤销的分析运行。
- 每个用例至少关联一个 `requirement_id`，并且只能引用分析结果中的需求。
- `traceability` 必须恰好覆盖该用例的全部 `requirement_ids`，不能加入其他需求；
  每个验收条件 ID 必须属于同一条追溯关系中的需求，不能借用另一条需求的条件。
- `steps[].order` 从 1 开始连续递增；每步必须同时有 `action` 和 `expected`。
- `method` 在 v1 业务草稿投影中只能是 `EP`、`BVA`、`ST`、`EG`；其他文案（包括 `boundary`）校验失败，不能伪装成 `EP`。需求分析 Schema 不提供方法建议，兼容视图的 `methods` 保持空数组，不能补造 `EP`。
- `priority` 和标签长度、步骤数量遵循项目质量门禁；超限时任务失败而不是截断。
- 用例 ID 在本次结果内唯一；保存为 `case_drafts` 前由服务端重新生成业务主键。
- `module_path`（1～8 层）和 `skill_priority`（P0～P3）分别保留源技能的模块路径与优先级，
  独立于业务草稿的 `priority=high/medium/low` 和适用端 `labels`。旧版 v1 结果允许缺省，
  但不会从标签、业务优先级或需求标题反推后导出 XMind。
- XMind 是原技能交付物，其每条用例只能有一个适用端标签值：`Pc`、`Web`、`Android`、
  `IOS` 或英文逗号加空格分隔的无重复组合（如 `Web, IOS`，仍放在 `labels` 数组的
  单个元素中）。业务标签、空标签、重复/大小写错误的标签不能混入技能导出；
  ACP 用例检查点中的非默认标签须逐条经用户明确批准后才接受结果，拒绝或取消
  均不能投影业务草稿。普通历史 `test-cases.v1` 结果仍可按既有 Schema 只读查看。
- Phase 2.8 的内部 `case-checkpoint.v2` 要求模型列出已识别的风险类别名称和数量；
  Host 对最终 `module_path=["风险点", "类别", ...]` 的类别集合逐项核对，不能
  用相同数量的其他类别替换。该检查点不是导出的原始 `test-cases.v1` 包络。
- 只对项目内已完成、带齐模块及源技能优先级且含 `风险点/风险类别` 模块的原始用例结果提供
  `GET /harness/tasks/{task_id}/xmind`。该接口以独立临时目录、无 shell 参数调用原
  `qa-doc-skills/scripts/generate_xmind.py`；`/result` 仍保持原始结构化结果，
  XMind 是从中导出的视图，不等同于证明其余技能阶段或模型质量已达标。
  下载时还会复核原始结果的任务和分析归属、用例 ID 与步骤序号，并在各用例笔记中
  保留 Harness 任务、分析运行、结果、Session、来源文档、时间、逐条和结果级质量提醒
  及其来源定位；笔记不替代受项目权限保护的原始 `/result` 和 Session。
- `traceability` 缺失、错绑或关键歧义未确认时，结果不能投影为业务草稿或已发布 `cases`；
  Host 原始 Session 仍可供受权复核，不能自动替模型补造追溯关系。

## 4. `skill-execution.v1`

通用项目技能使用统一包络，`data` 只允许保存该 Skill 的命名空间结果，不推断业务实体：

```json
{
  "schema": "skill-execution.v1",
  "result_id": "result_03",
  "task_id": "task_03",
  "session_id": "session_03",
  "skill_id": "api-parsing",
  "source_document_ids": ["src_01"],
  "generated_at": "2026-10-23T10:10:00Z",
  "warnings": [],
  "data": {"output": "接口解析摘要", "endpoints": [], "models": [], "errors": [], "test_points": []}
}
```

服务端仍校验 `skill_id`、来源文档和任务绑定；未知 Skill、越权来源、未知 Schema 或
超大输出直接失败。通用结果保留在 `HarnessTask.result` 和 Session 回放中，后续如要
进入业务表必须新增独立的领域 Schema 和投影，不能从任意 `data` 自动生成业务记录。
新任务的 `data` 必须含有非空的用户可见交付内容，可以是 `output` 文本，也可以是
`summary`、`findings`、`endpoints` 等结构化内容；仅空数组、空白文本或 `metadata`
不算交付。`figma-to-prd` 另外要求 `data.output` 是以
`# 产品需求文档（PRD）：` 开头的完整 Markdown。Host 在发出结果前执行该门禁，
Test Atelier 投影时再次核对；旧任务结果仅保持受权只读兼容。
当前执行路径只接收用户上传并授权的文档，Figma 材料须由用户预先导出；
不提供实时 Figma MCP 读稿，也不自动验证上传材料确实来自该设计稿。
缺少可读的设计事实时不能把模型自行猜测当作 PRD 交付。

## 5. 原始值、建议值和最终值

Harness 结果只属于“建议/草稿”层。Test Atelier 必须继续分离：

```text
original: 上传材料或导入文件中的原始值
suggested: Harness 生成的结构化候选
final: 用户确认后发布的值
```

编辑、确认、驳回、发布和导出都要产生审计记录，不能通过更新 Harness Session 来伪造人工决定。页面可以展示流式文本，但业务保存只接受最后一次通过 Schema 校验的完整结果。

## 6. 校验和版本测试

扩展仓库应提供纯函数 Schema 校验器和以下测试：

1. 最小有效结果、完整结果和空数组结果。
2. 缺失判别字段、未知版本、未知枚举、重复 ID 和非法来源引用。
3. blocking ambiguity 未确认时阻止用例编写和发布。
4. 多字节文本、过长字段、过多需求、步骤和标签的边界限制。
5. 重复 SSE 事件、页面刷新和重复提交不会产生重复业务记录。
6. Schema 失败时仍保留 `task_id`、`session_id`、原始文本摘要和诊断错误。

Schema 失败的诊断只保存受保护业务投影中的结果摘要：结果哈希、字节数、结果类型、
已知 `schema`、模型自报标识的哈希和有限的字段名列表。不会把 Pydantic 的
`input_value` 或模型返回的完整正文写入 `HarnessTask.error`、本地事件或普通日志；
需要复核原始输出时必须通过任务绑定的 Harness Session 回放。这样既保留了故障定位
能力，也避免在数据库中复制需求正文或模型输出。

任何 Schema 版本升级都要同时更新：协议文档、TypeScript 类型、Python 校验器、API 契约测试、结构化结果快照和迁移/回滚说明。
