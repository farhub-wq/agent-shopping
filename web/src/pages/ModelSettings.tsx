﻿import { useEffect, useState } from "react";
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

  return (
    <div>
      <div className="page-head">
        <div>
          <p className="eyebrow">AI Providers</p>
          <h1>模型设置</h1>
          <p className="muted">
            接入 DeepSeek、GLM、Claude、OpenAI 等厂商，填写密钥后即可测试连通性与延迟。
            {isStaff && " 管理员创建的共享提供商会出现在所有用户的列表中。"}
          </p>
        </div>
        {!loading && <span className="chip">{providers.length} 个提供商</span>}
      </div>

      {error && <p className="error">{error}</p>}

      <div className="settings-layout">
        <div className="panel">
          <h2>{editingId ? "编辑提供商" : "添加提供商"}</h2>
          <form onSubmit={handleSubmit}>
            {/* 预设只用于快速填充字段，选中后仍可手工修改 */}
            {!editingId && (
              <div className="preset-row">
                <span className="hint">快速选择预设厂商</span>
                <div className="chips">
                  {presets.map((preset) => (
                    <button
                      key={preset.key}
                      type="button"
                      className={selectedPreset === preset.key ? "chip active" : "chip"}
                      onClick={() => applyPreset(preset.key)}
                    >
                      {preset.name}
                    </button>
                  ))}
                  {!presets.length && <span className="hint">暂无预设，可直接手工填写。</span>}
                </div>
              </div>
            )}

            <div className="form-grid">
              <label className="span-2">
                名称
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder="例如 我的 DeepSeek"
                  required
                />
              </label>
              <label className="span-2">
                Base URL
                <input
                  type="url"
                  value={formData.base_url}
                  onChange={(e) => setFormData({ ...formData, base_url: e.target.value })}
                  placeholder="https://api.deepseek.com/v1"
                  required
                />
              </label>
              <label>
                模型名称
                <input
                  type="text"
                  value={formData.model}
                  onChange={(e) => setFormData({ ...formData, model: e.target.value })}
                  placeholder="deepseek-chat"
                  required
                />
              </label>
              <label>
                API Key{editingId ? "（留空不更新）" : ""}
                <input
                  type="password"
                  value={formData.api_key}
                  onChange={(e) => setFormData({ ...formData, api_key: e.target.value })}
                  placeholder={editingId ? "留空沿用旧密钥" : "sk-..."}
                  required={!editingId}
                />
              </label>
            </div>

            {isStaff && (
              <div>
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={formData.shared}
                    onChange={(e) => setFormData({ ...formData, shared: e.target.checked })}
                    disabled={!!editingId}
                  />
                  全局共享（所有用户可见）
                </label>
                {formData.shared && (
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={formData.is_default}
                      onChange={(e) => setFormData({ ...formData, is_default: e.target.checked })}
                    />
                    设为系统默认
                  </label>
                )}
              </div>
            )}

            <div className="form-actions">
              <button type="submit" className="primary small">{editingId ? "保存修改" : "添加提供商"}</button>
              {editingId && (
                <button type="button" className="ghost" onClick={resetForm}>取消</button>
              )}
            </div>
          </form>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h2>已配置的提供商</h2>
            <span className="hint">每个用户可用自己的密钥，互不影响</span>
          </div>

          {loading ? (
            <p className="muted center">加载中…</p>
          ) : providers.length === 0 ? (
            <p className="muted center">还没有提供商，先在左侧添加一个。</p>
          ) : (
            <div>
              {providers.map((provider) => {
                const testResult = testResults[provider.id];
                return (
                  <article className="provider-card" key={provider.id}>
                    <div className="provider-head">
                      <div>
                        <h3>
                          {provider.name}
                          {provider.is_default && <span className="status ok">默认</span>}
                          {!provider.enabled && <span className="status">已停用</span>}
                          {provider.owner_user_id === null && <span className="status">共享</span>}
                        </h3>
                        <div className="provider-meta">
                          <span>模型 {provider.model} · 类型 {provider.provider_type}</span>
                          <span>Base URL：{provider.base_url}</span>
                          <span>密钥：{provider.has_key ? provider.key_masked : "未配置"}</span>
                        </div>
                      </div>
                    </div>

                    {testResult && (
                      testResult.ok ? (
                        <p className="advice">连接成功，延迟 {testResult.latency_ms}ms</p>
                      ) : (
                        <p className="error">连接失败：{testResult.message}</p>
                      )
                    )}

                    <div className="row-actions">
                      <button
                        className="ghost small"
                        onClick={() => handleTest(provider.id)}
                        disabled={testing === provider.id || !provider.has_key}
                      >
                        {testing === provider.id ? "测试中…" : "测试连通"}
                      </button>
                      <button className="ghost small" onClick={() => startEdit(provider)}>编辑</button>
                      <button
                        className="ghost small"
                        onClick={() => handleToggleEnabled(provider.id, provider.enabled)}
                      >
                        {provider.enabled ? "停用" : "启用"}
                      </button>
                      {(provider.owner_user_id === session?.user_id || isStaff) && !provider.is_default && (
                        <button className="ghost small danger" onClick={() => handleDelete(provider.id)}>删除</button>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
