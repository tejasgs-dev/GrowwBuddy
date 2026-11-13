import type { Scheme, Thread } from "../types";
import { Icon } from "./Icon";

interface Props {
  threads: Thread[];
  status: "loading" | "ready" | "error";
  activeId: string | null;
  schemes: Scheme[];
  persistent: boolean;
  open: boolean;
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (thread: Thread) => void;
}

/** Thread sidebar (task 5.4). A fixed column on desktop, a drawer on narrow screens. */
export function ThreadList({
  threads,
  status,
  activeId,
  schemes,
  persistent,
  open,
  onNew,
  onSelect,
  onDelete,
}: Props) {
  const shortNames = schemes.map((s) => s.name.replace(/^HDFC\s+/, "").replace(/\s+Fund$/, ""));
  return (
    <aside id="thread-drawer" className={`sidebar${open ? " is-open" : ""}`} aria-label="Chats">
      <div className="sidebar-top">
        <button type="button" className="btn btn-secondary new-chat" onClick={onNew}>
          <Icon name="plus" />
          <span>New chat</span>
        </button>

        <h2 className="section-label sidebar-label" id="recent-label">
          Recent chats
        </h2>
        {status === "loading" && <p className="sidebar-empty">Loading chats…</p>}
        {status === "error" && (
          <p className="sidebar-empty" role="status">
            Couldn't load your chats.
          </p>
        )}
        {status === "ready" && threads.length === 0 && (
          <p className="sidebar-empty">No chats yet. Ask a question to start one.</p>
        )}
        <ul className="thread-list" aria-labelledby="recent-label">
          {threads.map((t) => {
            const active = t.thread_id === activeId;
            return (
              <li key={t.thread_id} className={`thread${active ? " is-active" : ""}`}>
                <button
                  type="button"
                  className="thread-open"
                  aria-current={active ? "true" : undefined}
                  onClick={() => onSelect(t.thread_id)}
                  title={t.title}
                >
                  {t.title}
                </button>
                <button
                  type="button"
                  className="icon-btn thread-delete"
                  aria-label={`Delete chat: ${t.title}`}
                  onClick={() => onDelete(t)}
                >
                  <Icon name="trash" size={16} />
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      <div className="sidebar-bottom">
        {!persistent && (
          <p className="storage-note" role="note">
            <Icon name="info" size={14} />
            Your browser is blocking storage, so these chats will disappear when you reload.
          </p>
        )}
        <div className="scope-card">
          <p className="scope-title">
            <Icon name="verified" size={14} />
            Coverage scope
          </p>
          <p className="scope-text">
            Covers {schemes.length} HDFC schemes: {shortNames.join(", ")}.
          </p>
        </div>
      </div>
    </aside>
  );
}
