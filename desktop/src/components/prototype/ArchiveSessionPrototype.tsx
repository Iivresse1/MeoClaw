import { useState } from "react";
import type { Session } from "../../api";

export type ArchivePrototypeVariant = "A" | "B" | "C";

const SAMPLE_TIME = Date.now() / 1000;
const SAMPLE_ARCHIVED: Session[] = [
  {
    id: "prototype-archive-1",
    title: "整理网关重试策略",
    permission: "human",
    archived: true,
    created_at: SAMPLE_TIME - 3600,
  },
  {
    id: "prototype-archive-2",
    title: "桌面端快捷操作调研",
    permission: "human",
    archived: true,
    created_at: SAMPLE_TIME - 86400,
  },
  {
    id: "prototype-archive-3",
    title: "修复旧版启动脚本",
    permission: "human",
    archived: true,
    created_at: SAMPLE_TIME - 259200,
  },
];

interface Props {
  variant: ArchivePrototypeVariant;
  sessions: Session[];
  currentId: string | null;
  disabled: boolean;
  onSelect: (session: Session) => void;
  onNew: () => void;
}

function ArchiveIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M4 6.5h12v9H4zM3 3.5h14v3H3zM8 10h4" />
    </svg>
  );
}

function RestoreIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M6 5H3v3m0-3 3.4 3.4A5 5 0 1 1 5 12" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M10 4v12M4 10h12" />
    </svg>
  );
}

function NewTaskButton({
  disabled,
  onNew,
  compact = false,
}: {
  disabled: boolean;
  onNew: () => void;
  compact?: boolean;
}) {
  return (
    <button
      className={`new-btn prototype-new-task${compact ? " is-compact" : ""}`}
      onClick={onNew}
      disabled={disabled}
    >
      <PlusIcon />
      <span>新任务</span>
    </button>
  );
}

function useArchiveDemo(sessions: Session[]) {
  const [archivedRealIds, setArchivedRealIds] = useState<string[]>([]);
  const [restoredSamples, setRestoredSamples] = useState<string[]>([]);

  const activeSessions = sessions.filter(
    (session) => !session.archived && !archivedRealIds.includes(session.id)
  );
  const archivedSessions = [
    ...sessions
      .filter((session) => archivedRealIds.includes(session.id))
      .map((session) => ({ ...session, archived: true })),
    ...SAMPLE_ARCHIVED.filter(
      (session) => !restoredSamples.includes(session.id)
    ),
  ];

  function archive(session: Session) {
    setArchivedRealIds((current) =>
      current.includes(session.id) ? current : [...current, session.id]
    );
  }

  function restore(session: Session) {
    if (session.id.startsWith("prototype-archive-")) {
      setRestoredSamples((current) => [...current, session.id]);
      return;
    }
    setArchivedRealIds((current) =>
      current.filter((id) => id !== session.id)
    );
  }

  return {
    activeSessions,
    archivedSessions,
    archive,
    restore,
  };
}

function SessionRow({
  session,
  active,
  disabled,
  onSelect,
  archived = false,
  onArchive,
  onRestore,
}: {
  session: Session;
  active: boolean;
  disabled: boolean;
  onSelect: (session: Session) => void;
  archived?: boolean;
  onArchive?: (session: Session) => void;
  onRestore?: (session: Session) => void;
}) {
  return (
    <div
      className={`session-item prototype-session${active ? " active" : ""}${
        archived ? " is-archived" : ""
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
      {archived ? (
        <button
          className="prototype-row-action"
          title="恢复会话"
          onClick={(event) => {
            event.stopPropagation();
            onRestore?.(session);
          }}
        >
          <RestoreIcon />
        </button>
      ) : (
        <button
          className="prototype-row-action"
          title="归档会话"
          onClick={(event) => {
            event.stopPropagation();
            onArchive?.(session);
          }}
        >
          <ArchiveIcon />
        </button>
      )}
    </div>
  );
}

function VariantA({
  sessions,
  currentId,
  disabled,
  onSelect,
  onNew,
}: Omit<Props, "variant">) {
  const [showArchived, setShowArchived] = useState(false);
  const demo = useArchiveDemo(sessions);
  const visibleSessions = (
    showArchived
      ? [...demo.activeSessions, ...demo.archivedSessions]
      : demo.activeSessions
  ).sort((left, right) => right.created_at - left.created_at);

  return (
    <>
      <div className="prototype-new-row">
        <NewTaskButton disabled={disabled} onNew={onNew} compact />
        <button
          className={`prototype-round-toggle${showArchived ? " is-open" : ""}`}
          title={showArchived ? "隐藏归档会话" : "显示归档会话"}
          aria-expanded={showArchived}
          onClick={() => setShowArchived((value) => !value)}
        >
          <ArchiveIcon />
          <span>{demo.archivedSessions.length}</span>
        </button>
      </div>

      <div className="side-section-label">最近会话</div>
      <nav className="side-list" aria-label="活动会话">
        {visibleSessions.length === 0 && (
          <div className="side-empty">没有活动会话</div>
        )}
        {visibleSessions.map((session) => (
          <SessionRow
            key={session.id}
            session={session}
            active={session.id === currentId}
            disabled={disabled}
            onSelect={onSelect}
            onArchive={demo.archive}
            onRestore={demo.restore}
            archived={session.archived}
          />
        ))}
      </nav>
    </>
  );
}

function VariantB({
  sessions,
  currentId,
  disabled,
  onSelect,
  onNew,
}: Omit<Props, "variant">) {
  const [showArchived, setShowArchived] = useState(false);
  const demo = useArchiveDemo(sessions);

  return (
    <>
      <NewTaskButton disabled={disabled} onNew={onNew} />
      <div className="prototype-section-row">
        <span className="side-section-label">最近会话</span>
        <button
          className={`prototype-round-toggle is-small${showArchived ? " is-open" : ""}`}
          title={showArchived ? "隐藏归档会话" : "显示归档会话"}
          aria-expanded={showArchived}
          onClick={() => setShowArchived((value) => !value)}
        >
          <ArchiveIcon />
          <span>{demo.archivedSessions.length}</span>
        </button>
        {showArchived && (
          <div className="prototype-archive-popover">
            <header>
              <strong>归档会话</strong>
              <button onClick={() => setShowArchived(false)}>关闭</button>
            </header>
            {demo.archivedSessions.map((session) => (
              <SessionRow
                key={session.id}
                session={session}
                active={session.id === currentId}
                disabled={disabled}
                onSelect={onSelect}
                archived
                onRestore={demo.restore}
              />
            ))}
          </div>
        )}
      </div>

      <nav className="side-list" aria-label="活动会话">
        {demo.activeSessions.map((session) => (
          <SessionRow
            key={session.id}
            session={session}
            active={session.id === currentId}
            disabled={disabled}
            onSelect={onSelect}
            onArchive={demo.archive}
          />
        ))}
      </nav>
    </>
  );
}

function VariantC({
  sessions,
  currentId,
  disabled,
  onSelect,
  onNew,
}: Omit<Props, "variant">) {
  const [view, setView] = useState<"active" | "archived">("active");
  const demo = useArchiveDemo(sessions);
  const showingArchived = view === "archived";

  return (
    <>
      <div className="prototype-new-row">
        <NewTaskButton disabled={disabled} onNew={onNew} compact />
        <button
          className={`prototype-round-toggle${showingArchived ? " is-open" : ""}`}
          title={showingArchived ? "返回最近会话" : "显示归档会话"}
          aria-pressed={showingArchived}
          onClick={() =>
            setView((current) =>
              current === "active" ? "archived" : "active"
            )
          }
        >
          <ArchiveIcon />
        </button>
      </div>

      <div className="prototype-view-tabs" role="tablist">
        <button
          className={!showingArchived ? "is-active" : ""}
          onClick={() => setView("active")}
        >
          最近
          <span>{demo.activeSessions.length}</span>
        </button>
        <button
          className={showingArchived ? "is-active" : ""}
          onClick={() => setView("archived")}
        >
          归档
          <span>{demo.archivedSessions.length}</span>
        </button>
      </div>

      <nav
        className="side-list"
        aria-label={showingArchived ? "归档会话" : "活动会话"}
      >
        {(showingArchived ? demo.archivedSessions : demo.activeSessions).map(
          (session) => (
            <SessionRow
              key={session.id}
              session={session}
              active={session.id === currentId}
              disabled={disabled}
              onSelect={onSelect}
              archived={showingArchived}
              onArchive={demo.archive}
              onRestore={demo.restore}
            />
          )
        )}
      </nav>
    </>
  );
}

export default function ArchiveSessionPrototype(props: Props) {
  if (props.variant === "A") return <VariantA {...props} />;
  if (props.variant === "B") return <VariantB {...props} />;
  return <VariantC {...props} />;
}
