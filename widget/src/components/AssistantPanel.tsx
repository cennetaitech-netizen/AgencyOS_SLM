import { useEffect, useRef, useState } from "react";
import type { ChatApiClient } from "../api/client";
import { formatTokens } from "../format";
import { useChat } from "../hooks/useChat";
import { ConversationList } from "./ConversationList";
import { MessageView } from "./MessageView";

export const DEFAULT_STARTERS = [
  "How did my campaigns perform last week?",
  "Which campaigns are behind on budget pacing?",
  "Compare Google and Meta for my top client this month",
  "Write 3 Google search headlines for a festive offer",
];

const BLOCK_TEXT: Record<string, string> = {
  insufficient_tokens: "You've used all your tokens.",
  daily_limit_reached: "Your organisation has reached today's assistant limit. It resets at midnight.",
  usage_cap_reached: "You've reached the assistant limit set by your admin.",
  billing_suspended: "The assistant is paused because of an unpaid invoice. Please contact your admin.",
  chat_disabled: "The assistant isn't available right now.",
};

export interface AssistantPanelProps {
  client: ChatApiClient;
  title?: string;
  starterPrompts?: string[];
  onBuyTokens?: () => void;
  className?: string;
}

export function AssistantPanel({
  client,
  title = "AgencyOS Assistant",
  starterPrompts = DEFAULT_STARTERS,
  onBuyTokens,
  className,
}: AssistantPanelProps) {
  const chat = useChat(client);
  const [draft, setDraft] = useState("");
  const [showTokenHelp, setShowTokenHelp] = useState(false);
  const [convsOpen, setConvsOpen] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const logRef = useRef<HTMLDivElement>(null);

  // Scroll only the message list (scrollIntoView would also scroll the host page and the panel).
  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [chat.items]);

  const submit = (text: string) => {
    if (!text.trim() || chat.sending || chat.blocked) return;
    setDraft("");
    void chat.send(text);
    inputRef.current?.focus();
  };

  const wallet = chat.wallet;
  const shadow = wallet?.billing_mode === "shadow";

  return (
    <div className={`aos-panel ${convsOpen ? "aos-panel--convs-open" : ""} ${className ?? ""}`}>
      <ConversationList
        conversations={chat.conversations}
        activeId={chat.activeId}
        onSelect={(id) => {
          setConvsOpen(false);
          void chat.selectConversation(id);
        }}
        onNew={() => {
          setConvsOpen(false);
          chat.newConversation();
        }}
        onRename={(id, t) => void chat.rename(id, t)}
        onDelete={(id) => void chat.remove(id)}
      />
      <main className="aos-main">
        <header className="aos-header">
          <button
            type="button"
            className="aos-btn aos-only-mobile"
            aria-expanded={convsOpen}
            onClick={() => setConvsOpen((v) => !v)}
          >
            Chats
          </button>
          <h2>{title}</h2>
          {wallet && (
            <div className="aos-balance">
              <span aria-label="Token balance">{formatTokens(Math.max(0, wallet.spendable))}</span>
              {shadow && <span className="aos-badge">Free during pilot</span>}
              <button type="button" className="aos-btn aos-btn--link" onClick={() => setShowTokenHelp((v) => !v)}>
                What is a token?
              </button>
            </div>
          )}
        </header>
        {showTokenHelp && (
          <p className="aos-help">
            A token is a small piece of text, roughly ¾ of an English word. A typical question and answer uses a few
            hundred tokens; a detailed report uses a few thousand. Every reply shows how many it used, and failed
            replies are never charged.
          </p>
        )}
        {wallet?.low_balance && !shadow && !chat.blocked && (
          <div className="aos-banner aos-banner--warn" role="status">
            Your token balance is running low.
            {onBuyTokens && (
              <button type="button" className="aos-btn aos-btn--link" onClick={onBuyTokens}>
                Buy tokens
              </button>
            )}
          </div>
        )}
        {chat.alerts.length > 0 && (
          <div className="aos-banner aos-banner--info" role="status">
            {chat.alerts.map((a) => (
              <span key={`${a.scope}-${a.threshold}`}>
                You've used {a.threshold}% of {a.scope.startsWith("cap:") ? "a usage limit" : "your token allowance"}.{" "}
              </span>
            ))}
            <button type="button" className="aos-btn aos-btn--link" onClick={chat.dismissAlerts}>
              Dismiss
            </button>
          </div>
        )}

        <div ref={logRef} className="aos-log" role="log" aria-live="polite" aria-relevant="additions text">
          {chat.items.length === 0 ? (
            <div className="aos-empty">
              <p>Ask about your campaigns, budgets, pacing, audiences or ad copy.</p>
              <div className="aos-starters">
                {starterPrompts.map((s) => (
                  <button key={s} type="button" className="aos-chip" onClick={() => submit(s)}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            chat.items.map((item) => (
              <MessageView
                key={item.id}
                item={item}
                onRate={(it, rating, reason, comment) =>
                  it.messageId && void chat.rate(it.id, it.messageId, rating, reason, comment)
                }
                onAction={(it, actionId, op) => void chat.runAction(it.id, actionId, op)}
              />
            ))
          )}
        </div>

        {chat.blocked && (
          <div className="aos-banner aos-banner--block" role="alert">
            {BLOCK_TEXT[chat.blocked.code] ?? chat.blocked.message}
            {chat.blocked.code === "insufficient_tokens" && onBuyTokens && (
              <button type="button" className="aos-btn aos-btn--primary" onClick={onBuyTokens}>
                Buy tokens
              </button>
            )}
            <button type="button" className="aos-btn aos-btn--link" onClick={() => void chat.clearBlocked()}>
              Try again
            </button>
          </div>
        )}

        <form
          className="aos-composer"
          onSubmit={(e) => {
            e.preventDefault();
            submit(draft);
          }}
        >
          <textarea
            ref={inputRef}
            aria-label="Message the assistant"
            placeholder={chat.blocked ? "Sending is paused" : "Ask about your campaigns…"}
            value={draft}
            maxLength={8000}
            rows={2}
            disabled={!!chat.blocked}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit(draft);
              }
            }}
          />
          {chat.sending ? (
            <button type="button" className="aos-btn" onClick={chat.stop}>
              Stop
            </button>
          ) : (
            <button type="submit" className="aos-btn aos-btn--primary" disabled={!draft.trim() || !!chat.blocked}>
              Send
            </button>
          )}
        </form>
        <p className="aos-disclaimer">AI answers can be wrong. Check important figures before acting.</p>
      </main>
    </div>
  );
}
