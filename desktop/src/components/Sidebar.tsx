import type { Session } from "../api";

interface Props {
  open: boolean;
  sessions: Session[];
  currentId: string | null;
  disabled: boolean;
  onSelect: (session: Session) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onCollapse: () => void;
  onSettings: () => void;
}

export default function Sidebar({
  open,
  sessions,
  currentId,
  disabled,
  onSelect,
  onNew,
  onDelete,
  onCollapse,
  onSettings,
}: Props) {
  if (!open) return null;
  return (
    <aside className="sidebar">
      <header className="side-head">
        <div className="brand">
          <span className="brand-mark">M</span>
          <span>
            MeoClaw
            <small>Local agent</small>
          </span>
        </div>
        <button className="icon-button collapse-btn" title="收起侧边栏" onClick={onCollapse} disabled={disabled}>
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="M12.5 5 7.5 10l5 5" />
          </svg>
        </button>
      </header>

      <button className="new-btn" onClick={onNew} disabled={disabled}>
        <svg viewBox="0 0 20 20" aria-hidden="true">
          <path d="M10 4v12M4 10h12" />
        </svg>
        <span>新任务</span>
      </button>

      <div className="side-section-label">最近会话</div>
      <nav className="side-list" aria-label="历史会话">
        {sessions.length === 0 && <div className="side-empty">还没有历史任务</div>}
        {sessions.map((session) => (
          <div
            key={session.id}
            className={`session-item${session.id === currentId ? " active" : ""}`}
            onClick={() => !disabled && onSelect(session)}
            onKeyDown={(event) => {
              if (!disabled && (event.key === "Enter" || event.key === " ")) {
                event.preventDefault();
                onSelect(session);
              }
            }}
            role="button"
            tabIndex={disabled ? -1 : 0}
          >
            <span className="session-glyph" aria-hidden="true" />
            <span className="title">{session.title}</span>
            <button
              className="del"
              title="删除会话"
              onClick={(event) => {
                event.stopPropagation();
                onDelete(session.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
      </nav>

      <button className="settings-btn" onClick={onSettings}>
        <svg viewBox="0 0 20 20" aria-hidden="true">
          <circle cx="10" cy="10" r="2.5" />
          <path d="M10 3.2v1.4m0 10.8v1.4M3.2 10h1.4m10.8 0h1.4M5.2 5.2l1 1m6.6 6.6 1 1m0-8.6-1 1m-6.6 6.6-1 1" />
        </svg>
        <span>设置</span>
      </button>
    </aside>
  );
}
