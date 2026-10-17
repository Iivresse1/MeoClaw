import { useEffect, useState } from "react";
import { api } from "../api";
import type { Provider, Settings } from "../api";

interface Form {
  name: string;
  model: string;
  base_url: string;
  api_key: string;
}

const EMPTY_FORM: Form = { name: "", model: "", base_url: "", api_key: "" };

export default function SettingsModal({ onClose }: { onClose: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [form, setForm] = useState<Form>(EMPTY_FORM);
  const [editing, setEditing] = useState<string | null>(null); // 正在编辑的供应商名
  const [msg, setMsg] = useState("");
  const [testing, setTesting] = useState(false);

  const refresh = () => api.settings().then(setSettings).catch(() => {});
  useEffect(() => {
    refresh();
  }, []);

  async function saveProvider() {
    if (!form.name.trim()) {
      setMsg("供应商名称不能为空");
      return;
    }
    const r = await api
      .upsertProvider({
        name: form.name.trim(),
        model: form.model.trim(),
        base_url: form.base_url.trim(),
        api_key: form.api_key || undefined,
      })
      .catch((e) => ({ ok: false, message: String(e) }));
    setMsg(r.ok ? "" : r.message || "保存失败");
    if (r.ok) {
      setForm(EMPTY_FORM);
      setEditing(null);
      refresh();
    }
  }

  async function testProvider(p: Form) {
    setTesting(true);
    setMsg("");
    const r = await api
      .testConnection({
        name: p.name,
        model: p.model,
        base_url: p.base_url,
        api_key: p.api_key || undefined,
      })
      .catch(() => ({ ok: false, message: "请求失败" }));
    setMsg(r.message);
    setTesting(false);
  }

  function editProvider(p: Provider) {
    setEditing(p.name);
    setForm({ name: p.name, model: p.model || "", base_url: p.base_url || "", api_key: "" });
  }

  async function saveParams() {
    if (!settings) return;
    await api
      .patchSettings({ temperature: settings.temperature, max_attempts: settings.max_attempts } as never)
      .catch(() => {});
    setMsg("运行参数已保存");
    refresh();
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>设置</span>
          <button className="close-x" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">
          <h3>模型供应商</h3>
          <div className="provider-list">
            {settings?.providers.map((p) => (
              <div key={p.name} className={"provider-row" + (p.name === settings.active_provider ? " active" : "")}>
                <div className="provider-info">
                  <b>{p.name}</b>
                  <span className="muted">
                    {p.model || "未填模型"} · {p.base_url || "默认地址"} · {p.has_key ? "密钥已存" : "缺少密钥"}
                  </span>
                </div>
                <div className="provider-actions">
                  {p.name !== settings.active_provider && (
                    <button
                      className="btn-ghost sm"
                      onClick={() => api.setActive(p.name).then(refresh).catch(() => {})}
                    >
                      设为当前
                    </button>
                  )}
                  <button className="btn-ghost sm" onClick={() => editProvider(p)}>
                    编辑
                  </button>
                  <button
                    className="btn-ghost sm"
                    onClick={() => api.deleteProvider(p.name).then(refresh).catch(() => {})}
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
            {settings && settings.providers.length === 0 && (
              <div className="muted">尚未添加供应商，请在下方填写并保存</div>
            )}
          </div>

          <div className="provider-form">
            <input
              placeholder="名称（如 DeepSeek）"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
            <input
              placeholder="模型名（如 deepseek-chat）"
              value={form.model}
              onChange={(e) => setForm({ ...form, model: e.target.value })}
            />
            <input
              placeholder="Base URL（如 https://api.deepseek.com/v1）"
              value={form.base_url}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            />
            <input
              type="password"
              placeholder={editing ? "API Key（留空保持不变）" : "API Key"}
              value={form.api_key}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            />
            <div className="form-actions">
              <button className="btn-dark" onClick={saveProvider}>
                {editing ? "保存修改" : "添加供应商"}
              </button>
              <button className="btn-ghost" onClick={() => testProvider(form)} disabled={testing}>
                {testing ? "测试中…" : "测试连接"}
              </button>
              {editing && (
                <button
                  className="btn-ghost"
                  onClick={() => {
                    setEditing(null);
                    setForm(EMPTY_FORM);
                  }}
                >
                  取消
                </button>
              )}
            </div>
            {msg && <div className="form-msg">{msg}</div>}
          </div>

          <h3>运行参数</h3>
          {settings && (
            <div className="params">
              <label>
                temperature
                <input
                  type="number"
                  step="0.1"
                  min="0"
                  max="2"
                  value={settings.temperature}
                  onChange={(e) => setSettings({ ...settings, temperature: Number(e.target.value) })}
                />
              </label>
              <label>
                最大重试次数
                <input
                  type="number"
                  step="1"
                  min="1"
                  max="9"
                  value={settings.max_attempts}
                  onChange={(e) => setSettings({ ...settings, max_attempts: Number(e.target.value) })}
                />
              </label>
              <button className="btn-ghost" onClick={saveParams}>
                保存参数
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
