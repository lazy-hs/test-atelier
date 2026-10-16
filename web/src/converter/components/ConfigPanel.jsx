import { ArrowRightLeft, FileCheck2, FileSpreadsheet, FileUp, FolderTree, Network, Play, Route } from "lucide-react";
import { useRef, useState } from "react";

const CONVERSION_MODES = [
  { value: "xmind_to_final_excel", label: "XMind → 终极Excel", description: "直接拼接目录并导出", Icon: Route },
  { value: "excel_to_excel", label: "Excel → Excel", description: "拼接目录并套用导入模板", Icon: FileSpreadsheet },
  { value: "xmind_to_excel", label: "XMind → Excel", description: "节点结构转换为原始表格", Icon: Network },
  { value: "excel_to_xmind", label: "Excel → XMind", description: "用例目录生成思维导图", Icon: ArrowRightLeft },
];

const FILE_HINTS = {
  excel_to_excel: { accept: ".xlsx,.csv", title: "拖入 Excel 或 CSV", detail: "支持 .xlsx、.csv，首行需为字段名" },
  xmind_to_excel: { accept: ".xmind", title: "拖入 XMind 文件", detail: "支持经典及新版 .xmind 文件" },
  xmind_to_final_excel: { accept: ".xmind", title: "拖入 XMind 文件", detail: "解析后直接生成最终拼接目录" },
  excel_to_xmind: { accept: ".xlsx", title: "拖入 Excel 文件", detail: "支持 .xlsx，用例目录按“ - ”拆分节点" },
};

export default function ConfigPanel({
  readonly,
  config,
  conversionMode,
  outputMode,
  file,
  prefixPath,
  prefixSaveState,
  caseType,
  caseStatus,
  busy,
  onModeChange,
  onOutputModeChange,
  onFileChange,
  onPrefixChange,
  onCaseTypeChange,
  onCaseStatusChange,
  onConvert,
}) {
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);
  const fileHint = FILE_HINTS[conversionMode];
  const needsOutputMode = conversionMode === "xmind_to_excel";
  const needsImportOptions = conversionMode === "excel_to_excel" || conversionMode === "xmind_to_final_excel" || (needsOutputMode && outputMode === "after");

  function acceptFiles(files) {
    const next = files?.[0];
    if (next) onFileChange(next);
  }

  return (
    <aside className="config-panel">
      <div className="panel-heading">
        <span className="section-index">01</span>
        <span>
          <h2>转换设置</h2>
          <p>选择转换方向、源文件与输出规则</p>
        </span>
      </div>

      <div className="conversion-mode-list" aria-label="转换方式">
        {CONVERSION_MODES.map(({ value, label, description, Icon }) => (
          <button
            type="button"
            className={conversionMode === value ? "conversion-mode is-active" : "conversion-mode"}
            aria-pressed={conversionMode === value}
            disabled={readonly}
            onClick={() => onModeChange(value)}
            key={value}
          >
            <span className="conversion-mode-icon"><Icon size={17} /></span>
            <span><strong>{label}</strong><small>{description}</small></span>
          </button>
        ))}
      </div>

      {needsOutputMode ? (
        <fieldset className="output-mode-field">
          <legend>Excel 输出格式</legend>
          <div className="output-mode-options">
            <label className={outputMode === "before" ? "is-active" : ""}>
              <input type="radio" name="output-mode" value="before" checked={outputMode === "before"} onChange={(event) => onOutputModeChange(event.target.value)} disabled={readonly} />
              <span><strong>拼接路径前</strong><small>输出 7 列原始用例格式</small></span>
            </label>
            <label className={outputMode === "after" ? "is-active" : ""}>
              <input type="radio" name="output-mode" value="after" checked={outputMode === "after"} onChange={(event) => onOutputModeChange(event.target.value)} disabled={readonly} />
              <span><strong>拼接路径后</strong><small>输出 15 列导入模板格式</small></span>
            </label>
          </div>
        </fieldset>
      ) : null}

      <div
        className={`drop-zone ${dragging ? "is-dragging" : ""} ${file ? "has-file" : ""}`}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={(event) => {
          event.preventDefault();
          setDragging(false);
        }}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (!readonly) acceptFiles(event.dataTransfer.files);
        }}
      >
        <input
          key={conversionMode}
          ref={inputRef}
          type="file"
          disabled={readonly}
          accept={fileHint.accept}
          onChange={(event) => acceptFiles(event.target.files)}
          hidden
        />
        <span className="drop-icon">{file ? <FileCheck2 size={24} /> : <FileUp size={24} />}</span>
        {file ? (
          <>
            <strong>{file.name}</strong>
            <small>{(file.size / 1024).toFixed(1)} KB · 已准备转换</small>
            <button className="text-button" type="button" onClick={() => inputRef.current?.click()} disabled={readonly}>
              重新选择
            </button>
          </>
        ) : (
          <>
            <strong>{fileHint.title}</strong>
            <small>{fileHint.detail}</small>
            <button className="outline-button" type="button" onClick={() => inputRef.current?.click()} disabled={readonly}>
              选择文件
            </button>
          </>
        )}
      </div>

      {needsImportOptions ? (
        <div className="import-options">
          <div className="field-group path-field">
            <label htmlFor="prefix-path">
              <FolderTree size={16} />
              前置路径
            </label>
              <textarea
              disabled={readonly}
              id="prefix-path"
              rows="4"
              value={prefixPath}
              onChange={(event) => onPrefixChange(event.target.value)}
              placeholder="例如：测试用例新 - 模块用例 - 02_功能测试"
            />
            <div className="path-field-help">
              <small>转换时会以“{config?.path_separator || " - "}”连接原用例目录。</small>
              <span className={`prefix-save-status is-${prefixSaveState}`} role="status">
                {prefixSaveState === "saving" ? "正在保存…" : null}
                {prefixSaveState === "saved" ? "已保存到项目配置" : null}
                {prefixSaveState === "error" ? "保存失败，请检查项目权限" : null}
                {prefixSaveState === "idle" ? "修改后自动保存" : null}
              </span>
            </div>
          </div>

          <div className="field-row">
            <div className="field-group">
              <label htmlFor="case-type">用例类型</label>
              <select id="case-type" value={caseType} onChange={(event) => onCaseTypeChange(event.target.value)} disabled={readonly}>
                {(config?.case_types || []).map((option) => (
                  <option value={option} key={option}>{option}</option>
                ))}
              </select>
            </div>
            <div className="field-group">
              <label htmlFor="case-status">用例状态</label>
              <select id="case-status" value={caseStatus} onChange={(event) => onCaseStatusChange(event.target.value)} disabled={readonly}>
                {(config?.case_statuses || []).map((option) => (
                  <option value={option} key={option}>{option}</option>
                ))}
              </select>
            </div>
          </div>
        </div>
      ) : null}

      <button className="primary-button convert-button" type="button" onClick={onConvert} disabled={readonly || busy || !file}>
        <Play size={17} fill="currentColor" />
        {busy ? "正在解析…" : "转换并预览"}
      </button>
    </aside>
  );
}
