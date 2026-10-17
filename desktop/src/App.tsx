import { useCallback, useEffect, useRef, useState } from "react";
import { api, taskSocket } from "./api";
import type { MsgRow, Session } from "./api";
import Sidebar from "./components/Sidebar";
import ChatView from "./components/ChatView";
import SettingsModal from "./components/SettingsModal";

export interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}

export interface Step {
  kind: string;
  text: string;
}

export interface PendingApproval {
  id: string;
  tool: string;
  detail: string;
}

export const MODE_LABELS: Record<string, string> = {
  chat: "仅对话",
  workspace: "在工作区内修改",
  system: "系统工具（需批准）",
};

interface TaskEvent {
  type: string;
  content?: string;
  node?: string;
  name?: string;
  tool?: string;
  id?: string;
  detail?: string;
  message?: string;
  command?: string;
  plan?: string;
}

function truncate(s: string, n = 90): string {
  s = (s || "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n) + "…" : s;
}

export default function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sid, setSid] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [steps, setSteps] = useState<Step[]>([]);
  const [stream, setStream] = useState("");
  const [running, setRunning] = useState(false);
  const [approval, setApproval] = useState<PendingApproval | null>(null);
  const [error, setError] = useState("");
  const [mode, setMode] = useState("workspace");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const streamRef = useRef("");

  const refreshSessions = useCallback(
    () => api.sessions().then(setSessions).catch(() => {}),
    []
  );

  const toggleSidebar = useCallback((v: boolean) => {
    setSidebarOpen(v);
    api.patchSettings({ sidebar_open: v } as never).catch(() => {});
  }, []);

  useEffect(() => {
    refreshSessions();
    api
      .settings()
      .then((s) => {
        if (s.permission && MODE_LABELS[s.permission]) setMode(s.permission);
        setSidebarOpen(s.sidebar_open !== false);
      })
      .catch(() => {});
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        setSidebarOpen((v) => {
          const n = !v;
          api.patchSettings({ sidebar_open: n } as never).catch(() => {});
          return n;
        });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [refreshSessions]);

  async function selectSession(s: Session) {
    if (running) return;
    setSid(s.id);
    setSteps([]);
    setStream("");
    setError("");
    try {
      const rows: MsgRow[] = await api.messages(s.id);
      setMessages(
        rows
          .filter((r) => r.role === "user" || r.role === "assistant")
          .map((r) => ({ role: r.role as ChatMsg["role"], content: r.content }))
      );
    } catch {
      setMessages([]);
    }
  }

  function newChat() {
    if (running) return;
    setSid(null);
    setMessages([]);
    setSteps([]);
    setStream("");
    setError("");
  }

  function finishRun() {
    const text = streamRef.current;
    if (text) {
      setMessages((m) => [...m, { role: "assistant", content: text }]);
    }
    streamRef.current = "";
    setStream("");
    setRunning(false);
    refreshSessions();
  }

  function handleEvent(ev: TaskEvent) {
    switch (ev.type) {
      case "ai_token": {
        streamRef.current += ev.content || "";
        setStream(streamRef.current);
        break;
      }
      case "node_start":
        setSteps((s) => [...s, { kind: "节点", text: ev.node || ev.name || "" }]);
        break;
      case "plan":
        setSteps((s) => [...s, { kind: "计划", text: truncate(ev.content || ev.plan || "") }]);
        break;
      case "tool_call":
        setSteps((s) => [...s, { kind: "工具", text: ev.name || ev.tool || "" }]);
        break;
      case "tool_result":
        setSteps((s) => [...s, { kind: "结果", text: truncate(ev.content || "") }]);
        break;
      case "verification_command":
        setSteps((s) => [...s, { kind: "校验命令", text: truncate(ev.command || ev.content || "") }]);
        break;
      case "verification":
        setSteps((s) => [...s, { kind: "校验", text: truncate(ev.content || "") }]);
        break;
      case "react_done":
        setSteps((s) => [...s, { kind: "回合完成", text: truncate(ev.content || "") }]);
        break;
      case "final_answer": {
        const c = ev.content || "";
        if (c) {
          streamRef.current = c;
          setStream(c);
        }
        break;
      }
      case "workflow_end":
        setSteps((s) => [...s, { kind: "结束", text: "" }]);
        break;
      case "approval_request":
        if (ev.id) setApproval({ id: ev.id, tool: ev.tool || "", detail: ev.detail || "" });
        break;
      case "error":
        setError(ev.message || "运行出错");
        break;
      case "ui_done":
        finishRun();
        break;
    }
  }

  function send(text: string) {
    const task = text.trim();
    if (!task || running) return;
    setError("");
    setSteps([]);
    setStream("");
    streamRef.current = "";
    setMessages((m) => [...m, { role: "user", content: task }]);
    setRunning(true);
    api
      .startTask({ task, mode, session_id: sid })
      .then((r) => {
        if (!r.ok || !r.task_id) {
          setError(r.message || "任务启动失败");
          setRunning(false);
          return;
        }
        if (!sid) setSid(r.session_id || null);
        refreshSessions();
        const ws = taskSocket(r.task_id);
        ws.onmessage = (e) => {
          try {
            handleEvent(JSON.parse(e.data) as TaskEvent);
          } catch {
            /* 忽略坏帧 */
          }
        };
        ws.onclose = () => setRunning(false);
        ws.onerror = () => setRunning(false);
      })
      .catch((err) => {
        setError(String(err?.message || err));
        setRunning(false);
      });
  }

  async function resolveApproval(decision: boolean) {
    if (!approval) return;
    try {
      await api.resolveApproval(approval.id, decision);
    } catch {
      /* 服务侧超时兜底 */
    }
    setApproval(null);
  }

  return (
    <div className="app">
      <Sidebar
        open={sidebarOpen}
        sessions={sessions}
        currentId={sid}
        disabled={running}
        onSelect={selectSession}
        onNew={newChat}
        onDelete={(id) => api.deleteSession(id).then(refreshSessions).catch(() => {})}
        onCollapse={() => toggleSidebar(false)}
        onSettings={() => setSettingsOpen(true)}
      />
      <main className="main">
        {!sidebarOpen && (
          <button className="expand-btn" title="展开侧边栏" onClick={() => toggleSidebar(true)}>
            ☰
          </button>
        )}
        <ChatView
          messages={messages}
          steps={steps}
          stream={stream}
          running={running}
          approval={approval}
          error={error}
          mode={mode}
          onMode={(m) => {
            setMode(m);
            api.patchSettings({ permission: m } as never).catch(() => {});
          }}
          onSend={send}
          onResolveApproval={resolveApproval}
        />
      </main>
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
