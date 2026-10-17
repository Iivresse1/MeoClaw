import { useEffect, useRef, useState } from "react";
import type { ChatMsg, PendingApproval, Step } from "../App";
import ProgressCard from "./ProgressCard";
import ApprovalCard from "./ApprovalCard";
import Composer from "./Composer";

interface Props {
  messages: ChatMsg[];
  steps: Step[];
  stream: string;
  running: boolean;
  approval: PendingApproval | null;
  error: string;
  mode: string;
  onMode: (m: string) => void;
  onSend: (text: string) => void;
  onResolveApproval: (decision: boolean) => void;
}

export default function ChatView({
  messages,
  steps,
  stream,
  running,
  approval,
  error,
  mode,
  onMode,
  onSend,
  onResolveApproval,
}: Props) {
  const [draft, setDraft] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, stream, steps, running]);

  const empty = messages.length === 0 && !running && !stream;

  return (
    <div className="chat">
      <div className="chat-scroll">
        {empty ? (
          <div className="welcome">
            <div className="welcome-brand">MeoClaw🐱</div>
            <div className="welcome-sub">你的本地桌面助理，随时开始对话或处理任务</div>
          </div>
        ) : (
          <div className="msg-list">
            {messages.map((m, i) => (
              <div key={i} className={"msg " + m.role}>
                <div className="msg-body">{m.content}</div>
              </div>
            ))}
            {steps.length > 0 && <ProgressCard steps={steps} />}
            {stream && (
              <div className="msg assistant">
                <div className="msg-body">{stream}</div>
              </div>
            )}
            {error && <div className="error-line">{error}</div>}
            <div ref={bottomRef} />
          </div>
        )}
      </div>
      <div className="chat-bottom">
        {approval && <ApprovalCard approval={approval} onResolve={onResolveApproval} />}
        <Composer
          value={draft}
          onChange={setDraft}
          mode={mode}
          onMode={onMode}
          running={running}
          onSend={() => {
            onSend(draft);
            setDraft("");
          }}
        />
      </div>
    </div>
  );
}
