import { useEffect, useState, type FormEvent } from 'react'
import { CheckCircle2, Eye, EyeOff, FlaskConical, KeyRound, LoaderCircle, Save, Server, ShieldCheck, Terminal } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { api, type AiConfig } from '@/lib/api'

type Settings = AiConfig
type SettingsResponse = Partial<AiConfig>
const defaults: Settings = {
  local_url: 'http://127.0.0.1:11434', local_model: 'qwen3:8b', remote_enabled: false, remote_url: '', remote_model: '',
  remote_api_key_set: false, remote_api_key_mask: '', codex_enabled: false, codex_command: 'codex', codex_model: '',
  claude_enabled: false, claude_command: 'claude', claude_model: '',
}

function normalizeSettings(value: SettingsResponse): Settings {
  return {
    ...defaults,
    ...value,
    // Older API builds called this field `codex_binary`; keep the UI compatible
    // while the setting migrates to the clearer command name.
    codex_command: value.codex_command || value.codex_binary || defaults.codex_command,
  }
}

export function ModelSettingsPage({ projectId }: { projectId: number }) {
  const [settings, setSettings] = useState<Settings>(defaults)
  const [apiKey, setApiKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [busy, setBusy] = useState(false)
  const [testing, setTesting] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [testResult, setTestResult] = useState('')
  const [testProvider, setTestProvider] = useState('local')
  const base = `/projects/${projectId}`

  useEffect(() => { void api<SettingsResponse>(`${base}/ai/admin-config`).then(value => setSettings(normalizeSettings(value))).catch(cause => setError((cause as Error).message)) }, [base])
  function change(name: keyof Settings, value: string | boolean) { setSettings(previous => ({ ...previous, [name]: value })) }
  function payload() {
    return {
      local_url: settings.local_url.trim(), local_model: settings.local_model.trim(), remote_enabled: settings.remote_enabled,
      remote_url: settings.remote_url.trim(), remote_model: settings.remote_model.trim(), remote_api_key: apiKey,
      codex_enabled: settings.codex_enabled, codex_command: settings.codex_command.trim() || 'codex',
      // Keep the binary alias for API versions that shipped before `command`.
      codex_binary: settings.codex_command.trim() || 'codex', codex_model: settings.codex_model.trim(),
      claude_enabled: settings.claude_enabled, claude_command: settings.claude_command.trim() || 'claude',
      claude_model: settings.claude_model.trim(), provider: testProvider,
    }
  }
  async function save(event?: FormEvent) {
    event?.preventDefault(); setBusy(true); setError(''); setNotice(''); setTestResult('')
    try { const value = await api<SettingsResponse>(`${base}/ai/admin-config`, { method: 'PUT', body: JSON.stringify(payload()) }); setSettings(normalizeSettings(value)); setApiKey(''); setNotice('模型配置已保存并立即生效') }
    catch (cause) { setError((cause as Error).message) } finally { setBusy(false) }
  }
  async function testConnection() {
    setTesting(true); setError(''); setTestResult('')
    try { const result = await api<{ message: string; models?: string[] }>(`${base}/ai/admin-config/test`, { method: 'POST', body: JSON.stringify(payload()) }); setTestResult(result.models?.length ? `${result.message} · 已发现 ${result.models.join('、')}` : result.message) }
    catch (cause) { setError((cause as Error).message) } finally { setTesting(false) }
  }
  return <div className="model-settings-page">
    <div className="model-settings-intro"><div><h2>模型配置</h2><p>管理需求分析、用例编写和分类建议使用的 AI 服务</p></div><span className="model-security-badge"><ShieldCheck size={15} />仅全局管理员可见</span></div>
    {error && <div className="ai-alert error" role="alert">{error}</div>}
    {notice && <div className="ai-alert" role="status"><CheckCircle2 size={16} />{notice}</div>}
    {testResult && <div className="ai-alert" role="status"><CheckCircle2 size={16} />{testResult}</div>}
    <form onSubmit={event => void save(event)} className="model-settings-grid">
      <section className="model-settings-section"><div className="model-settings-heading"><span className="model-settings-icon"><Server size={18} /></span><div><h3>本地 Ollama</h3><p>需求正文留在当前设备，默认优先使用</p></div></div>
        <label>服务地址<Input aria-label="本地 Ollama 地址" value={settings.local_url} onChange={event => change('local_url', event.target.value)} placeholder="http://127.0.0.1:11434" required /></label>
        <label>模型名称<Input aria-label="本地模型名称" value={settings.local_model} onChange={event => change('local_model', event.target.value)} placeholder="qwen3:8b" required /></label>
        <p className="model-field-hint">请先在 Ollama 中执行 <code>ollama pull {settings.local_model || 'qwen3:8b'}</code></p>
      </section>
      <section className="model-settings-section"><div className="model-settings-heading"><span className="model-settings-icon"><KeyRound size={18} /></span><div><h3>远程 OpenAI 兼容接口</h3><p>只有显式启用后，正文才会发送到远程服务</p></div></div>
        <label className="model-toggle"><input type="checkbox" checked={settings.remote_enabled} onChange={event => change('remote_enabled', event.target.checked)} /><span><strong>启用远程模型</strong><small>{settings.remote_enabled ? '已允许管理员在 AI 功能中选择' : '当前关闭'}</small></span></label>
        <label>接口地址<Input aria-label="远程接口地址" value={settings.remote_url} onChange={event => change('remote_url', event.target.value)} placeholder="https://api.example.com/v1" /></label>
        <label>模型名称<Input aria-label="远程模型名称" value={settings.remote_model} onChange={event => change('remote_model', event.target.value)} placeholder="model-name" /></label>
        <label>API Key<div className="model-key-input"><Input aria-label="远程 API Key" type={showKey ? 'text' : 'password'} value={apiKey} onChange={event => setApiKey(event.target.value)} placeholder={settings.remote_api_key_set ? '已配置  输入新值可替换' : '仅保存加密摘要'} /><Button type="button" variant="ghost" size="icon-sm" aria-label={showKey ? '隐藏 API Key' : '显示 API Key'} title={showKey ? '隐藏 API Key' : '显示 API Key'} onClick={() => setShowKey(value => !value)}>{showKey ? <EyeOff /> : <Eye />}</Button></div></label>
        <p className="model-field-hint">密钥只在 API 进程使用，不会回显到页面或日志</p>
      </section>
      <section className="model-settings-section cli-settings-section"><div className="model-settings-heading"><span className="model-settings-icon"><Terminal size={18} /></span><div><h3>本机 CLI Provider</h3><p>通过 API 进程启动本机已安装的 Codex CLI 或 Claude Code</p></div></div>
        <div className="cli-local-note"><ShieldCheck size={14} /><span>仅传入当前任务内容 不经过远程接口&nbsp; 命令只接受可执行文件名或绝对路径</span></div>
        <div className="cli-provider-grid">
          <div className={`cli-provider-card ${settings.codex_enabled ? 'is-enabled' : ''}`}>
            <label className="model-toggle"><input type="checkbox" checked={settings.codex_enabled} onChange={event => change('codex_enabled', event.target.checked)} /><span><strong>Codex CLI</strong><small>{settings.codex_enabled ? '可在 AI 任务中选择' : '当前关闭'}</small></span></label>
            <label>命令<Input aria-label="Codex CLI 命令" value={settings.codex_command} onChange={event => change('codex_command', event.target.value)} placeholder="codex" /></label>
            <label>模型（可选）<Input aria-label="Codex CLI 模型" value={settings.codex_model} onChange={event => change('codex_model', event.target.value)} placeholder="留空使用 CLI 默认模型" /></label>
          </div>
          <div className={`cli-provider-card ${settings.claude_enabled ? 'is-enabled' : ''}`}>
            <label className="model-toggle"><input type="checkbox" checked={settings.claude_enabled} onChange={event => change('claude_enabled', event.target.checked)} /><span><strong>Claude Code</strong><small>{settings.claude_enabled ? '可在 AI 任务中选择' : '当前关闭'}</small></span></label>
            <label>命令<Input aria-label="Claude Code 命令" value={settings.claude_command} onChange={event => change('claude_command', event.target.value)} placeholder="claude" /></label>
            <label>模型（可选）<Input aria-label="Claude Code 模型" value={settings.claude_model} onChange={event => change('claude_model', event.target.value)} placeholder="留空使用 CLI 默认模型" /></label>
          </div>
        </div>
        <p className="model-field-hint">保存后，新建的需求分析、用例编写和建议任务可选择对应 CLI&nbsp; 执行中的任务继续使用原配置</p>
      </section>
      <footer className="model-settings-footer"><span><ShieldCheck size={15} />修改后立即影响新建 AI 任务，执行中的任务继续使用原配置</span><div><select aria-label="选择测试 provider" value={testProvider} onChange={event => setTestProvider(event.target.value)} disabled={testing || busy}><option value="local">测试 Ollama</option>{settings.codex_enabled && <option value="codex_cli">测试 Codex CLI</option>}{settings.claude_enabled && <option value="claude_code">测试 Claude Code</option>}</select><Button type="button" variant="outline" onClick={() => void testConnection()} disabled={testing || busy}><FlaskConical data-icon="inline-start" />{testing ? '测试中…' : '测试连接'}</Button><Button type="submit" disabled={busy || testing}><Save data-icon="inline-start" />{busy ? '保存中…' : '保存配置'}</Button></div></footer>
    </form>
    {testing && <div className="model-testing"><LoaderCircle size={16} />正在检查模型服务响应</div>}
  </div>
}
