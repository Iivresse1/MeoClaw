import { useCallback, useEffect, useRef, useState } from "react";
import { api, taskSocket } from "./api";
import type { MsgRow, Session } from "./api";
import Sidebar from "./components/Sidebar";
import ChatView from "./components/ChatView";
import SettingsModal from "./components/SettingsModal";
import type { Artifact, ChatMsg, PendingApproval, Step, StepStatus } from "./types";
import { MODE_LABELS } from "./types";

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
  plan_summary?: string;
  passed?: boolean;
  checks?: unknown[];
  results?: unknown[];
  args?: Record<string, unknown>;
  ok?: boolean;
  result?: unknown;
}

function truncate(s: string, n = 90): string {
  s = (s || "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function detailText(value: unknown, limit = 1200): string {
  const text =
    typeof value === "string"
      ? value
      : JSON.stringify(value, null, 2) ?? String(value ?? "");
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
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
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [durationMs, setDurationMs] = useState<number | null>(null);
  const streamRef = useRef("");
  const stepIdRef = useRef(0);
  const runStartedRef = useRef<number | null>(null);

  function addStep(
    kind: string,
    text: string,
    status: StepStatus = "info",
    detail?: string
  ) {
    stepIdRef.current += 1;
    const step: Step = {
      id: `step-${stepIdRef.current}`,
      kind,
      text,
      status,
      detail,
    };
    setSteps((current) => [...current, step]);
  }

  function rememberArtifact(pathValue: unknown, operation: Artifact["operation"]) {
    const path = typeof pathValue === "string" ? pathValue.trim() : "";
    if (!path) return;
    setArtifacts((current) => {
      const existing = current.findIndex((item) => item.path === path);
      if (existing >= 0) {
        const next = [...current];
        next[existing] = {
          path,
          operation:
            next[existing].operation === "created" || operation === "created"
              ? "created"
              : "modified",
        };
        return next;
      }
      return [...current, { path, operation }];
    });
  }

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
    setArtifacts([]);
    setDurationMs(null);
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
    setArtifacts([]);
    setDurationMs(null);
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
    if (runStartedRef.current !== null) {
      setDurationMs(Date.now() - runStartedRef.current);
      runStartedRef.current = null;
    }
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
        addStep("阶段", ev.node || ev.name || "开始处理", "running");
        break;
      case "plan":
        addStep(
          "计划",
          truncate(ev.plan_summary || ev.content || "已生成执行计划", 80),
          "success",
          detailText(ev.checks ?? ev.results ?? undefined)
        );
        break;
      case "tool_call": {
        const name = ev.name || ev.tool || "工具";
        if (name === "FileWriteTool" || name === "FileEditTool") {
          rememberArtifact(
            ev.args?.file_path,
            name === "FileWriteTool" ? "created" : "modified"
          );
        }
        addStep(
          "工具",
          name,
          "running",
          ev.args && Object.keys(ev.args).length ? detailText(ev.args) : undefined
        );
        break;
      }
      case "tool_result": {
        const result = ev.result ?? ev.content ?? "工具执行完成";
        setSteps((current) => {
          const next = [...current];
          const index = [...next]
            .reverse()
            .findIndex((step) => step.kind === "工具" && step.status === "running");
          if (index < 0) return current;
          const target = next.length - 1 - index;
          next[target] = {
            ...next[target],
            status: ev.ok === false ? "error" : "success",
            detail: detailText(result),
          };
          return next;
        });
        break;
      }
      case "verification_command":
        addStep(
          "验收命令",
          truncate(ev.command || ev.content || "运行验收命令", 120),
          ev.ok === false ? "error" : "success",
          ev.results ? detailText(ev.results) : undefined
        );
        break;
      case "verification":
        addStep(
          "验收结论",
          ev.passed ? "全部检查通过" : "存在未通过项",
          ev.passed ? "success" : "error",
          ev.checks ? detailText(ev.checks) : undefined
        );
        break;
      case "react_done":
        addStep(
          "阶段完成",
          truncate(ev.content || "Agent 回合完成", 90),
          "success"
        );
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
        addStep(
          "任务结束",
          ev.passed === false ? "任务未通过验收" : "任务已完成",
          ev.passed === false ? "error" : "success"
        );
        break;
      case "approval_request":
        if (ev.id) setApproval({ id: ev.id, tool: ev.tool || "", detail: ev.detail || "" });
        break;
      case "error":
        setError(ev.message || "运行出错");
        addStep("错误", ev.message || "运行出错", "error");
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
    setArtifacts([]);
    setDurationMs(null);
    setStream("");
    streamRef.current = "";
    stepIdRef.current = 0;
    runStartedRef.current = Date.now();
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
          artifacts={artifacts}
          stream={stream}
          running={running}
          durationMs={durationMs}
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
