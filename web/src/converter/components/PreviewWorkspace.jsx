import { Columns2, Download, FileSearch, Search, Table2 } from "lucide-react";
import { useDeferredValue, useMemo, useState } from "react";
import DataTable from "./DataTable";
import TreePreview from "./TreePreview";

const MODES = [
  ["converted", "转换后", Table2],
  ["original", "转换前", FileSearch],
  ["compare", "并排对照", Columns2],
];

function filterRows(rows, keyword) {
  const needle = keyword.trim().toLowerCase();
  if (!needle) return rows;
  return rows.filter((row) => Object.values(row).some((value) => String(value ?? "").toLowerCase().includes(needle)));
}

function filterTree(node, keyword) {
  const needle = keyword.trim().toLowerCase();
  if (!node || !needle) return node;
  const children = (node.children || []).map((child) => filterTree(child, keyword)).filter(Boolean);
  const ownText = [node.title, node.notes, ...(node.markers || [])].join(" ").toLowerCase();
  return ownText.includes(needle) || children.length ? { ...node, children } : null;
}

function PreviewSurface({ kind, headers, rows, tree, compact, keyword }) {
  if (kind === "tree") return <TreePreview tree={tree} forceOpen={Boolean(keyword.trim())} />;
  return <DataTable headers={headers || []} rows={rows || []} compact={compact} />;
}

function formatHistoryDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export default function PreviewWorkspace({ preview, busy, stale, error, historyRecord, onExport, readonly }) {
  const [mode, setMode] = useState("converted");
  const [keyword, setKeyword] = useState("");
  const deferredKeyword = useDeferredValue(keyword);

  const sourceRows = useMemo(
    () => filterRows(preview?.source_rows || [], deferredKeyword),
    [preview?.source_rows, deferredKeyword],
  );
  const targetRows = useMemo(
    () => filterRows(preview?.target_rows || [], deferredKeyword),
    [preview?.target_rows, deferredKeyword],
  );
  const sourceTree = useMemo(
    () => filterTree(preview?.source_tree, deferredKeyword),
    [preview?.source_tree, deferredKeyword],
  );
  const targetTree = useMemo(
    () => filterTree(preview?.target_tree, deferredKeyword),
    [preview?.target_tree, deferredKeyword],
  );

  if (!preview) {
    return (
      <section className="preview-workspace empty-workspace">
        <div className="empty-illustration" aria-hidden="true">
          <span className="sheet sheet-back" />
          <span className="sheet sheet-front"><Table2 size={34} /></span>
        </div>
        <h2>{busy ? "正在读取用例内容" : "转换结果会在这里展开"}</h2>
        <p>{error || (busy ? "正在识别表格字段与 XMind 节点…" : "选择转换方式并上传文件，即可核对转换前后的表格或节点结构。")}</p>
        {error ? <span className="error-hint">请修正文件或配置后重试</span> : null}
      </section>
    );
  }

  const sourceCount = preview.source_kind === "table" ? sourceRows.length : preview.row_count;
  const targetCount = preview.target_kind === "table" ? targetRows.length : preview.row_count;
  const showsWideTarget = mode !== "original" && preview.target_kind === "table" && preview.target_headers.length > 10;
  const subtitle = historyRecord
    ? `历史记录 · ${formatHistoryDate(historyRecord.created_at)} · ${historyRecord.output_filename}`
    : `${preview.filename} · ${preview.sheet_name}`;

  return (
    <section className="preview-workspace">
      <div className="preview-header">
        <div>
          <div className="preview-title-line">
            <h2>用例预览</h2>
            <span className="count-badge">{preview.row_count} 条</span>
            {historyRecord ? <span className="history-badge">历史</span> : null}
            {stale ? <span className="stale-badge">配置已变更</span> : null}
          </div>
          <p>{subtitle}</p>
        </div>
        <button className="primary-button export-button" type="button" onClick={onExport} disabled={busy || stale || (readonly && !historyRecord)}>
          <Download size={17} />
          {busy ? "处理中…" : historyRecord ? "下载历史文件" : preview.export_label || "导出结果"}
        </button>
      </div>

      <div className="table-toolbar">
        <div className="mode-switch" role="tablist" aria-label="预览模式">
          {MODES.map(([value, label, Icon]) => (
            <button
              type="button"
              role="tab"
              aria-selected={mode === value}
              className={mode === value ? "is-active" : ""}
              onClick={() => setMode(value)}
              key={value}
            >
              <Icon size={15} />
              {label}
            </button>
          ))}
        </div>
        <div className="toolbar-side">
          {showsWideTarget ? <span className="wide-table-hint">15 列模板 · 横向滚动查看全部字段</span> : null}
          <label className="search-box">
            <Search size={16} />
            <input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索标题、目录、步骤或节点" />
          </label>
        </div>
      </div>

      {stale ? <div className="stale-banner">左侧配置已修改，请重新“转换并预览”后再导出。</div> : null}

      {mode === "converted" ? (
        <PreviewSurface
          kind={preview.target_kind}
          headers={preview.target_headers}
          rows={targetRows}
          tree={targetTree}
          keyword={deferredKeyword}
        />
      ) : null}
      {mode === "original" ? (
        <PreviewSurface
          kind={preview.source_kind}
          headers={preview.source_headers}
          rows={sourceRows}
          tree={sourceTree}
          keyword={deferredKeyword}
        />
      ) : null}
      {mode === "compare" ? (
        <div className="compare-grid">
          <div className="compare-pane">
            <div className="compare-label"><span>转换前</span><small>{sourceCount} 条</small></div>
            <PreviewSurface
              kind={preview.source_kind}
              headers={preview.source_headers}
              rows={sourceRows}
              tree={sourceTree}
              compact
              keyword={deferredKeyword}
            />
          </div>
          <div className="compare-arrow" aria-hidden="true">→</div>
          <div className="compare-pane">
            <div className="compare-label is-after"><span>转换后</span><small>{targetCount} 条</small></div>
            <PreviewSurface
              kind={preview.target_kind}
              headers={preview.target_headers}
              rows={targetRows}
              tree={targetTree}
              compact
              keyword={deferredKeyword}
            />
          </div>
        </div>
      ) : null}
    </section>
  );
}
