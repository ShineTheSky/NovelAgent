import { useState } from 'react';
import { createPortal } from 'react-dom';
import { getLLMSettings, saveLLMSettings, saveProviderSettings } from '../api/client';
import type { LLMPositionSetting, LLMSettings } from '../types/llm';

type SettingsTab = 'providers' | 'agents';

interface ProviderDraft {
  baseUrl: string;
  apiKey: string;
  modelsText: string;
  storage: 'persistent' | 'temporary';
}

function parseModelNames(value: string) {
  return value.split(/[\n,]/).map(model => model.trim()).filter(Boolean);
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : '操作失败，请稍后重试';
}

export function LLMSettingsPanel() {
  const [open, setOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<SettingsTab>('providers');
  const [settings, setSettings] = useState<LLMSettings | null>(null);
  const [providerDrafts, setProviderDrafts] = useState<Record<string, ProviderDraft>>({});
  const [loading, setLoading] = useState(false);
  const [savingAgentRouting, setSavingAgentRouting] = useState(false);
  const [savingProvider, setSavingProvider] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const loadSettings = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getLLMSettings();
      setSettings(data);
      setProviderDrafts(Object.fromEntries(data.providers.map(provider => [provider.key, {
        baseUrl: provider.base_url,
        apiKey: '',
        modelsText: provider.models.map(model => model.model).join('\n'),
        storage: provider.has_temporary_key ? 'temporary' : 'persistent',
      }])));
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setLoading(false);
    }
  };

  const showPanel = () => {
    setOpen(true);
    setActiveTab('providers');
    setMessage(null);
    void loadSettings();
  };

  const updatePosition = (key: string, update: Partial<LLMPositionSetting>) => {
    setSettings(current => current && {
      ...current,
      positions: {
        ...current.positions,
        [key]: { ...current.positions[key], ...update },
      },
    });
  };

  const updateProviderDraft = (key: string, update: Partial<ProviderDraft>) => {
    setProviderDrafts(current => ({
      ...current,
      [key]: { ...current[key], ...update },
    }));
  };

  const saveProvider = async (key: string) => {
    const draft = providerDrafts[key];
    if (!draft) return;
    setSavingProvider(key);
    setError(null);
    setMessage(null);
    try {
      const apiKey = draft.apiKey.trim();
      const result = await saveProviderSettings({
        [key]: { base_url: draft.baseUrl, models: parseModelNames(draft.modelsText), storage: draft.storage, ...(apiKey ? { api_key: apiKey } : {}) },
      });
      setMessage(result.message);
      await loadSettings();
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setSavingProvider(null);
    }
  };

  const saveAgentRouting = async () => {
    if (!settings) return;
    setSavingAgentRouting(true);
    setError(null);
    setMessage(null);
    try {
      const positions = Object.fromEntries(Object.entries(settings.positions).map(([key, value]) => [key, {
        provider: value.provider,
        model: value.model,
      }]));
      const result = await saveLLMSettings(positions);
      setMessage(result.message);
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setSavingAgentRouting(false);
    }
  };

  return (
    <>
      <button type="button" onClick={showPanel} className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 shadow-sm hover:border-purple-200 hover:text-purple-600 transition-colors" aria-label="打开模型设置">模型设置</button>
      {open && createPortal(
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="llm-settings-title">
          <div className="relative w-full max-w-2xl max-h-[85vh] overflow-y-auto rounded-2xl bg-white p-6 pr-14 shadow-2xl mx-4">
            <div className="mb-5">
              <div>
                <h2 id="llm-settings-title" className="font-semibold text-gray-800">模型设置</h2>
                <p className="mt-1 text-xs text-gray-400">先配置公司 API，再为不同 Agent 选择对应的 provider 和模型。</p>
              </div>
            </div>
            <button type="button" onClick={() => setOpen(false)} aria-label="关闭模型设置" className="absolute right-4 top-4 flex h-8 w-8 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-700">✕</button>

            <div className="mb-4 flex gap-1 rounded-xl bg-gray-100 p-1" role="tablist" aria-label="模型设置分类">
              <button type="button" role="tab" aria-selected={activeTab === 'providers'} onClick={() => setActiveTab('providers')} className={`flex-1 rounded-lg px-3 py-2 text-sm transition-colors ${activeTab === 'providers' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>1. 公司 API</button>
              <button type="button" role="tab" aria-selected={activeTab === 'agents'} onClick={() => setActiveTab('agents')} className={`flex-1 rounded-lg px-3 py-2 text-sm transition-colors ${activeTab === 'agents' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>2. Agent 模型路由</button>
            </div>

            {loading && <div className="py-12 text-center text-sm text-gray-400">正在加载设置…</div>}
            {!loading && error && <div role="alert" className="mb-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</div>}
            {!loading && message && <div role="status" className="mb-3 rounded-lg border border-green-100 bg-green-50 px-3 py-2 text-sm text-green-700">{message}</div>}

            {!loading && settings && activeTab === 'providers' && (
              <div className="space-y-3" role="tabpanel">
                <p className="text-xs leading-relaxed text-gray-500">在这里一起配置 API 地址、密钥和该 API 可调用的模型。模型名会用于下一个“Agent 模型路由”页；页面不会回显已保存的密钥。</p>
                {settings.providers.map(provider => {
                  const draft = providerDrafts[provider.key] ?? { baseUrl: provider.base_url, apiKey: '', modelsText: provider.models.map(model => model.model).join('\n'), storage: provider.has_temporary_key ? 'temporary' : 'persistent' };
                  return (
                    <div key={provider.key} className="rounded-xl border border-gray-100 bg-gray-50 p-3">
                      <div className="mb-2 flex items-center justify-between gap-3">
                        <span className="text-sm font-medium text-gray-700">{provider.name}</span>
                        <span className={`rounded-full px-2 py-0.5 text-[11px] ${provider.has_temporary_key || provider.is_configured ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700'}`}>{provider.has_temporary_key ? '临时 Key 已配置' : provider.is_configured ? '本机 Key 已配置' : '尚未配置 Key'}</span>
                      </div>
                      <div className="grid gap-2 sm:grid-cols-[1fr_1fr_10rem_auto]">
                        <label className="text-xs text-gray-500">
                          API 地址
                          <input type="url" value={draft.baseUrl} onChange={event => updateProviderDraft(provider.key, { baseUrl: event.target.value })} className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 outline-none focus:border-purple-400" />
                        </label>
                        <label className="text-xs text-gray-500">
                          API Key
                          <input type="password" autoComplete="new-password" value={draft.apiKey} onChange={event => updateProviderDraft(provider.key, { apiKey: event.target.value })} placeholder={provider.has_temporary_key ? '临时 Key 已配置；输入以替换' : provider.is_configured ? '已配置；留空则保留' : '输入 API Key'} className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 outline-none focus:border-purple-400" />
                        </label>
                        <label className="text-xs text-gray-500">
                          保存方式
                          <select value={draft.storage} onChange={event => updateProviderDraft(provider.key, { storage: event.target.value as ProviderDraft['storage'] })} className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 outline-none focus:border-purple-400">
                            <option value="temporary">仅本次服务运行</option>
                            <option value="persistent">保存到本机</option>
                          </select>
                        </label>
                        <button type="button" onClick={() => void saveProvider(provider.key)} disabled={savingProvider !== null} className="self-end rounded-lg bg-purple-600 px-3 py-1.5 text-sm text-white hover:bg-purple-700 disabled:opacity-50">{savingProvider === provider.key ? '保存中…' : '保存'}</button>
                      </div>
                      <label className="mt-3 block text-xs text-gray-500">
                        可用模型
                        <textarea value={draft.modelsText} onChange={event => updateProviderDraft(provider.key, { modelsText: event.target.value })} rows={2} placeholder={'每行一个模型名，例如：\ngpt-5.6-sol'} className="mt-1 w-full resize-y rounded-lg border border-gray-200 bg-white px-2 py-1.5 text-sm leading-5 text-gray-700 outline-none focus:border-purple-400" />
                        <span className="mt-1 block text-[11px] text-gray-400">每行一个模型名，也可用英文逗号分隔。至少配置一个模型。</span>
                      </label>
                    </div>
                  );
                })}
              </div>
            )}

            {!loading && settings && activeTab === 'agents' && (
              <div className="space-y-3" role="tabpanel">
                <p className="text-xs leading-relaxed text-gray-500">仅显示已在“公司 API”中配置模型名的 Provider。修改模型列表后，先保存公司 API，再切换路由。</p>
                {Object.entries(settings.positions).map(([key, position]) => {
                  const selectableProviders = settings.providers.filter(provider => provider.models.length > 0);
                  const provider = selectableProviders.find(item => item.key === position.provider);
                  return (
                    <div key={key} className="rounded-xl border border-gray-100 bg-gray-50 p-3">
                      <div className="mb-2 text-sm font-medium text-gray-700">{position.label}</div>
                      <div className="grid grid-cols-2 gap-2">
                        <label className="text-xs text-gray-500">
                          公司 API
                          <select value={position.provider} onChange={event => {
                            const nextProvider = selectableProviders.find(item => item.key === event.target.value);
                            updatePosition(key, { provider: event.target.value, model: nextProvider?.models[0]?.model ?? '' });
                          }} className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 outline-none focus:border-purple-400">
                            {selectableProviders.map(item => <option key={item.key} value={item.key}>{item.name}</option>)}
                          </select>
                        </label>
                        <label className="text-xs text-gray-500">
                          模型
                          <select value={position.model} onChange={event => updatePosition(key, { model: event.target.value })} className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-700 outline-none focus:border-purple-400">
                            {provider?.models.map(item => <option key={item.model} value={item.model}>{item.label}</option>)}
                          </select>
                        </label>
                      </div>
                    </div>
                  );
                })}
                <div className="flex justify-end gap-2 pt-2">
                  <button type="button" onClick={() => setOpen(false)} disabled={savingAgentRouting} className="rounded-xl border border-gray-200 px-4 py-2 text-sm text-gray-600 hover:bg-gray-50">关闭</button>
                  <button type="button" onClick={() => void saveAgentRouting()} disabled={savingAgentRouting} className="rounded-xl bg-purple-600 px-4 py-2 text-sm font-medium text-white hover:bg-purple-700 disabled:opacity-50">{savingAgentRouting ? '保存中…' : '保存 Agent 路由'}</button>
                </div>
              </div>
            )}
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
