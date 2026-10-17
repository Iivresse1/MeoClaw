import { KeyboardEvent } from "react";
import { MODE_LABELS } from "../App";

interface Props {
  value: string;
  onChange: (v: string) => void;
  mode: string;
  onMode: (m: string) => void;
  running: boolean;
  onSend: () => void;
}

export default function Composer({ value, onChange, mode, onMode, running, onSend }: Props) {
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!running && value.trim()) onSend();
    }
  }
  return (
    <div className="composer">
      <textarea
        className="input-box"
        rows={2}
        placeholder="输入消息，Enter 发送，Shift+Enter 换行"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="composer-bar">
        <select
          className="mode-select"
          value={mode}
          onChange={(e) => onMode(e.target.value)}
          disabled={running}
          title="权限档位"
        >
          {Object.entries(MODE_LABELS).map(([k, label]) => (
            <option key={k} value={k}>
              {label}
            </option>
          ))}
        </select>
        <button
          className="send-btn"
          onClick={onSend}
          disabled={running || !value.trim()}
          title="发送"
        >
          ↑
        </button>
      </div>
    </div>
  );
}
