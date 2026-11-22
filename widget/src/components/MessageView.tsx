import { useState } from "react";
import { formatTokens } from "../format";
import type { AssistantItem, ChatItem } from "../hooks/useChat";
import { ActionCard } from "./ActionCard";
import { Markdown } from "./Markdown";

const REASONS: [string, string][] = [
  ["wrong_numbers", "Wrong numbers"],
  ["didnt_understand", "Didn't understand me"],
  ["too_long", "Too long"],
  ["other", "Other"],
];

interface Props {
  item: ChatItem;
  onRate: (item: AssistantItem, rating: "up" | "down", reason?: string, comment?: string) => void;
  onAction: (item: AssistantItem, actionId: string, op: "confirm" | "cancel" | "undo") => void;
}

export function MessageView({ item, onRate, onAction }: Props) {
  if (item.kind === "user") {
    return (
      <div className="aos-msg aos-msg--user">
        <div className="aos-bubble">{item.text}</div>
      </div>
    );
  }
  return <AssistantMessage item={item} onRate={onRate} onAction={onAction} />;
}

function AssistantMessage({ item, onRate, onAction }: { item: AssistantItem } & Omit<Props, "item">) {
  const [askReason, setAskReason] = useState(false);
  const [reason, setReason] = useState("wrong_numbers");
  const [comment, setComment] = useState("");
  const end = item.end;
  const ungrounded = end?.quality_flags?.ungrounded_numbers ?? [];

  return (
    <div className="aos-msg aos-msg--assistant">
      <div className="aos-bubble" aria-busy={item.streaming}>
        {item.text ? <Markdown text={item.text} /> : item.streaming && !item.toolLabel && <span className="aos-typing">Thinking…</span>}
        {item.toolLabel && (
          <div className="aos-tool" role="status">
            <span className="aos-spinner" aria-hidden="true" /> {item.toolLabel}…
          </div>
        )}
        {item.actions.map((card) => (
          <ActionCard
            key={card.proposal.action_id}
            card={card}
            onConfirm={() => onAction(item, card.proposal.action_id, "confirm")}
            onCancel={() => onAction(item, card.proposal.action_id, "cancel")}
            onUndo={() => onAction(item, card.proposal.action_id, "undo")}
          />
        ))}
        {ungrounded.length > 0 && (
          <p className="aos-flag">Some figures in this answer couldn't be verified against your data. Please check them.</p>
        )}
        {item.error && (
          <p className="aos-error" role="alert">
            {item.error.message}
          </p>
        )}
      </div>
      {end && !item.streaming && (
        <div className="aos-meta">
          <span className="aos-tokens" title="Billable tokens for this reply">
            {formatTokens(end.tokens_charged)}
            {end.billing_mode === "shadow" ? " · free during pilot" : ""}
          </span>
          {item.messageId && !item.messageId.startsWith("local-") && (
            <span className="aos-rate">
              {item.rating ? (
                <span className="aos-thanks">Thanks for the feedback</span>
              ) : (
                <>
                  <button type="button" className="aos-icon-btn" aria-label="Good answer" onClick={() => onRate(item, "up")}>
                    👍
                  </button>
                  <button type="button" className="aos-icon-btn" aria-label="Bad answer" onClick={() => setAskReason(true)}>
                    👎
                  </button>
                </>
              )}
            </span>
          )}
        </div>
      )}
      {askReason && !item.rating && (
        <form
          className="aos-feedback"
          onSubmit={(e) => {
            e.preventDefault();
            onRate(item, "down", reason, comment || undefined);
            setAskReason(false);
          }}
        >
          <label>
            What was wrong?{" "}
            <select value={reason} onChange={(e) => setReason(e.target.value)}>
              {REASONS.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
          </label>
          <input placeholder="Optional details" value={comment} onChange={(e) => setComment(e.target.value)} maxLength={2000} />
          <button type="submit" className="aos-btn">
            Send
          </button>
        </form>
      )}
    </div>
  );
}
