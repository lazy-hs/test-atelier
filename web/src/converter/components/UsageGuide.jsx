import {
  AlertTriangle,
  BadgeCheck,
  BookOpenText,
  CircleDot,
  FileSpreadsheet,
  Flag,
  GitBranch,
  ListChecks,
  NotebookPen,
  Rows3,
  ScrollText,
} from "lucide-react";

const SOURCE_HEADERS = ["用例目录", "用例名称", "用例等级", "前置条件", "用例步骤", "预期结果", "是否是冒烟用例"];
const XMIND_FIELD_HEADERS = ["用例等级", "前置条件", "用例步骤", "预期结果", "是否冒烟用例"];

const XMIND_RULES = [
  { Icon: Flag, title: "冒烟标记", detail: "字段节点写“是否冒烟用例：是/否”；未明确填写时应核对预览，不要将空值当作否。" },
  { Icon: CircleDot, title: "优先级标记", detail: "支持 1-4、priority-1 至 priority-4，或 P0-P3 等级文本转换。" },
  { Icon: NotebookPen, title: "前置条件", detail: "Excel 转 XMind 时写入用例节点备注；XMind 导入可用备注或字段节点承载。" },
];

const WORKFLOWS = [
  ["XMind → 终极Excel", "上传 .xmind 后填写前置路径，直接生成最终拼接目录的 Excel。"],
  ["Excel → Excel", "上传至少包含基础字段的源表，填写前置路径、用例类型和状态后导出。"],
  ["XMind → Excel", "上传 .xmind，系统会解析节点、备注和标记，输出路径拼接前的原始表格。"],
  ["Excel → XMind", "上传 Excel，按“用例目录”拆分层级，生成带步骤和预期结果的 .xmind 文件。"],
];

export default function UsageGuide({ config }) {
  const separator = config?.path_separator || " - ";
  const appVersion = config?.app_version || "...";

  return (
    <main className="guide-page">
      <section className="guide-hero">
        <span className="guide-hero-icon" aria-hidden="true">
          <ScrollText size={35} />
        </span>
        <span className="guide-hero-copy">
          <h1>使用说明与格式规范</h1>
          <p>按当前转换逻辑整理 XMind 与 Excel 的文件要求、节点结构和字段映射规则。</p>
        </span>
      </section>

      <section className="guide-content">
        <div className="guide-alert" role="note">
          <AlertTriangle size={18} />
          <strong>关键提示：</strong>
          <span>正确的字段名、节点层级和路径分隔符，是预览、导出和历史复用稳定的基础。</span>
        </div>

        <div className="guide-card-grid">
          {XMIND_RULES.map(({ Icon, title, detail }) => (
            <article className="guide-info-card" key={title}>
              <span aria-hidden="true"><Icon size={28} /></span>
              <h2>{title}</h2>
              <p>{detail}</p>
            </article>
          ))}
        </div>

        <section className="guide-section">
          <div className="guide-section-heading">
            <BookOpenText size={21} />
            <span>
              <h2>XMind 格式规范</h2>
              <p>适用于 XMind → Excel 导入，也适用于从 Excel 反向生成 XMind 后的核对。</p>
            </span>
          </div>
          <div className="guide-split">
            <div className="guide-block">
              <h3>结构层级要求</h3>
              <ol className="guide-number-list">
                <li><strong>根节点：</strong>产品或项目名称。</li>
                <li><strong>目录节点：</strong>模块、功能或场景目录，可按层级继续向下拆分。</li>
                <li><strong>用例节点：</strong>节点标题会转换为“用例名称”。</li>
                <li><strong>步骤节点：</strong>用例节点下的子节点会汇总为“用例步骤”。</li>
                <li><strong>预期结果节点：</strong>步骤节点下的子节点会汇总为“预期结果”。</li>
              </ol>
            </div>
            <div className="guide-block">
              <h3>字段节点写法</h3>
              <p>用例节点下也可以直接放字段节点，标题支持“字段名：内容”或字段名独占一行。</p>
              <div className="guide-chip-list" aria-label="XMind 字段节点">
                {XMIND_FIELD_HEADERS.map((header) => <span key={header}>{header}</span>)}
              </div>
            </div>
          </div>
        </section>

        <section className="guide-section">
          <div className="guide-section-heading">
            <FileSpreadsheet size={21} />
            <span>
              <h2>Excel 字段规范</h2>
              <p>源文件首行必须是字段名；用例名称、前置条件、用例步骤、预期结果为必填列。</p>
            </span>
          </div>
          <div className="guide-split">
            <div className="guide-block">
              <h3>识别的 7 个基础字段</h3>
              <div className="guide-chip-list" aria-label="Excel 源字段">
                {SOURCE_HEADERS.map((header) => <span key={header}>{header}</span>)}
              </div>
            </div>
            <div className="guide-block">
              <h3>读取要求</h3>
              <ol className="guide-number-list">
                <li><strong>字段名匹配：</strong>系统按字段名读取，不依赖列顺序。</li>
                <li><strong>额外列：</strong>不会影响解析，但不会进入本工具的转换输出。</li>
                <li><strong>必填内容：</strong>每条有效数据的“用例名称”不能为空。</li>
              </ol>
            </div>
          </div>
        </section>

        <section className="guide-section">
          <div className="guide-section-heading">
            <GitBranch size={21} />
            <span>
              <h2>路径与转换规则</h2>
              <p>当前路径分隔符为“{separator}”，前置路径会与源“用例目录”按该分隔符拼接。</p>
            </span>
          </div>
          <div className="guide-flow-list">
            {WORKFLOWS.map(([title, detail]) => (
              <article className="guide-flow-item" key={title}>
                <span aria-hidden="true"><Rows3 size={18} /></span>
                <strong>{title}</strong>
                <p>{detail}</p>
              </article>
            ))}
          </div>
          <div className="guide-meta-row">
            <span><BadgeCheck size={16} />当前版本 v{appVersion}</span>
            <span><BadgeCheck size={16} />Excel 源文件需包含 4 个必填字段</span>
            <span><BadgeCheck size={16} />支持 .xlsx、.csv、.xmind</span>
            <span><ListChecks size={16} />导出前建议先核对预览结果</span>
          </div>
        </section>
      </section>
    </main>
  );
}
