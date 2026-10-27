import { useEffect, useRef, useState } from "react";
import type {
  Artifact,
  ChatMsg,
  PendingApproval,
  Step,
} from "../types";
import { PERMISSION_HINTS, PERMISSION_LABELS } from "../types";
import ArtifactList from "./ArtifactList";
import ApprovalCard from "./ApprovalCard";
import Composer from "./Composer";
import ExecutionDetails from "./ExecutionDetails";
import MarkdownMessage from "./MarkdownMessage";

interface Props {
  messages: ChatMsg[];
  steps: Step[];
  artifacts: Artifact[];
  stream: string;
  running: boolean;
  durationMs: number | null;
  approval: PendingApproval | null;
  error: string;
  permission: string;
  onPermission: (permission: string) => void;
  archived: boolean;
  onRestore: () => void;
  onSend: (text: string) => void;
  onResolveApproval: (decision: boolean) => void;
}

const SUGGESTIONS = [
  "梳理当前项目的结构和关键模块",
  "检查最近改动并列出潜在风险",
  "创建一个脚本并验证它能够运行",
];

function durationLabel(durationMs: number | null): string {
  if (durationMs === null) return "";
  if (durationMs < 1000) return `${durationMs} ms`;
  return `${(durationMs / 1000).toFixed(1)} s`;
}

function CopyAnswer({ content }: { content: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      setCopied(false);
    }
  }

  return (
    <button className="answer-action" onClick={copy} title="复制回答">
      {copied ? "已复制" : "复制"}
    </button>
  );
}

function AssistantAnswer({
  content,
  streaming = false,
}: {
  content: string;
  streaming?: boolean;
}) {
  return (
    <article className={`assistant-answer${streaming ? " is-streaming" : ""}`}>
      <header className="answer-head">
        <span className="assistant-avatar">M</span>
        <div className="answer-identity">
          <strong>MeoClaw</strong>
          <span>{streaming ? "正在生成" : "任务结果"}</span>
        </div>
        {!streaming && content && <CopyAnswer content={content} />}
      </header>
      <MarkdownMessage content={content} />
      {streaming && <span className="stream-cursor" aria-hidden="true" />}
    </article>
  );
}

export default function ChatView({
  messages,
  steps,
  artifacts,
  stream,
  running,
  durationMs,
  approval,
  error,
  permission,
  onPermission,
  archived,
  onRestore,
  onSend,
  onResolveApproval,
}: Props) {
  const [draft, setDraft] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    bottomRef.current?.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "end",
    });
  }, [messages, stream, steps, running, artifacts]);

  const empty = messages.length === 0 && !running && !stream;
  const displayPermission = PERMISSION_LABELS[permission] || permission;

  return (
    <div className="chat">
      <header className="chat-topbar">
        <div className="run-state">
          <span className={`status-dot${running ? " is-running" : ""}`} />
          <span>{running ? "正在执行" : "准备就绪"}</span>
          {durationMs !== null && <span className="run-duration">{durationLabel(durationMs)}</span>}
        </div>
        <span
          className="permission-badge"
          title={PERMISSION_HINTS[permission] || ""}
        >
          {archived ? "已归档" : displayPermission}
        </span>
      </header>

      <div className="chat-scroll">
        {empty ? (
          <div className="welcome">
            <div className="welcome-mark">M</div>
            <h1>今天想让 MeoClaw 做什么？</h1>
            <p>把目标交给它，结果、执行过程和产出文件会被分层整理。</p>
            <div className="suggestion-list">
              {SUGGESTIONS.map((suggestion) => (
                <button key={suggestion} onClick={() => onSend(suggestion)}>
                  <span>{suggestion}</span>
                  <span aria-hidden="true">↗</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="msg-list" role="log" aria-live="polite">
            {messages.map((message, index) =>
              message.role === "user" ? (
                <div key={index} className="user-message">
                  <div className="user-bubble">{message.content}</div>
                </div>
              ) : (
                <AssistantAnswer key={index} content={message.content} />
              )
            )}

            {stream && <AssistantAnswer content={stream} streaming={running} />}
            {running && !stream && steps.length === 0 && (
              <div className="thinking-card" role="status">
                <span />
                <span />
                <span />
              </div>
            )}

            <ExecutionDetails steps={steps} running={running} />
            {!running && <ArtifactList artifacts={artifacts} />}
            {error && (
              <div className="error-card" role="alert">
                <strong>运行没有完成</strong>
                <span>{error}</span>
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      <div className="chat-bottom">
        {archived && (
          <div className="archived-banner">
            <span>此会话已归档，只能查看历史记录。</span>
            <button onClick={onRestore}>恢复会话</button>
          </div>
        )}
        {approval && <ApprovalCard approval={approval} onResolve={onResolveApproval} />}
        <Composer
          value={draft}
          onChange={setDraft}
          permission={permission}
          onPermission={onPermission}
          running={running}
          locked={archived}
          onSend={() => {
            onSend(draft);
            setDraft("");
          }}
        />
        <p className="composer-note">Enter 发送 · Shift + Enter 换行 · 所有工作区操作均可验证</p>
      </div>
    </div>
  );
}
