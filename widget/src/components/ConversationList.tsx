import { useState } from "react";
import type { Conversation } from "../types";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}

export function ConversationList({ conversations, activeId, onSelect, onNew, onRename, onDelete }: Props) {
  const [editing, setEditing] = useState<string | null>(null);
  const [title, setTitle] = useState("");

  return (
    <nav className="aos-convs" aria-label="Conversations">
      <button type="button" className="aos-btn aos-btn--primary aos-convs__new" onClick={onNew}>
        + New chat
      </button>
      <ul>
        {conversations.map((c) => (
          <li key={c.id} className={c.id === activeId ? "is-active" : undefined}>
            {editing === c.id ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  if (title.trim()) onRename(c.id, title.trim());
                  setEditing(null);
                }}
              >
                <input aria-label="Conversation name" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus maxLength={200} />
              </form>
            ) : (
              <>
                <button type="button" className="aos-convs__item" onClick={() => onSelect(c.id)} aria-current={c.id === activeId}>
                  {c.title || "New chat"}
                </button>
                <span className="aos-convs__tools">
                  <button
                    type="button"
                    className="aos-icon-btn"
                    aria-label={`Rename ${c.title ?? "chat"}`}
                    onClick={() => {
                      setEditing(c.id);
                      setTitle(c.title ?? "");
                    }}
                  >
                    ✎
                  </button>
                  <button
                    type="button"
                    className="aos-icon-btn"
                    aria-label={`Delete ${c.title ?? "chat"}`}
                    onClick={() => {
                      if (window.confirm("Delete this conversation?")) onDelete(c.id);
                    }}
                  >
                    🗑
                  </button>
                </span>
              </>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
