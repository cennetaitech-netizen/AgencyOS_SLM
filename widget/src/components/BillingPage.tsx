import { useEffect, useState } from "react";
import { ApiError, type ChatApiClient } from "../api/client";
import { BUCKET_LABELS, formatDate, formatMoney, formatTokens } from "../format";
import type { Cap, Invoice, Pack, Subscription, Wallet } from "../types";

export interface BillingPageProps {
  client: ChatApiClient;
  /** Called with the payment provider's checkout URL; default navigates the browser there. */
  onCheckout?: (url: string) => void;
}

/** Usage & billing for org admins: balance, packs, plan, invoices (with usage CSV) and caps. */
export function BillingPage({ client, onCheckout = (url) => window.location.assign(url) }: BillingPageProps) {
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [packs, setPacks] = useState<Pack[]>([]);
  const [sub, setSub] = useState<{ subscription: Subscription | null; capacity_tokens: number } | null>(null);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [caps, setCaps] = useState<Cap[]>([]);
  const [adminOnly, setAdminOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void client.getWallet().then(setWallet).catch(() => undefined);
    void client.listPacks().then(setPacks).catch(() => undefined);
    Promise.all([client.getSubscription(), client.listInvoices(), client.listCaps()])
      .then(([s, inv, c]) => {
        setSub(s);
        setInvoices(inv);
        setCaps(c);
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 403) setAdminOnly(true);
        else setError("Couldn't load billing details.");
      });
  }, [client]);

  const buy = async (packId: string) => {
    try {
      const { checkout_url } = await client.checkout(packId);
      onCheckout(checkout_url);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 403 ? "Only organisation admins can buy tokens." : "Checkout failed.");
    }
  };

  const downloadCsv = async (inv: Invoice) => {
    const csv = await client.invoiceUsageCsv(inv.invoice_id);
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `usage-${inv.invoice_number ?? inv.invoice_id}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="aos-billing">
      <h2>Assistant usage &amp; billing</h2>
      {error && (
        <p className="aos-error" role="alert">
          {error}
        </p>
      )}

      <section>
        <h3>Balance</h3>
        {wallet ? (
          <>
            <p className="aos-big">{formatTokens(Math.max(0, wallet.spendable))}</p>
            {wallet.billing_mode === "shadow" && <p className="aos-badge">Free during pilot: usage is counted, not charged</p>}
            <table className="aos-table">
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Remaining</th>
                  <th>Expires</th>
                </tr>
              </thead>
              <tbody>
                {wallet.buckets.map((b, i) => (
                  <tr key={i}>
                    <td>{BUCKET_LABELS[b.source] ?? b.source}</td>
                    <td>{formatTokens(b.remaining)}</td>
                    <td>{formatDate(b.expires_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <p>Loading…</p>
        )}
      </section>

      {packs.length > 0 && (
        <section>
          <h3>Buy tokens</h3>
          <div className="aos-packs">
            {packs.map((p) => (
              <div key={p.pack_id} className="aos-pack">
                <strong>{formatTokens(p.tokens)}</strong>
                <span>{formatMoney(p.price_minor, p.currency)}</span>
                <span className="aos-muted">Valid {p.validity_days} days</span>
                <button type="button" className="aos-btn aos-btn--primary" onClick={() => void buy(p.pack_id)}>
                  Buy
                </button>
              </div>
            ))}
          </div>
        </section>
      )}

      {adminOnly ? (
        <p className="aos-muted">Plan, invoices and limits are visible to organisation admins.</p>
      ) : (
        <>
          <section>
            <h3>Plan</h3>
            {sub?.subscription ? (
              <dl className="aos-dl">
                <dt>Plan</dt>
                <dd>
                  {sub.subscription.plan_name} ({formatMoney(sub.subscription.monthly_fee_minor, sub.subscription.currency)}
                  /month, {formatTokens(sub.subscription.monthly_included_tokens)} included)
                </dd>
                <dt>Current period</dt>
                <dd>
                  {formatDate(sub.subscription.current_period_start)} – {formatDate(sub.subscription.current_period_end)}
                </dd>
                <dt>Overage spend limit</dt>
                <dd>
                  {sub.subscription.spend_limit_minor == null
                    ? "No overage"
                    : formatMoney(sub.subscription.spend_limit_minor, sub.subscription.currency)}
                </dd>
                <dt>Status</dt>
                <dd>{sub.subscription.status.replace("_", " ")}</dd>
              </dl>
            ) : (
              <p className="aos-muted">No subscription. You're using prepaid tokens.</p>
            )}
          </section>

          <section>
            <h3>Invoices</h3>
            {invoices.length ? (
              <table className="aos-table">
                <thead>
                  <tr>
                    <th>Number</th>
                    <th>Period</th>
                    <th>Total</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {invoices.map((inv) => (
                    <tr key={inv.invoice_id}>
                      <td>{inv.invoice_number}</td>
                      <td>
                        {formatDate(inv.period_start)} – {formatDate(inv.period_end)}
                      </td>
                      <td>{formatMoney(inv.total_minor, inv.currency)}</td>
                      <td>{inv.status}</td>
                      <td>
                        <button type="button" className="aos-btn aos-btn--link" onClick={() => void downloadCsv(inv)}>
                          Usage CSV
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="aos-muted">No invoices yet.</p>
            )}
          </section>

          <section>
            <h3>Usage limits</h3>
            {caps.length ? (
              <ul>
                {caps.map((c) => (
                  <li key={c.cap_id}>
                    {c.scope_type === "org" ? "Organisation" : c.scope_type === "user" ? "User" : "Workspace"} ·{" "}
                    {formatTokens(c.max_tokens)} per {c.period}
                    {c.hard ? "" : " (warning only)"}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="aos-muted">No limits set.</p>
            )}
          </section>
        </>
      )}
    </div>
  );
}
