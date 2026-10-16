import { api, rawApi } from '../lib/api';

async function download(path, init) {
  const response = await rawApi(path, init);
  const blob = await response.blob();
  const disposition = response.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  const filename = match ? decodeURIComponent(match[1]) : '用例格式转换结果';
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function inputForm({ file, conversionMode, outputMode, prefixPath, caseType, caseStatus }) {
  const form = new FormData();
  if (file) form.append('file', file);
  form.append('conversion_mode', conversionMode);
  form.append('output_mode', outputMode);
  form.append('prefix_path', prefixPath);
  form.append('case_type', caseType);
  form.append('case_status', caseStatus);
  return form;
}

export function createConverterApi(projectId) {
  const base = `/projects/${projectId}/conversion`;
  return {
    getConfig: () => api(`${base}/config`),
    savePrefixPath: (prefix) => api(`${base}/config`, { method: 'PATCH', body: JSON.stringify({ default_prefix_path: prefix }) }),
    getHistory: () => api(`${base}/history`),
    getHistoryPreview: (id) => api(`${base}/history/${encodeURIComponent(id)}`),
    deleteHistory: (id) => api(`${base}/history/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    clearHistory: () => api(`${base}/history`, { method: 'DELETE' }),
    createPreview: (input) => api(`${base}/preview`, { method: 'POST', body: inputForm(input) }),
    downloadResult: (input) => download(`${base}/export`, { method: 'POST', body: inputForm(input) }),
    downloadHistory: (id) => download(`${base}/history/${encodeURIComponent(id)}` + '/download'),
  };
}
