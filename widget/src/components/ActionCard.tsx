import { changedFields, formatValue } from "../format";
import type { ActionCardModel } from "../hooks/useChat";

const TIER_LABEL: Record<number, string> = { 1: "Low risk", 2: "Medium risk", 3: "High risk" };

interface Props {
  card: ActionCardModel;
  onConfirm: () => void;
  onCancel: () => void;
  onUndo: () => void;
}

/** The only way a change happens: the user reviews the exact before → after and clicks Confirm. */
export function ActionCard({ card, onConfirm, onCancel, onUndo }: Props) {
  const { proposal, view, busy, error } = card;
  const fields = changedFields(proposal.before, proposal.after);
  const status = view?.status ?? "proposed";
  const open = status === "proposed";

  return (
    <section className={`aos-action aos-action--${status}`} aria-label={`Proposed change: ${proposal.label}`}>
      <header className="aos-action__head">
        <strong>{proposal.undo_of ? `Undo: ${proposal.label}` : proposal.label}</strong>
        <span className={`aos-tier aos-tier--${proposal.risk_tier}`}>{TIER_LABEL[proposal.risk_tier]}</span>
      </header>
      {proposal.target_id && <div className="aos-action__target">{String(proposal.before.name ?? proposal.target_id)}</div>}
      <table className="aos-action__diff">
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">Now</th>
            <th scope="col">After</th>
          </tr>
        </thead>
        <tbody>
          {fields.map((f) => (
            <tr key={f}>
              <th scope="row">{f}</th>
              <td>{formatValue(proposal.before[f])}</td>
              <td className="aos-action__new">{formatValue(proposal.after[f])}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {proposal.warnings.length > 0 && (
        <ul className="aos-action__warnings">
          {proposal.warnings.map((w) => (
            <li key={w}>⚠ {w}</li>
          ))}
        </ul>
      )}
      {open && (
        <div className="aos-action__buttons">
          <button type="button" className="aos-btn aos-btn--primary" disabled={busy} onClick={onConfirm}>
            {busy ? "Applying…" : proposal.requires_second_approval ? "Confirm and request approval" : "Confirm"}
          </button>
          <button type="button" className="aos-btn" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
        </div>
      )}
      {status === "awaiting_second_approval" && <p className="aos-action__note">Waiting for a second approver.</p>}
      {status === "executed" && (
        <div className="aos-action__done">
          <span>✓ {view?.summary ?? "Done"}</span>
          {view?.undo_available && (
            <button type="button" className="aos-btn aos-btn--link" disabled={busy} onClick={onUndo}>
              Undo
            </button>
          )}
        </div>
      )}
      {status === "undone" && <p className="aos-action__note">This change was undone.</p>}
      {(status === "cancelled" || status === "expired" || status === "rejected") && (
        <p className="aos-action__note">
          {status === "cancelled" ? "Cancelled." : status === "expired" ? "Expired. Ask again." : "Rejected."}
          {view?.error ? ` ${view.error}` : ""}
        </p>
      )}
      {status === "failed" && <p className="aos-action__error">The change wasn't made: {view?.error}</p>}
      {error && (
        <p className="aos-action__error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
