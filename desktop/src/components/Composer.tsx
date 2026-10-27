import { KeyboardEvent } from "react";
import { PERMISSION_HINTS, PERMISSION_LABELS } from "../types";

interface Props {
  value: string;
  onChange: (v: string) => void;
  permission: string;
  onPermission: (permission: string) => void;
  running: boolean;
  locked?: boolean;
  onSend: () => void;
}

export default function Composer({
  value,
  onChange,
  permission,
  onPermission,
  running,
  locked = false,
  onSend,
}: Props) {
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!running && !locked && value.trim()) onSend();
    }
  }
  return (
    <div className="composer">
      <textarea
        className="input-box"
        rows={3}
        placeholder={
          locked
            ? "归档会话仅可查看历史，恢复后才能继续"
            : running
              ? "MeoClaw 正在执行当前任务…"
              : "描述任务、粘贴错误，或让 MeoClaw 检查当前项目"
        }
        disabled={locked}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="composer-bar">
        <label
          className="composer-permission"
          title={PERMISSION_HINTS[permission] || "执行权限"}
        >
          <span className="permission-dot" />
          <select
            className="permission-select"
            value={permission}
            onChange={(e) => onPermission(e.target.value)}
            disabled={running || locked}
          >
            {Object.entries(PERMISSION_LABELS).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <button
          className="send-btn"
          onClick={onSend}
          disabled={running || locked || !value.trim()}
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
