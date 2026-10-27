/** 桥接服务 API 客户端（REST + WS） */

const runtime = (window as {
  meoclaw?: { bridgePort?: number; bridgeToken?: string };
}).meoclaw;
const PORT = runtime?.bridgePort ?? 8931;
const TOKEN = runtime?.bridgeToken ?? "";
const BASE = `http://127.0.0.1:${PORT}`;

export interface Session {
  id: string;
  title: string;
  permission: string;
  archived: boolean;
  created_at: number;
}

export interface MsgRow {
  id: number;
  session_id: string;
  role: string;
  content: string;
  meta: string;
  created_at: number;
}

export interface Provider {
  name: string;
  model: string;
  base_url: string;
  has_key?: boolean;
}

export interface Settings {
  temperature: number;
  max_attempts: number;
  permission: string;
  providers: Provider[];
  active_provider: string;
  sidebar_open: boolean;
}

export interface TavilyStatus {
  ok: boolean;
  configured: boolean;
  source: "env" | "keyring" | "";
}

async function j<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(BASE + path, {
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${TOKEN}`,
    },
    ...opts,
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

export const api = {
  sessions: () => j<Session[]>("/api/sessions"),
  messages: (sid: string) => j<MsgRow[]>(`/api/sessions/${sid}/messages`),
  archiveSession: (sid: string, archived: boolean) =>
    j<{ ok: boolean; message?: string }>(`/api/sessions/${sid}/archive`, {
      method: "POST",
      body: JSON.stringify({ archived }),
    }),
  settings: () => j<Settings>("/api/settings"),
  patchSettings: (patch: Partial<Settings>) =>
    j("/api/settings", { method: "POST", body: JSON.stringify(patch) }),
  upsertProvider: (p: { name: string; model: string; base_url: string; api_key?: string }) =>
    j<{ ok: boolean; message?: string }>("/api/providers", { method: "POST", body: JSON.stringify(p) }),
  deleteProvider: (name: string) =>
    j(`/api/providers/${encodeURIComponent(name)}`, { method: "DELETE" }),
  setActive: (name: string) =>
    j("/api/providers/active", { method: "POST", body: JSON.stringify({ name }) }),
  testConnection: (p: { name: string; model: string; base_url: string; api_key?: string }) =>
    j<{ ok: boolean; message: string }>("/api/test-connection", { method: "POST", body: JSON.stringify(p) }),
  tavilyStatus: () => j<TavilyStatus>("/api/tavily"),
  configureTavily: (api_key: string) =>
    j<{ ok: boolean; message: string }>("/api/tavily", {
      method: "POST",
      body: JSON.stringify({ api_key }),
    }),
  removeTavily: () =>
    j<{ ok: boolean; message: string }>("/api/tavily", { method: "DELETE" }),
  startTask: (body: {
    task: string;
    permission: string;
    session_id?: string | null;
  }) =>
    j<{ ok: boolean; message?: string; task_id?: string; session_id?: string }>(
      "/api/task",
      { method: "POST", body: JSON.stringify(body) }
    ),
  resolveApproval: (id: string, decision: boolean) =>
    j(`/api/approval/${id}`, { method: "POST", body: JSON.stringify({ decision }) }),
};

export function taskSocket(taskId: string): WebSocket {
  return new WebSocket(
    `ws://127.0.0.1:${PORT}/ws/task/${taskId}?token=${encodeURIComponent(TOKEN)}`
  );
}
