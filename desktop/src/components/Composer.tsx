import { KeyboardEvent } from "react";
import { MODE_HINTS, MODE_LABELS } from "../types";

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
        rows={3}
        placeholder={running ? "MeoClaw 正在执行当前任务…" : "描述任务、粘贴错误，或让 MeoClaw 检查当前项目"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="composer-bar">
        <label className="composer-mode" title={MODE_HINTS[mode] || "执行权限"}>
          <span className="mode-dot" />
          <select
            className="mode-select"
            value={mode}
            onChange={(e) => onMode(e.target.value)}
            disabled={running}
          >
            {Object.entries(MODE_LABELS).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <button
          className="send-btn"
          onClick={onSend}
          disabled={running || !value.trim()}
          title="发送"
        >
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="M10 15V5m0 0L5.5 9.5M10 5l4.5 4.5" />
          </svg>
        </button>
      </div>
    </div>
  );
}
