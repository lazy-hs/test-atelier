import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { Box, Check, CheckCircle2, ChevronRight, CircleDot, Cpu, Eye, EyeOff, FlaskConical, Globe2, GripVertical, LoaderCircle, Save, ShieldCheck, Terminal, Trash2, Plus, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { api, type AiConfig } from '@/lib/api'

type Settings = AiConfig
type SettingsResponse = Partial<AiConfig>
type ProviderKind = 'remote' | 'cli'
type ProviderProtocol = 'openai' | 'responses' | 'anthropic' | 'gemini' | 'system-one'
type ManagedProvider = { id: string; kind: ProviderKind; name: string; endpoint: string; model: string; enabled: boolean; detail: string; protocol?: ProviderProtocol; api_key_set?: boolean; api_key?: string }
type AiTool = NonNullable<AiConfig['tools']>[number]
type ModelOption = { id: string; name: string }
type ProbeResult = { ok: boolean; protocol: ProviderProtocol; model?: string; status?: number; error?: string }

const defaults: Settings = {
  local_url: '', local_model: '', local_enabled: false, remote_enabled: false, remote_url: '', remote_model: '',
  remote_api_key_set: false, remote_api_key_mask: '', codex_enabled: false, codex_command: 'codex', codex_model: '',
  claude_enabled: false, claude_command: 'claude', claude_model: '',
}

function normalizeSettings(value: SettingsResponse): Settings { return { ...defaults, ...value, codex_command: value.codex_command || value.codex_binary || defaults.codex_command } }
function providerIcon(kind: ProviderKind) { return kind === 'remote' ? <Globe2 size={17} /> : <Terminal size={17} /> }
function providersFromSettings(settings: Settings): ManagedProvider[] {
  const catalog = settings.models?.filter(provider => provider.kind === 'remote').length ? settings.models.filter(provider => provider.kind === 'remote') : settings.providers?.filter(provider => provider.kind === 'remote')
  if (catalog?.length) return catalog.map(provider => ({ ...provider, kind: provider.kind as 'remote' }))
  return [
    { id: 'remote', kind: 'remote', name: '云端 OpenAI', endpoint: settings.remote_url || '尚未配置接口地址', model: settings.remote_model || '尚未选择模型', enabled: settings.remote_enabled, detail: settings.remote_enabled ? '已允许发送正文' : '已停用', protocol: 'openai', api_key_set: settings.remote_api_key_set },
  ]
}

function modelProviders(value: ManagedProvider[]) { return value.filter(provider => provider.kind !== 'cli') }

export function ModelSettingsPage({ projectId }: { projectId: number }) {
  const [settings, setSettings] = useState<Settings>(defaults)
  const [providers, setProviders] = useState<ManagedProvider[]>(providersFromSettings(defaults))
  const [tools, setTools] = useState<AiTool[]>([])
  const [selectedProvider, setSelectedProvider] = useState('remote')
  const [providerQuery, setProviderQuery] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [newProvider, setNewProvider] = useState({ name: '', kind: 'remote' as ProviderKind, endpoint: '', model: '', protocol: 'openai' as ProviderProtocol, apiKey: '' })
  const [trialModel, setTrialModel] = useState('')
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([])
  const [probeResults, setProbeResults] = useState<ProbeResult[]>([])
  const [providerAction, setProviderAction] = useState<'detect' | 'models' | 'test-models' | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [busy, setBusy] = useState(false)
  const [testing, setTesting] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [testResult, setTestResult] = useState('')
  const [testProvider, setTestProvider] = useState('remote')
  const base = `/projects/${projectId}`

  useEffect(() => { void api<SettingsResponse>(`${base}/ai/admin-config`).then(value => { const next = normalizeSettings(value); setSettings(next); setProviders(providersFromSettings(next)); setTools(next.tools ?? []); setTestProvider(current => current === 'local' ? 'remote' : current) }).catch(cause => setError((cause as Error).message)) }, [base])
  function change(name: keyof Settings, value: string | boolean) {
    setSettings(previous => ({ ...previous, [name]: value }))
    if (name === 'codex_enabled' || name === 'claude_enabled') {
      const toolId = name === 'codex_enabled' ? 'codex_cli' : 'claude_code'
      setTools(previous => previous.map(tool => tool.id === toolId ? { ...tool, enabled: Boolean(value) } : tool))
    }
    setProviders(previous => previous.map(provider => {
      if (provider.id === 'remote' && name === 'remote_url') return { ...provider, endpoint: String(value) }
      if (provider.id === 'remote' && name === 'remote_model') return { ...provider, model: String(value) }
      if (provider.id === 'remote' && name === 'remote_enabled') return { ...provider, enabled: Boolean(value), detail: value ? '已允许发送正文' : '已停用' }
      if (provider.id === 'codex' && name === 'codex_enabled') return { ...provider, enabled: Boolean(value), detail: value ? '本机命令行' : '已停用' }
      if (provider.id === 'claude' && name === 'claude_enabled') return { ...provider, enabled: Boolean(value), detail: value ? '本机命令行' : '已停用' }
      return provider
    }))
    if (name === 'codex_enabled' && value === false && testProvider === 'codex_cli') setTestProvider('remote')
    if (name === 'claude_enabled' && value === false && testProvider === 'claude_code') setTestProvider('remote')
    if (name === 'remote_enabled' && value === false && testProvider === 'remote') setTestProvider('codex_cli')
  }
  function payload() { return { local_url: '', local_model: '', local_enabled: false, remote_enabled: settings.remote_enabled, remote_url: settings.remote_url.trim(), remote_model: settings.remote_model.trim(), remote_api_key: apiKey, codex_enabled: settings.codex_enabled, codex_command: settings.codex_command.trim() || 'codex', codex_binary: settings.codex_command.trim() || 'codex', codex_model: settings.codex_model.trim(), claude_enabled: settings.claude_enabled, claude_command: settings.claude_command.trim() || 'claude', claude_model: settings.claude_model.trim(), providers: modelProviders(providers), provider: testProvider } }
  async function save(event?: FormEvent) { event?.preventDefault(); setBusy(true); setError(''); setNotice(''); setTestResult(''); try { const value = await api<SettingsResponse>(`${base}/ai/admin-config`, { method: 'PUT', body: JSON.stringify(payload()) }); const next = normalizeSettings(value); setSettings(next); setProviders(providersFromSettings(next)); setTools(next.tools ?? tools); setApiKey(''); setNotice('模型连接与 AI 工具配置已保存并立即生效') } catch (cause) { setError((cause as Error).message) } finally { setBusy(false) } }
  async function testConnection() { setTesting(true); setError(''); setTestResult(''); try { const result = await api<{ message: string; models?: string[] }>(`${base}/ai/admin-config/test`, { method: 'POST', body: JSON.stringify(payload()) }); setTestResult(result.models?.length ? `${result.message} · 已发现 ${result.models.join('、')}` : result.message) } catch (cause) { setError((cause as Error).message) } finally { setTesting(false) } }
  function probePayload(extra: Record<string, unknown> = {}) {
    return { provider_id: editingId ?? '', endpoint: newProvider.endpoint.trim(), protocol: newProvider.protocol, model: trialModel.trim() || newProvider.model.split(',')[0]?.trim() || '', api_key: newProvider.apiKey.trim(), ...extra }
  }
  async function detectProtocol() {
    setProviderAction('detect'); setError(''); setProbeResults([])
    try {
      const result = await api<{ results: ProbeResult[] }>(`${base}/ai/admin-config/detect`, { method: 'POST', body: JSON.stringify(probePayload()) })
      setProbeResults(result.results)
      const supported = result.results.filter(item => item.ok).map(item => item.protocol)
      setNotice(supported.length ? `已检测到协议 ${supported.join('、')}` : '没有协议返回成功 请检查地址 密钥和试用模型')
    } catch (cause) { setError((cause as Error).message) } finally { setProviderAction(null) }
  }
  async function fetchModels() {
    setProviderAction('models'); setError(''); setProbeResults([])
    try {
      const result = await api<{ models: ModelOption[] }>(`${base}/ai/admin-config/models`, { method: 'POST', body: JSON.stringify(probePayload()) })
      setModelOptions(result.models)
      setNotice(result.models.length ? `已获取 ${result.models.length} 个模型 请点击选择` : '供应商没有返回模型列表 请直接填写模型 ID')
    } catch (cause) { setError((cause as Error).message); setModelOptions([]) } finally { setProviderAction(null) }
  }
  async function testSelectedModels() {
    const models = newProvider.model.split(',').map(value => value.trim()).filter(Boolean)
    if (!models.length) { setError('请先填写或选择至少一个模型'); return }
    setProviderAction('test-models'); setError(''); setProbeResults([])
    try {
      const result = await api<{ results: ProbeResult[] }>(`${base}/ai/admin-config/test-models`, { method: 'POST', body: JSON.stringify(probePayload({ models })) })
      setProbeResults(result.results)
      const passed = result.results.filter(item => item.ok).length
      setNotice(`已检测 ${result.results.length} 个模型 ${passed} 个可用`)
    } catch (cause) { setError((cause as Error).message) } finally { setProviderAction(null) }
  }
  function toggleModel(model: string) {
    const current = newProvider.model.split(',').map(value => value.trim()).filter(Boolean)
    const next = current.includes(model) ? current.filter(value => value !== model) : [...current, model]
    setNewProvider(previous => ({ ...previous, model: next.join(', ') }))
  }
  function resetProviderDraft() { setNewProvider({ name: '', kind: 'remote', endpoint: '', model: '', protocol: 'openai', apiKey: '' }); setTrialModel(''); setModelOptions([]); setProbeResults([]) }
  function addProvider(event: FormEvent) { event.preventDefault(); const name = newProvider.name.trim() || (newProvider.kind === 'remote' ? '新远程接口' : '新模型供应商'); const endpoint = newProvider.endpoint.trim() || '尚未配置接口地址'; const model = newProvider.model.trim() || '尚未选择模型'; const apiKeyValue = newProvider.apiKey.trim(); const id = `custom-${Date.now()}`; setProviders(previous => [...previous, { id, kind: newProvider.kind, name, endpoint, model, enabled: true, detail: newProvider.kind === 'remote' ? '待检测连接' : '已启用', protocol: newProvider.protocol, api_key_set: Boolean(apiKeyValue), api_key: apiKeyValue }]); if (newProvider.kind === 'remote') { setSettings(previous => ({ ...previous, remote_enabled: true, remote_url: endpoint, remote_model: model, remote_api_key_set: Boolean(apiKeyValue) })); if (apiKeyValue) setApiKey(apiKeyValue) } setSelectedProvider(id); setAddOpen(false); resetProviderDraft(); setNotice('已添加供应商 点击保存配置后生效') }
  function startEdit(provider: ManagedProvider) {
    if (!provider) return
    setEditingId(provider.id)
    setNewProvider({ name: provider.name, kind: provider.kind, endpoint: provider.endpoint, model: provider.model, protocol: provider.protocol ?? 'openai', apiKey: '' })
    setTrialModel('')
    setModelOptions([])
    setProbeResults([])
    setEditOpen(true)
  }
  function updateProvider(event: FormEvent) {
    event.preventDefault()
    if (!editingId) return
    const name = newProvider.name.trim() || '未命名供应商'
    const endpoint = newProvider.endpoint.trim() || '尚未配置接口地址'
    const model = newProvider.model.trim() || '尚未选择模型'
    setProviders(previous => previous.map(provider => provider.id === editingId
      ? { ...provider, name, kind: newProvider.kind, endpoint, model, protocol: newProvider.protocol, api_key_set: Boolean(newProvider.apiKey.trim()) || provider.api_key_set, api_key: newProvider.apiKey.trim() || provider.api_key }
      : provider))
    if (editingId === 'remote') { setSettings(previous => ({ ...previous, remote_url: endpoint, remote_model: model, ...(newProvider.apiKey.trim() ? { remote_api_key_set: true } : {}) })); if (newProvider.apiKey.trim()) setApiKey(newProvider.apiKey.trim()) }
    setEditOpen(false)
    setEditingId(null)
    setNotice('供应商信息已更新 点击保存配置后持久化')
  }
  function toggleProvider(provider: ManagedProvider) {
    const enabled = !provider.enabled
    setProviders(previous => previous.map(item => item.id === provider.id ? { ...item, enabled, detail: enabled ? (item.kind === 'cli' ? '本机命令行' : '已允许发送正文') : '已停用' } : item))
    if (provider.id === 'remote') setSettings(previous => ({ ...previous, remote_enabled: enabled }))
    if (provider.id === 'codex') setSettings(previous => ({ ...previous, codex_enabled: enabled }))
    if (provider.id === 'claude') setSettings(previous => ({ ...previous, claude_enabled: enabled }))
  }
  function removeProvider(provider: ManagedProvider) {
    if (providers.length <= 1) return
    setProviders(previous => previous.filter(item => item.id !== provider.id))
    if (provider.id === 'remote') {
      setSettings(previous => ({ ...previous, remote_enabled: false, remote_url: '', remote_model: '', remote_api_key_set: false }))
      setApiKey('')
    }
    setSelectedProvider(providers.find(item => item.id !== provider.id)?.id ?? 'remote')
    setEditOpen(false)
    setEditingId(null)
    setNotice('供应商已移除 点击保存配置后生效')
  }
  function copyProvider(provider: ManagedProvider) {
    const copy = { ...provider, id: `custom-${Date.now()}`, name: `${provider.name} 副本`, enabled: false, detail: '草稿配置' }
    setProviders(previous => [...previous, copy])
    setSelectedProvider(copy.id)
    setEditOpen(false)
    setEditingId(null)
    setNotice('已复制供应商 点击保存配置后生效')
  }
  const filteredProviders = useMemo(() => providers.filter(provider => `${provider.name} ${provider.endpoint} ${provider.model}`.toLowerCase().includes(providerQuery.toLowerCase())), [providerQuery, providers])
  const enabledCount = providers.filter(provider => provider.enabled).length
  const selected = providers.find(provider => provider.id === selectedProvider) ?? providers[0]

  return <div className="model-settings-page">
    <div className="model-settings-intro"><div><div className="eyebrow">AI CONTROL PLANE / 模型与工具</div><h2>模型管理</h2><p>统一管理云端接口与 AI 工具</p></div><span className="model-security-badge"><ShieldCheck size={15} />仅全局管理员可见</span></div>
    {error && <div className="ai-alert error" role="alert">{error}</div>}{notice && <div className="ai-alert" role="status"><CheckCircle2 size={16} />{notice}</div>}{testResult && <div className="ai-alert" role="status"><CheckCircle2 size={16} />{testResult}</div>}
    <section className="provider-overview" aria-label="供应商概览"><div className="provider-overview-metric"><span>已启用供应商</span><strong>{enabledCount}<small> / {providers.length}</small></strong><em><CircleDot size={12} />实时</em></div><div className="provider-overview-metric"><span>云端接口</span><strong>{providers.filter(provider => provider.kind === 'remote').length}</strong><small>按需发送正文</small></div><div className="provider-overview-metric"><span>连接状态</span><strong className="metric-ready"><Check size={17} />待检测</strong><small>保存后可测试</small></div></section>
    <section className="provider-directory"><div className="provider-directory-head"><div><h3>模型连接</h3><p>模型连接独立保存 可明确区分云端连接</p></div><div className="provider-directory-actions"><Input aria-label="查找模型连接" value={providerQuery} onChange={event => setProviderQuery(event.target.value)} placeholder="查找连接或模型" /><Button type="button" onClick={() => setAddOpen(true)}><Plus data-icon="inline-start" />添加连接</Button></div></div><div className="provider-list" role="list">{filteredProviders.map((provider, index) => <motion.button type="button" className={`provider-row ${selected?.id === provider.id ? 'is-selected' : ''} ${!provider.enabled ? 'is-muted' : ''}`} key={provider.id} onClick={event => { if ((event.target as HTMLElement).closest('.provider-switch')) { event.stopPropagation(); toggleProvider(provider); return } setSelectedProvider(provider.id); startEdit(provider) }} initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * .045 }}><span className="provider-drag" aria-hidden="true"><GripVertical size={14} /></span><span className={`provider-logo kind-${provider.kind}`}>{providerIcon(provider.kind)}</span><span className="provider-row-copy"><strong>{provider.name}</strong><span>{provider.endpoint} · {provider.model}</span></span><span className="provider-row-meta"><span className="provider-scope scope-remote">云端</span><span className={`provider-status ${provider.enabled ? 'is-on' : ''}`}><i />{provider.enabled ? '运行中' : '已停用'}</span><span className="provider-detail">{provider.detail}</span></span><span className={`provider-switch ${provider.enabled ? 'is-on' : ''}`} aria-label={`${provider.name}${provider.enabled ? '已启用' : '已停用'}`} title="切换启用状态"><i /></span><ChevronRight size={17} className="provider-chevron" /></motion.button>)}{filteredProviders.length === 0 && <div className="provider-empty">没有匹配的模型连接</div>}</div></section>
    <form onSubmit={event => void save(event)} className="model-settings-grid">
      <section className="model-settings-section cli-settings-section"><div className="model-settings-heading"><span className="model-settings-icon"><Cpu size={18} /></span><div><h3>AI 工具</h3><p>Codex CLI 与 Claude Code 独立于模型连接保存</p></div><span className="model-section-count">{tools.filter(tool => tool.enabled).length} 个已启用</span></div><div className="cli-local-note"><ShieldCheck size={14} /><span>AI 工具运行在本机 仅传入当前任务内容 不经过远程模型连接</span></div><div className="cli-provider-grid"><div className={`cli-provider-card ${settings.codex_enabled ? 'is-enabled' : ''}`}><label className="model-toggle"><input type="checkbox" checked={settings.codex_enabled} onChange={event => change('codex_enabled', event.target.checked)} /><span><strong>Codex CLI</strong><small>{settings.codex_enabled ? '已启用 AI 工具' : '当前关闭'}</small></span></label><label>命令<Input aria-label="Codex CLI 命令" value={settings.codex_command} onChange={event => change('codex_command', event.target.value)} placeholder="codex" /></label><label>模型（可选）<Input aria-label="Codex CLI 模型" value={settings.codex_model} onChange={event => change('codex_model', event.target.value)} placeholder="留空使用 CLI 默认模型" /></label></div><div className={`cli-provider-card ${settings.claude_enabled ? 'is-enabled' : ''}`}><label className="model-toggle"><input type="checkbox" checked={settings.claude_enabled} onChange={event => change('claude_enabled', event.target.checked)} /><span><strong>Claude Code</strong><small>{settings.claude_enabled ? '已启用 AI 工具' : '当前关闭'}</small></span></label><label>命令<Input aria-label="Claude Code 命令" value={settings.claude_command} onChange={event => change('claude_command', event.target.value)} placeholder="claude" /></label><label>模型（可选）<Input aria-label="Claude Code 模型" value={settings.claude_model} onChange={event => change('claude_model', event.target.value)} placeholder="留空使用 CLI 默认模型" /></label></div></div><p className="model-field-hint">保存后 新建的需求分析 用例编写和建议任务可选择对应 AI 工具</p></section>
      <footer className="model-settings-footer"><span><ShieldCheck size={15} />修改后立即影响新建 AI 任务 执行中的任务继续使用原配置</span><div><select aria-label="选择测试 provider" value={testProvider} onChange={event => setTestProvider(event.target.value)} disabled={testing || busy}><option value="remote">测试云端模型</option>{settings.remote_enabled && <option value="remote">测试远程模型</option>}{settings.codex_enabled && <option value="codex_cli">测试 Codex CLI</option>}{settings.claude_enabled && <option value="claude_code">测试 Claude Code</option>}</select><Button type="button" variant="outline" onClick={() => void testConnection()} disabled={testing || busy}><FlaskConical data-icon="inline-start" />{testing ? '测试中…' : '测试连接'}</Button><Button type="submit" disabled={busy || testing}><Save data-icon="inline-start" />{busy ? '保存中…' : '保存配置'}</Button></div></footer>
    </form>
    {testing && <div className="model-testing"><LoaderCircle size={16} />正在检查模型服务响应</div>}
    <AnimatePresence>
      {(addOpen || editOpen) && <motion.div className="provider-dialog-backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={event => { if (event.target === event.currentTarget) { setAddOpen(false); setEditOpen(false) } }}>
        <motion.form className="provider-dialog provider-dialog-large" onSubmit={editOpen ? updateProvider : addProvider} initial={{ opacity: 0, y: 12, scale: .98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: .98 }}>
          <div className="provider-dialog-titlebar"><span className="provider-dialog-cube"><Box size={22} /></span><div><h3>{editOpen ? '编辑供应商' : '自定义供应商'}</h3><p>{editOpen ? '修改连接信息后保存配置' : '连接一个兼容的模型服务'}</p></div><Button type="button" variant="ghost" size="icon-sm" aria-label="关闭供应商表单" onClick={() => { setAddOpen(false); setEditOpen(false) }}><X /></Button></div>
          <div className="provider-dialog-body">
            <label>名称<Input aria-label="供应商名称" value={newProvider.name} onChange={event => setNewProvider(previous => ({ ...previous, name: event.target.value }))} placeholder="例如 My Relay" autoFocus /></label>
            <div className="provider-dialog-field"><span>部署位置</span><div className="provider-scope-tabs" role="tablist" aria-label="模型部署位置"><button type="button" role="tab" aria-selected={true} className="is-active"><Globe2 size={14} />云端接口</button></div><small className="provider-dialog-note">用于标识数据是否会离开当前设备</small></div>
            <label>Base URL
              <div className="protocol-tabs" role="tablist" aria-label="接口协议">
                {([['openai', 'OpenAI 兼容'], ['responses', 'OpenAI Responses'], ['anthropic', 'Anthropic 兼容'], ['gemini', 'Gemini 兼容'], ['system-one', 'System One']] as [ProviderProtocol, string][]).map(([value, label]) => <button type="button" role="tab" aria-selected={newProvider.protocol === value} className={newProvider.protocol === value ? 'is-active' : ''} key={value} onClick={() => setNewProvider(previous => ({ ...previous, protocol: value }))}>{label}</button>)}
              </div>
              <Input aria-label="供应商 Base URL" value={newProvider.endpoint} onChange={event => setNewProvider(previous => ({ ...previous, endpoint: event.target.value }))} placeholder="https://.../v1" />
            </label>
            <div className="provider-dialog-actions-inline"><Button type="button" variant="outline" onClick={() => void detectProtocol()} disabled={providerAction !== null}><FlaskConical data-icon="inline-start" />{providerAction === 'detect' ? '检测中…' : '检测协议'}</Button><Input aria-label="试用模型" value={trialModel} onChange={event => setTrialModel(event.target.value)} placeholder="试用的模型 · 留空则从供应商的模型列表中挑选" /><Button type="button" variant="outline" onClick={() => void testSelectedModels()} disabled={providerAction !== null}>{providerAction === 'test-models' ? '检测中…' : '逐个检测已选模型'}</Button></div>
            {probeResults.length > 0 && <div className="provider-probe-results" role="status">{probeResults.map((result, index) => <span className={result.ok ? 'is-ok' : 'is-bad'} key={`${result.protocol}-${result.model ?? index}`}><i />{result.protocol}{result.model ? ` · ${result.model}` : ''}{result.ok ? ' 可用' : ` 不可用${result.status ? ` · ${result.status}` : ''}`}</span>)}</div>}
            <label>API 密钥<div className="model-key-input"><Input aria-label="供应商 API 密钥" type={showKey ? 'text' : 'password'} value={newProvider.apiKey} onChange={event => setNewProvider(previous => ({ ...previous, apiKey: event.target.value }))} placeholder={selected?.api_key_set && editOpen ? '已配置  输入新密钥可替换' : '粘贴 API 密钥'} /><Button type="button" variant="ghost" size="icon-sm" aria-label={showKey ? '隐藏供应商 API 密钥' : '显示供应商 API 密钥'} onClick={() => setShowKey(value => !value)}>{showKey ? <EyeOff /> : <Eye />}</Button></div><small className="provider-dialog-note">保存在服务端加密配置中 不会回显到页面或日志</small></label>
            <div className="provider-dialog-field"><span>图标</span><div className="provider-icon-actions"><button type="button" className="provider-dialog-icon"><Box size={22} /></button><button type="button" onClick={() => setNotice('图片选择功能将在后续版本接入')}>选择图片…</button><button type="button" onClick={() => setNotice('网站图标获取已加入待办')}>从网站获取</button><button type="button" onClick={() => setNotice('已使用内置图标')}>内置图标</button></div></div>
            <div className="provider-dialog-model-row"><label>模型<Input aria-label="供应商模型名称" value={newProvider.model} onChange={event => setNewProvider(previous => ({ ...previous, model: event.target.value }))} placeholder="模型 ID  逗号分隔  例如 gpt-5.5, claude-sonnet-5" /></label><Button type="button" variant="outline" onClick={() => void fetchModels()} disabled={providerAction !== null}>{providerAction === 'models' ? '获取中…' : '获取模型'}</Button></div>{modelOptions.length > 0 && <div className="provider-model-options" role="list" aria-label="可选模型">{modelOptions.map(model => <button type="button" className={newProvider.model.split(',').map(value => value.trim()).includes(model.id) ? 'is-picked' : ''} aria-pressed={newProvider.model.split(',').map(value => value.trim()).includes(model.id)} key={model.id} onClick={() => toggleModel(model.id)}><span>{model.name}</span>{model.name !== model.id && <small>{model.id}</small>}</button>)}</div>}<p className="provider-dialog-note">可选 用「获取模型」从供应商列表中勾选 或直接填写模型 ID 不选则保存后自动获取模型列表</p>
            <details className="provider-dialog-details"><summary>高级</summary><div><label>连接超时（秒）<Input defaultValue="30" /></label><label>并发限制<Input defaultValue="4" /></label></div></details><details className="provider-dialog-details"><summary>更多端点</summary><p>可在保存后继续添加聊天 嵌入和重排序端点</p></details>
          </div>
          <div className="provider-dialog-footer"><div className="provider-dialog-footer-left">{editOpen && selected && <><Button type="button" variant="ghost" className="provider-danger-action" onClick={() => removeProvider(selected)}><Trash2 data-icon="inline-start" />移除</Button><Button type="button" variant="ghost" onClick={() => copyProvider(selected)}><Plus data-icon="inline-start" />复制</Button></>}</div><div className="provider-dialog-footer-right"><Button type="button" variant="ghost" onClick={() => { setAddOpen(false); setEditOpen(false) }}>取消</Button><Button type="submit">{editOpen ? <><Save data-icon="inline-start" />保存</> : <><Plus data-icon="inline-start" />添加</>}</Button></div></div>
        </motion.form>
      </motion.div>}
    </AnimatePresence>
  </div>
}
