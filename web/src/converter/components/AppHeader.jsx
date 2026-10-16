import { BookOpenText, FileSpreadsheet, Sparkles } from "lucide-react";

const NAV_ITEMS = [
  { value: "converter", label: "转换台", Icon: FileSpreadsheet },
  { value: "guide", label: "使用说明", Icon: BookOpenText },
];

export default function AppHeader({ templateFile, appVersion, activePage, onPageChange }) {
  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <FileSpreadsheet size={20} strokeWidth={2.2} />
        </span>
        <span>
          <strong>用例格式转换台</strong>
          <small>Excel 与 XMind 双向转换、核对与导出</small>
        </span>
      </div>
      <div className="header-actions">
        <nav className="page-tabs" aria-label="主页面">
          {NAV_ITEMS.map(({ value, label, Icon }) => (
            <button
              type="button"
              className={activePage === value ? "is-active" : ""}
              aria-current={activePage === value ? "page" : undefined}
              onClick={() => onPageChange(value)}
              key={value}
            >
              <Icon size={15} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <span className="version-badge" title={appVersion ? `当前版本 v${appVersion}` : "正在读取版本号"}>
          v{appVersion || "..."}
        </span>
        <div className="template-indicator" title={templateFile || "正在读取模板"}>
          <Sparkles size={15} />
          <span>当前模板</span>
          <strong>{templateFile || "读取中…"}</strong>
        </div>
      </div>
    </header>
  );
}
