import { useEffect, useState } from "react";
import { api, getSession, ModelProvider, ProviderPreset } from "../api";

/**
 * 模型设置页面：管理 AI 模型提供商,支持多厂商接入、密钥配置、连通性测试。
 * 用户可自行添加不同AI厂商（DeepSeek、GLM、Claude、OpenAI等），输入API密钥后测试连通性与延迟。
 * 管理员可创建共享提供商供全员使用，普通用户只能管理自己的私有提供商。
 */
export default function ModelSettings() {
  const [presets, setPresets] = useState<ProviderPreset[]>([]);
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 新增/编辑表单状态
  const [editingId, setEditingId] = useState<string | null>(null);
  const [selectedPreset, setSelectedPreset] = useState<string>("");
  const [formData, setFormData] = useState({
    name: "",
    provider_type: "openai_compatible",
    base_url: "",
    model: "",
    api_key: "",
    shared: false,
    is_default: false,
  });
  const [testing, setTesting] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; latency_ms: number; message: string }>>({});

  const session = getSession();
  const isStaff = session?.role === "admin" || session?.role === "operator";

  // 加载预设和现有提供商
  useEffect(() => {
    loadData();
  }, []);

  async function loadData() {
    setLoading(true);
    setError(null);
    try {
      const [presetsRes, providersRes] = await Promise.all([
        api.providerPresets(),
        api.listProviders(),
      ]);
      setPresets(presetsRes.items);
      setProviders(providersRes.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  // 选择预设后填充表单
  function applyPreset(presetKey: string) {
    const preset = presets.find((p) => p.key === presetKey);
    if (!preset) return;
    setSelectedPreset(presetKey);
    setFormData({
      ...formData,
      name: preset.name,
      provider_type: preset.type,
      base_url: preset.base_url,
      model: preset.default_model,
    });
  }

  // 创建或更新提供商
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      if (editingId) {
        await api.updateProvider(editingId, {
          name: formData.name,
          base_url: formData.base_url,
          model: formData.model,
          api_key: formData.api_key || undefined,
          is_default: formData.is_default,
        });
      } else {
        await api.createProvider(formData);
      }
      await loadData();
      resetForm();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    }
  }

  function startEdit(provider: ModelProvider) {
    setEditingId(provider.id);
    setFormData({
      name: provider.name,
      provider_type: provider.provider_type,
      base_url: provider.base_url,
      model: provider.model,
      api_key: "",
      shared: provider.owner_user_id === null,
      is_default: provider.is_default,
    });
    setSelectedPreset("custom");
  }

  function resetForm() {
    setEditingId(null);
    setSelectedPreset("");
    setFormData({
      name: "",
      provider_type: "openai_compatible",
      base_url: "",
      model: "",
      api_key: "",
      shared: false,
      is_default: false,
    });
  }

  async function handleDelete(id: string) {
    if (!confirm("确认删除此提供商？")) return;
    setError(null);
    try {
      await api.deleteProvider(id);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    }
  }

  async function handleTest(id: string) {
    setTesting(id);
    setError(null);
    try {
      const result = await api.testProvider(id);
      setTestResults({ ...testResults, [id]: result });
    } catch (err) {
      setError(err instanceof Error ? err.message : "测试失败");
    } finally {
      setTesting(null);
    }
  }

  async function handleToggleEnabled(id: string, enabled: boolean) {
    setError(null);
    try {
      await api.updateProvider(id, { enabled: !enabled });
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "更新失败");
    }
  }

  if (loading) return <div className="container mx-auto p-6">加载中...</div>;

  return (
    <div className="container mx-auto p-6 max-w-5xl">
      <h1 className="text-3xl font-bold mb-6">模型设置</h1>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded mb-4">
          {error}
        </div>
      )}
      <div className="bg-white shadow rounded-lg p-6 mb-8">
        <h2 className="text-xl font-semibold mb-4">
          {editingId ? "编辑提供商" : "添加新提供商"}
        </h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          {!editingId && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">选择预设</label>
              <select
                className="w-full border border-gray-300 rounded-md px-3 py-2"
                value={selectedPreset}
                onChange={(e) => applyPreset(e.target.value)}
              >
                <option value="">-- 选择预设厂商 --</option>
                {presets.map((p) => (
                  <option key={p.key} value={p.key}>{p.name}</option>
                ))}
              </select>
            </div>
          )}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">名称</label>
            <input type="text" className="w-full border border-gray-300 rounded-md px-3 py-2" value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })} required />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Base URL</label>
            <input type="url" className="w-full border border-gray-300 rounded-md px-3 py-2" value={formData.base_url} onChange={(e) => setFormData({ ...formData, base_url: e.target.value })} required />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">模型名称</label>
            <input type="text" className="w-full border border-gray-300 rounded-md px-3 py-2" value={formData.model} onChange={(e) => setFormData({ ...formData, model: e.target.value })} required />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">API Key {editingId && "(留空表示不更新)"}</label>
            <input type="password" className="w-full border border-gray-300 rounded-md px-3 py-2" value={formData.api_key} onChange={(e) => setFormData({ ...formData, api_key: e.target.value })} placeholder={editingId ? "留空不更新密钥" : ""} required={!editingId} />
          </div>
          {isStaff && (
            <>
              <div className="flex items-center">
                <input type="checkbox" id="shared" className="h-4 w-4 rounded border-gray-300" checked={formData.shared} onChange={(e) => setFormData({ ...formData, shared: e.target.checked })} disabled={!!editingId} />
                <label htmlFor="shared" className="ml-2 text-sm text-gray-700">全局共享（所有用户可见）</label>
              </div>
              {formData.shared && (
                <div className="flex items-center">
                  <input type="checkbox" id="is_default" className="h-4 w-4 rounded border-gray-300" checked={formData.is_default} onChange={(e) => setFormData({ ...formData, is_default: e.target.checked })} />
                  <label htmlFor="is_default" className="ml-2 text-sm text-gray-700">设为系统默认</label>
                </div>
              )}
            </>
          )}
          <div className="flex gap-3">
            <button type="submit" className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700">{editingId ? "保存" : "添加"}</button>
            {editingId && (
              <button type="button" onClick={resetForm} className="bg-gray-300 text-gray-700 px-4 py-2 rounded-md hover:bg-gray-400">取消</button>
            )}
          </div>
        </form>
      </div>
      <div className="bg-white shadow rounded-lg p-6">
        <h2 className="text-xl font-semibold mb-4">已配置的提供商</h2>
        {providers.length === 0 ? (
          <p className="text-gray-500">暂无提供商</p>
        ) : (
          <div className="space-y-4">
            {providers.map((provider) => {
              const testResult = testResults[provider.id];
              return (
                <div key={provider.id} className="border border-gray-200 rounded-lg p-4 hover:shadow-md transition">
                  <div className="flex justify-between items-start mb-2">
                    <div>
                      <h3 className="font-semibold text-lg">
                        {provider.name}
                        {provider.is_default && (<span className="ml-2 text-xs bg-green-100 text-green-700 px-2 py-1 rounded">默认</span>)}
                        {!provider.enabled && (<span className="ml-2 text-xs bg-gray-100 text-gray-600 px-2 py-1 rounded">已停用</span>)}
                        {provider.owner_user_id === null && (<span className="ml-2 text-xs bg-blue-100 text-blue-700 px-2 py-1 rounded">共享</span>)}
                      </h3>
                      <p className="text-sm text-gray-600 mt-1">模型: {provider.model} | 类型: {provider.provider_type}</p>
                      <p className="text-sm text-gray-500 mt-1">Base URL: {provider.base_url}</p>
                      <p className="text-sm text-gray-500">密钥: {provider.has_key ? provider.key_masked : "未配置"}</p>
                    </div>
                  </div>
                  {testResult && (
                    <div className={`mt-2 p-2 rounded text-sm ${testResult.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"}`}>
                      {testResult.ok ? `✓ 连接成功 (延迟: ${testResult.latency_ms}ms)` : `✗ ${testResult.message}`}
                    </div>
                  )}
                  <div className="mt-4 flex gap-2">
                    <button onClick={() => handleTest(provider.id)} disabled={testing === provider.id || !provider.has_key} className="text-sm bg-blue-100 text-blue-700 px-3 py-1 rounded hover:bg-blue-200 disabled:opacity-50">{testing === provider.id ? "测试中..." : "测试连通"}</button>
                    <button onClick={() => startEdit(provider)} className="text-sm bg-gray-100 text-gray-700 px-3 py-1 rounded hover:bg-gray-200">编辑</button>
                    <button onClick={() => handleToggleEnabled(provider.id, provider.enabled)} className="text-sm bg-gray-100 text-gray-700 px-3 py-1 rounded hover:bg-gray-200">{provider.enabled ? "停用" : "启用"}</button>
                    {(provider.owner_user_id === session?.user_id || isStaff) && !provider.is_default && (
                      <button onClick={() => handleDelete(provider.id)} className="text-sm bg-red-100 text-red-700 px-3 py-1 rounded hover:bg-red-200">删除</button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
