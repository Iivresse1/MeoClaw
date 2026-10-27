import { useState } from "react";
import type { Session } from "../api";
import ArchiveSessionPrototype, {
  type ArchivePrototypeVariant,
} from "./prototype/ArchiveSessionPrototype";

interface Props {
  open: boolean;
  sessions: Session[];
  currentId: string | null;
  disabled: boolean;
  onSelect: (session: Session) => void;
  onNew: () => void;
  onSetArchived: (id: string, archived: boolean) => void;
  onCollapse: () => void;
  onSettings: () => void;
  archivePrototype?: ArchivePrototypeVariant | null;
}

export default function Sidebar({
  open,
  sessions,
  currentId,
  disabled,
  onSelect,
  onNew,
  onSetArchived,
  onCollapse,
  onSettings,
  archivePrototype = null,
}: Props) {
  const [showArchived, setShowArchived] = useState(false);
  if (!open) return null;
  const archivedCount = sessions.filter((session) => session.archived).length;
  const visibleSessions = [
    ...(showArchived
      ? sessions
      : sessions.filter((session) => !session.archived)),
  ].sort((left, right) => right.created_at - left.created_at);

  function ArchiveIcon({ restore }: { restore: boolean }) {
    return (
      <svg viewBox="0 0 20 20" aria-hidden="true">
        {restore ? (
          <path d="M6 5H3v3m0-3 3.4 3.4A5 5 0 1 1 5 12" />
        ) : (
          <path d="M4 6.5h12v9H4zM3 3.5h14v3H3zM8 10h4" />
        )}
      </svg>
    );
  }

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

      {archivePrototype ? (
        <ArchiveSessionPrototype
          variant={archivePrototype}
          sessions={sessions}
          currentId={currentId}
          disabled={disabled}
          onSelect={onSelect}
          onNew={onNew}
        />
      ) : (
        <>
          <div className="new-task-row">
            <button className="new-btn" onClick={onNew} disabled={disabled}>
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M10 4v12M4 10h12" />
              </svg>
              <span>新任务</span>
            </button>
            <button
              className={`archive-toggle${showArchived ? " is-open" : ""}`}
              title={showArchived ? "隐藏归档会话" : "显示归档会话"}
              aria-expanded={showArchived}
              onClick={() => setShowArchived((value) => !value)}
            >
              <ArchiveIcon restore={false} />
              {archivedCount > 0 && <span>{archivedCount}</span>}
            </button>
          </div>

          <div className="side-section-label">最近会话</div>
          <nav className="side-list" aria-label="历史会话">
            {visibleSessions.length === 0 && (
              <div className="side-empty">还没有历史任务</div>
            )}
            {visibleSessions.map((session) => (
              <div
                key={session.id}
                className={`session-item${session.id === currentId ? " active" : ""}${
                  session.archived ? " is-archived" : ""
                }`}
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
                  className="session-action"
                  title={session.archived ? "恢复会话" : "归档会话"}
                  onClick={(event) => {
                    event.stopPropagation();
                    onSetArchived(session.id, !session.archived);
                  }}
                >
                  <ArchiveIcon restore={session.archived} />
                </button>
              </div>
            ))}
          </nav>
        </>
      )}

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
