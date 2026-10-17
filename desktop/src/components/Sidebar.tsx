import type { Session } from "../api";

interface Props {
  open: boolean;
  sessions: Session[];
  currentId: string | null;
  disabled: boolean;
  onSelect: (s: Session) => void;
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
      <div className="side-head">
        <button className="collapse-btn" title="收起侧边栏" onClick={onCollapse} disabled={disabled}>
          «
        </button>
        <div className="brand">MeoClaw🐱</div>
      </div>
      <button className="new-btn" onClick={onNew} disabled={disabled}>
        ＋ 新会话
      </button>
      <div className="side-list">
        {sessions.map((s) => (
          <div
            key={s.id}
            className={"item" + (s.id === currentId ? " active" : "")}
            onClick={() => !disabled && onSelect(s)}
          >
            <span className="title">{s.title}</span>
            <button
              className="del"
              title="删除会话"
              onClick={(e) => {
                e.stopPropagation();
                onDelete(s.id);
              }}
            >
              ✕
            </button>
          </div>
        ))}
      </div>
      <button className="settings-btn" onClick={onSettings}>
        设置
      </button>
    </aside>
  );
}
