import { useEffect, useRef, useState } from "react";
import { Clock3, Download, Eye, History, RefreshCw, Trash2 } from "lucide-react";

const MODE_LABELS = {
  excel_to_excel: "Excel → Excel",
  xmind_to_excel: "XMind → Excel",
  xmind_to_final_excel: "XMind → 终极Excel",
  excel_to_xmind: "Excel → XMind",
};

const OUTPUT_LABELS = {
  before: "拼接路径前",
  after: "拼接路径后",
};

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatSize(value) {
  if (!value) return "";
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function historyMeta(record) {
  const mode = MODE_LABELS[record.conversion_mode] || record.conversion_mode;
  const output = record.conversion_mode === "xmind_to_excel" ? ` · ${OUTPUT_LABELS[record.output_mode] || record.output_mode}` : "";
  const version = record.app_version ? ` · v${record.app_version}` : "";
  return `${mode}${output} · ${record.row_count || 0} 条 · ${formatSize(record.file_size)}${version}`;
}

export default function HistoryPanel({
  readonly,
  records,
  retentionDays,
  loading,
  error,
  activeId,
  onRefresh,
  onView,
  onDownload,
  onDelete,
  onClear,
}) {
  const [open, setOpen] = useState(false);
  const panelRef = useRef(null);
  const count = records.length;

  useEffect(() => {
    if (!open) return undefined;
    function handlePointerDown(event) {
      if (panelRef.current && !panelRef.current.contains(event.target)) {
        setOpen(false);
      }
    }
    function handleKeyDown(event) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  function handleView(id) {
    onView(id);
    setOpen(false);
  }

  return (
    <div className="history-panel" ref={panelRef}>
      <button
        className={`history-trigger ${open ? "is-open" : ""}`}
        type="button"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((value) => !value)}
      >
        <History size={16} />
        <span>历史记录</span>
        {count ? <strong>{count}</strong> : null}
      </button>

      {open ? (
        <section className="history-popover" aria-label="历史记录列表">
          <div className="history-heading">
            <span>
              <History size={16} />
              <strong>历史记录</strong>
            </span>
            <div className="history-heading-actions">
              <button
                className="history-clear-button"
                type="button"
                title="清空"
                aria-label="清空全部历史记录"
                onClick={onClear}
                disabled={readonly || loading || !records.length}
              >
                <Trash2 size={14} />
              </button>
              <button type="button" title="刷新历史记录" aria-label="刷新历史记录" onClick={onRefresh} disabled={loading}>
                <RefreshCw className={loading ? "is-spinning" : ""} size={15} />
              </button>
            </div>
          </div>

          <p className="history-hint">
            <Clock3 size={14} />
            历史文件保留 {retentionDays || 180} 天，过期自动清理。
          </p>

          {error ? <div className="history-error">{error}</div> : null}
          {!records.length && !loading ? <div className="history-empty">暂无历史导出</div> : null}
          {loading && !records.length ? <div className="history-empty">正在读取历史记录…</div> : null}

          {records.length ? (
            <div className="history-list">
              {records.map((record) => (
                <article className={`history-item ${activeId === record.id ? "is-active" : ""}`} key={record.id}>
                  <div className="history-item-main">
                    <strong title={record.output_filename}>{record.output_filename}</strong>
                    <small>{historyMeta(record)}</small>
                    <span>{formatDate(record.created_at)} · 来源：{record.source_filename}</span>
                  </div>
                  <div className="history-actions">
                    <button type="button" title="查看历史预览" aria-label="查看历史预览" onClick={() => handleView(record.id)} disabled={loading}>
                      <Eye size={15} />
                    </button>
                    <button type="button" title="下载历史文件" aria-label="下载历史文件" onClick={() => onDownload(record.id)} disabled={loading}>
                      <Download size={15} />
                    </button>
                    <button
                      className="history-delete-button"
                      type="button"
                      title="删除这条历史记录"
                      aria-label={`删除历史记录：${record.output_filename}`}
                      onClick={() => onDelete(record.id)}
                      disabled={readonly || loading}
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                </article>
              ))}
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
