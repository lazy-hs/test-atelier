import { useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, X } from "lucide-react";
import { createConverterApi } from "./api";
import AppHeader from "./components/AppHeader";
import ConfigPanel from "./components/ConfigPanel";
import HistoryPanel from "./components/HistoryPanel";
import PreviewWorkspace from "./components/PreviewWorkspace";
import StepRail from "./components/StepRail";
import UsageGuide from "./components/UsageGuide";

const MODE_EXTENSIONS = {
  excel_to_excel: [".xlsx", ".csv"],
  xmind_to_excel: [".xmind"],
  xmind_to_final_excel: [".xmind"],
  excel_to_xmind: [".xlsx"],
};

export default function Converter({ projectId, readonly = false }) {
  const { clearHistory, createPreview, deleteHistory, downloadHistory, downloadResult,
    getConfig, getHistory, getHistoryPreview, savePrefixPath } = useMemo(() => createConverterApi(projectId), [projectId]);
  const [config, setConfig] = useState(null);
  const [conversionMode, setConversionMode] = useState("excel_to_excel");
  const [outputMode, setOutputMode] = useState("after");
  const [file, setFile] = useState(null);
  const [prefixPath, setPrefixPath] = useState("");
  const [savedPrefixPath, setSavedPrefixPath] = useState(null);
  const [prefixSaveState, setPrefixSaveState] = useState("idle");
  const [caseType, setCaseType] = useState("");
  const [caseStatus, setCaseStatus] = useState("");
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [stale, setStale] = useState(false);
  const [exported, setExported] = useState(false);
  const [history, setHistory] = useState({ records: [], retention_days: 180 });
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [historyRecord, setHistoryRecord] = useState(null);
  const [activePage, setActivePage] = useState("converter");

  async function loadHistory() {
    setHistoryLoading(true);
    setHistoryError("");
    try {
      const result = await getHistory();
      setHistory(result);
    } catch (requestError) {
      setHistoryError(requestError.message);
    } finally {
      setHistoryLoading(false);
    }
  }

  useEffect(() => {
    let active = true;
    getConfig()
      .then((value) => {
        if (!active) return;
        setConfig(value);
        setPrefixPath(value.default_prefix_path);
        setSavedPrefixPath(value.default_prefix_path);
        setCaseType(value.default_case_type);
        setCaseStatus(value.default_case_status);
      })
      .catch((requestError) => setError(requestError.message));
    loadHistory();
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(""), 3000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!config || savedPrefixPath === null || prefixPath === savedPrefixPath) return undefined;

    let active = true;
    setPrefixSaveState("saving");
    const timer = window.setTimeout(async () => {
      try {
        const result = await savePrefixPath(prefixPath);
        if (!active) return;
        setSavedPrefixPath(result.default_prefix_path);
        setConfig((current) => ({ ...current, default_prefix_path: result.default_prefix_path }));
        setPrefixSaveState("saved");
      } catch {
        if (active) setPrefixSaveState("error");
      }
    }, 600);

    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [config, prefixPath, savedPrefixPath]);

  function resetResult() {
    setPreview(null);
    setStale(false);
    setExported(false);
    setError("");
    setNotice("");
    setHistoryRecord(null);
  }

  function handleModeChange(nextMode) {
    setConversionMode(nextMode);
    setOutputMode(nextMode === "xmind_to_excel" ? "before" : "after");
    setFile(null);
    resetResult();
  }

  function markChanged(setter) {
    return (value) => {
      setter(value);
      setHistoryRecord(null);
      setExported(false);
      if (preview) setStale(true);
    };
  }

  function handleFileChange(nextFile) {
    const suffix = `.${nextFile.name.split(".").pop()?.toLowerCase() || ""}`;
    if (!MODE_EXTENSIONS[conversionMode].includes(suffix)) {
      setFile(null);
      setPreview(null);
      setError(`当前转换方式仅支持 ${MODE_EXTENSIONS[conversionMode].join("、")} 文件。`);
      return;
    }
    setFile(nextFile);
    resetResult();
  }

  const requestInput = { file, conversionMode, outputMode, prefixPath, caseType, caseStatus };

  async function handleConvert() {
    if (!file) {
      setError("请先选择源用例文件。");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await createPreview(requestInput);
      setPreview(result);
      setStale(false);
      setExported(false);
      setNotice(`已完成 ${result.row_count} 条用例转换`);
    } catch (requestError) {
      setPreview(null);
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleExport() {
    setBusy(true);
    setError("");
    try {
      await downloadResult(requestInput);
      setExported(true);
      setNotice("导出文件已生成");
      loadHistory();
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleHistoryView(id) {
    setBusy(true);
    setError("");
    try {
      const result = await getHistoryPreview(id);
      setPreview(result.preview);
      setHistoryRecord(result.record);
      setStale(false);
      setExported(true);
      setNotice("已打开历史预览");
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleHistoryDownload(id) {
    setBusy(true);
    setError("");
    try {
      await downloadHistory(id);
      setNotice("历史文件已下载");
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function handleHistoryDelete(id) {
    const record = history.records.find((item) => item.id === id);
    const name = record?.output_filename || "这条历史记录";
    if (!window.confirm(`确定删除“${name}”吗？\n删除后，历史文件和预览将无法恢复。`)) return;

    setHistoryLoading(true);
    setHistoryError("");
    try {
      const result = await deleteHistory(id);
      setHistory(result);
      if (historyRecord?.id === id) {
        setPreview(null);
        setHistoryRecord(null);
        setStale(false);
        setExported(false);
      }
      setNotice("历史记录已删除");
    } catch (requestError) {
      setHistoryError(requestError.message);
    } finally {
      setHistoryLoading(false);
    }
  }

  async function handleHistoryClear() {
    const count = history.records.length;
    if (!count || !window.confirm(`确定清空全部 ${count} 条历史记录吗？\n清空后，所有历史文件和预览将无法恢复。`)) return;

    setHistoryLoading(true);
    setHistoryError("");
    try {
      const result = await clearHistory();
      setHistory(result);
      if (historyRecord) {
        setPreview(null);
        setHistoryRecord(null);
        setStale(false);
        setExported(false);
      }
      setNotice(`已清空 ${result.deleted_count ?? count} 条历史记录`);
    } catch (requestError) {
      setHistoryError(requestError.message);
    } finally {
      setHistoryLoading(false);
    }
  }

  const activeStep = exported ? 3 : preview ? (stale ? 1 : 2) : 1;

  return (
    <div className="app-shell">
      <AppHeader
        templateFile={config?.template_file}
        appVersion={config?.app_version}
        activePage={activePage}
        onPageChange={setActivePage}
      />
      {activePage === "guide" ? (
        <UsageGuide config={config} />
      ) : (
        <>
          <StepRail activeStep={activeStep}>
            <HistoryPanel
              readonly={readonly}
              records={history.records || []}
              retentionDays={history.retention_days || config?.history_retention_days}
              loading={historyLoading}
              error={historyError}
              activeId={historyRecord?.id}
              onRefresh={loadHistory}
              onView={handleHistoryView}
              onDownload={handleHistoryDownload}
              onDelete={readonly ? undefined : handleHistoryDelete}
              onClear={readonly ? undefined : handleHistoryClear}
            />
          </StepRail>
          <main className="converter-layout">
            <ConfigPanel
              readonly={readonly}
              config={config}
              conversionMode={conversionMode}
              outputMode={outputMode}
              file={file}
              prefixPath={prefixPath}
              prefixSaveState={prefixSaveState}
              caseType={caseType}
              caseStatus={caseStatus}
              busy={busy}
              onModeChange={handleModeChange}
              onOutputModeChange={markChanged(setOutputMode)}
              onFileChange={handleFileChange}
              onPrefixChange={markChanged(setPrefixPath)}
              onCaseTypeChange={markChanged(setCaseType)}
              onCaseStatusChange={markChanged(setCaseStatus)}
              onConvert={readonly ? undefined : handleConvert}
            />
            <PreviewWorkspace
              readonly={readonly}
              preview={preview}
              busy={busy}
              stale={stale}
              error={error}
              historyRecord={historyRecord}
              onExport={historyRecord ? () => handleHistoryDownload(historyRecord.id) : handleExport}
            />
          </main>
        </>
      )}

      {notice ? (
        <div className="toast success-toast" role="status">
          <CheckCircle2 size={18} />
          <span>{notice}</span>
          <button aria-label="关闭提示" onClick={() => setNotice("")}><X size={15} /></button>
        </div>
      ) : null}
      {error && preview ? (
        <div className="toast error-toast" role="alert">
          <AlertCircle size={18} />
          <span>{error}</span>
          <button aria-label="关闭错误" onClick={() => setError("")}><X size={15} /></button>
        </div>
      ) : null}
    </div>
  );
}
