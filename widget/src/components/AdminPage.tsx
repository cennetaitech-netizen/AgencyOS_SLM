import { cloneElement, useCallback, useEffect, useId, useState, type ReactElement } from "react";
import { ApiError, type ChatApiClient } from "../api/client";
import { changedFields, formatMoney, formatTokens, formatValue } from "../format";
import type {
  ActionCatalogItem,
  ActionRules,
  ActionView,
  BillingProfile,
  Cap,
  OrgSettings,
  Plan,
  Subscription,
} from "../types";

export interface AdminPageProps {
  client: ChatApiClient;
  /** Start on a specific tab, e.g. "approvals" from a notification link. */
  initialTab?: TabId;
}

type TabId = "approvals" | "actions" | "limits" | "plan" | "profile" | "privacy";

const TABS: [TabId, string][] = [
  ["approvals", "Approvals"],
  ["actions", "Chat actions"],
  ["limits", "Usage limits"],
  ["plan", "Plan & spend"],
  ["profile", "Billing details"],
  ["privacy", "Data & privacy"],
];

const TIER_LABEL: Record<number, string> = { 1: "Low risk", 2: "Medium risk", 3: "High risk" };

/** Money inputs are in major units (e.g. rupees); the API stores minor units (paise). */
const toMinor = (v: string): number | null => (v.trim() === "" ? null : Math.round(Number(v) * 100));
const fromMinor = (v: number | null | undefined): string => (v == null ? "" : String(v / 100));

function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return "Only organisation admins can change this.";
    return e.message;
  }
  return "Something went wrong. Please try again.";
}

/** Load-once helper with error state. */
function useLoad<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    load()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(errorText(e)));
  }, [load]);
  useEffect(reload, [reload]);
  return { data, error, reload, setData };
}

function Status({ ok, error }: { ok?: string | null; error?: string | null }) {
  if (error)
    return (
      <p className="aos-error" role="alert">
        {error}
      </p>
    );
  if (ok)
    return (
      <p className="aos-ok" role="status">
        {ok}
      </p>
    );
  return null;
}

/** Label + control + hint. The hint is linked with aria-describedby, so screen readers announce the field
 * name on its own and the hint as a description. */
function Field({ label, hint, children }: { label: string; hint?: string; children: ReactElement }) {
  const id = useId();
  const hintId = `${id}-hint`;
  const control = cloneElement(children as ReactElement<Record<string, unknown>>, {
    id,
    "aria-describedby": hint ? hintId : undefined,
  });
  return (
    <div className="aos-field">
      <label className="aos-field__label" htmlFor={id}>
        {label}
      </label>
      {control}
      {hint && (
        <span id={hintId} className="aos-field__hint">
          {hint}
        </span>
      )}
    </div>
  );
}

/** Settings for organisation admins: approvals, chat actions, limits, plan, billing details, consent. */
export function AdminPage({ client, initialTab = "approvals" }: AdminPageProps) {
  const [tab, setTab] = useState<TabId>(initialTab);
  const settings = useLoad(useCallback(() => client.getOrgSettings(), [client]));

  if (settings.error) {
    return (
      <div className="aos-billing aos-admin">
        <h2>Assistant settings</h2>
        <p className="aos-muted">{settings.error}</p>
      </div>
    );
  }
  const currency = settings.data?.currency ?? "USD";

  return (
    <div className="aos-billing aos-admin">
      <h2>Assistant settings</h2>
      <div className="aos-tabs" role="tablist" aria-label="Settings sections">
        {TABS.map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            id={`aos-tab-${id}`}
            aria-selected={tab === id}
            aria-controls={`aos-panel-${id}`}
            className="aos-tab"
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <section role="tabpanel" id={`aos-panel-${tab}`} aria-labelledby={`aos-tab-${tab}`} className="aos-tabpanel">
        {tab === "approvals" && <Approvals client={client} />}
        {tab === "actions" && <ActionSettings client={client} currency={currency} />}
        {tab === "limits" && settings.data && <Limits client={client} settings={settings.data} />}
        {tab === "plan" && <PlanAndSpend client={client} />}
        {tab === "profile" && <BillingDetails client={client} />}
        {tab === "privacy" && settings.data && (
          <Privacy client={client} settings={settings.data} onChange={(s) => settings.setData(s)} />
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------- approvals

function Approvals({ client }: { client: ChatApiClient }) {
  const list = useLoad(useCallback(() => client.listActions("awaiting_second_approval"), [client]));
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok?: string; error?: string }>({});

  const act = async (a: ActionView, approve: boolean) => {
    setBusy(a.action_id);
    setMsg({});
    try {
      const view = approve ? await client.approveAction(a.action_id) : await client.rejectAction(a.action_id);
      setMsg({ ok: approve ? view.summary ?? "Approved." : "Rejected. Nothing was changed." });
      list.reload();
    } catch (e) {
      setMsg({ error: errorText(e) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <h3>Changes waiting for a second approver</h3>
      <p className="aos-muted">
        Large changes proposed in chat need a second person. You can't approve a change you requested or confirmed.
      </p>
      <Status ok={msg.ok} error={msg.error ?? list.error} />
      {list.data?.length === 0 && <p className="aos-muted">Nothing is waiting for approval.</p>}
      {list.data?.map((a) => {
        const fields = a.before && a.after ? changedFields(a.before, a.after) : [];
        return (
          <article key={a.action_id} className="aos-action" aria-label={`Approval request: ${a.label}`}>
            <header className="aos-action__head">
              <strong>{a.label}</strong>
              {a.risk_tier && <span className={`aos-tier aos-tier--${a.risk_tier}`}>{TIER_LABEL[a.risk_tier]}</span>}
            </header>
            <div className="aos-action__target">{String(a.before?.name ?? a.target_id ?? "")}</div>
            <table className="aos-action__diff">
              <tbody>
                {fields.map((f) => (
                  <tr key={f}>
                    <th scope="row">{f}</th>
                    <td>{formatValue(a.before?.[f])}</td>
                    <td className="aos-action__new">→ {formatValue(a.after?.[f])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {a.warnings?.map((w) => (
              <p key={w} className="aos-flag">
                ⚠ {w}
              </p>
            ))}
            <div className="aos-action__buttons">
              <button
                type="button"
                className="aos-btn aos-btn--primary"
                disabled={busy === a.action_id}
                onClick={() => void act(a, true)}
              >
                Approve and apply
              </button>
              <button type="button" className="aos-btn" disabled={busy === a.action_id} onClick={() => void act(a, false)}>
                Reject
              </button>
            </div>
          </article>
        );
      })}
    </>
  );
}

// ---------------------------------------------------------------- chat action settings

function ActionSettings({ client, currency }: { client: ChatApiClient; currency: string }) {
  const load = useCallback(() => Promise.all([client.getActionRules(), client.getActionCatalog()]), [client]);
  const state = useLoad(load);
  const [form, setForm] = useState<{
    enabled: boolean;
    pct: string;
    maxAbs: string;
    secondAbove: string;
    dailyCap: string;
    tier1Confirm: boolean;
    blocked: string[];
    roles: Record<string, string>;
  } | null>(null);
  const [msg, setMsg] = useState<{ ok?: string; error?: string }>({});

  useEffect(() => {
    if (!state.data) return;
    const [r] = state.data;
    setForm({
      enabled: r.write_actions_enabled,
      pct: String(r.max_budget_change_pct),
      maxAbs: fromMinor(r.max_budget_change_abs_minor),
      secondAbove: fromMinor(r.second_approval_above_minor),
      dailyCap: fromMinor(r.daily_spend_change_cap_minor),
      tier1Confirm: r.tier1_requires_confirmation,
      blocked: r.blocked_tools,
      roles: Object.fromEntries(["1", "2", "3"].map((t) => [t, (r.roles_by_tier[t] ?? []).join(", ")])),
    });
  }, [state.data]);

  if (state.error) return <Status error={state.error} />;
  if (!form || !state.data) return <p>Loading…</p>;
  const catalog: ActionCatalogItem[] = state.data[1];

  const save = async () => {
    setMsg({});
    const pct = Number(form.pct);
    if (!(pct > 0 && pct <= 100)) {
      setMsg({ error: "Maximum change per action must be between 0 and 100%." });
      return;
    }
    const rules: Partial<ActionRules> = {
      write_actions_enabled: form.enabled,
      max_budget_change_pct: pct,
      max_budget_change_abs_minor: toMinor(form.maxAbs),
      second_approval_above_minor: toMinor(form.secondAbove),
      daily_spend_change_cap_minor: toMinor(form.dailyCap),
      tier1_requires_confirmation: form.tier1Confirm,
      blocked_tools: form.blocked,
      roles_by_tier: Object.fromEntries(
        Object.entries(form.roles)
          .map(([t, v]) => [t, v.split(",").map((x) => x.trim()).filter(Boolean)] as const)
          .filter(([, v]) => v.length > 0),
      ),
    };
    try {
      await client.updateActionRules(rules);
      setMsg({ ok: "Saved. New rules apply to the next proposal." });
    } catch (e) {
      setMsg({ error: errorText(e) });
    }
  };

  const money = (label: string, key: "maxAbs" | "secondAbove" | "dailyCap", hint: string) => (
    <Field label={`${label} (${currency})`} hint={hint}>
      <input
        type="number"
        min="0"
        step="0.01"
        inputMode="decimal"
        value={form[key]}
        placeholder="No limit"
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
      />
    </Field>
  );

  return (
    <form
      className="aos-form"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <h3>Chat actions</h3>
      <label className="aos-check">
        <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
        <span>
          <strong>Let the assistant propose changes to campaigns</strong>
          <span className="aos-field__hint">
            Every change is shown as a card and only happens when a person confirms it.
          </span>
        </span>
      </label>
      <fieldset disabled={!form.enabled}>
        <legend>Budget safety limits</legend>
        <Field label="Maximum budget change per action (%)" hint="Larger changes are refused; the assistant suggests a smaller step.">
          <input type="number" min="1" max="100" step="1" value={form.pct} onChange={(e) => setForm({ ...form, pct: e.target.value })} />
        </Field>
        {money("Maximum budget change per action", "maxAbs", "Leave empty for no amount limit.")}
        {money("Second approver needed above", "secondAbove", "Changes above this amount need a second admin to approve.")}
        {money("Daily limit on budget changes made through chat", "dailyCap", "Total of all changes per day.")}
      </fieldset>
      <fieldset disabled={!form.enabled}>
        <legend>Which actions are allowed</legend>
        {catalog.map((a) => (
          <label key={a.name} className="aos-check">
            <input
              type="checkbox"
              checked={!form.blocked.includes(a.name)}
              onChange={(e) =>
                setForm({
                  ...form,
                  blocked: e.target.checked ? form.blocked.filter((b) => b !== a.name) : [...form.blocked, a.name],
                })
              }
            />
            <span>
              {a.label} <span className={`aos-tier aos-tier--${a.risk_tier}`}>{TIER_LABEL[a.risk_tier]}</span>
            </span>
          </label>
        ))}
        <label className="aos-check">
          <input
            type="checkbox"
            checked={form.tier1Confirm}
            onChange={(e) => setForm({ ...form, tier1Confirm: e.target.checked })}
          />
          <span>Ask for confirmation on low-risk actions too (recommended)</span>
        </label>
      </fieldset>
      <fieldset disabled={!form.enabled}>
        <legend>Who can make changes (optional)</legend>
        <p className="aos-field__hint">Comma-separated AgencyOS roles. Leave empty to allow everyone with access.</p>
        {["3", "2", "1"].map((t) => (
          <Field key={t} label={TIER_LABEL[Number(t)]}>
            <input
              value={form.roles[t]}
              placeholder="e.g. org_admin, senior_planner"
              onChange={(e) => setForm({ ...form, roles: { ...form.roles, [t]: e.target.value } })}
            />
          </Field>
        ))}
      </fieldset>
      <button type="submit" className="aos-btn aos-btn--primary">
        Save chat action settings
      </button>
      <Status ok={msg.ok} error={msg.error} />
    </form>
  );
}

// ---------------------------------------------------------------- usage limits

function Limits({ client, settings }: { client: ChatApiClient; settings: OrgSettings }) {
  const caps = useLoad(useCallback(() => client.listCaps(), [client]));
  const [form, setForm] = useState({ scope: "org" as Cap["scope_type"], scopeId: "", period: "month" as Cap["period"], max: "", hard: true });
  const [msg, setMsg] = useState<{ ok?: string; error?: string }>({});

  const add = async () => {
    setMsg({});
    const max = Math.round(Number(form.max));
    if (!(max > 0)) return setMsg({ error: "Enter a token limit greater than 0." });
    const scopeId = form.scope === "org" ? settings.org_id : form.scopeId.trim();
    if (!/^[0-9a-f-]{36}$/i.test(scopeId)) return setMsg({ error: "Enter the user or workspace ID." });
    try {
      await client.setCap({ scope_type: form.scope, scope_id: scopeId, period: form.period, max_tokens: max, hard: form.hard });
      setMsg({ ok: "Limit saved." });
      setForm({ ...form, max: "", scopeId: "" });
      caps.reload();
    } catch (e) {
      setMsg({ error: errorText(e) });
    }
  };

  const remove = async (cap: Cap) => {
    try {
      await client.deleteCap(cap.cap_id);
      caps.reload();
    } catch (e) {
      setMsg({ error: errorText(e) });
    }
  };

  const scopeLabel = (c: Cap) => (c.scope_type === "org" ? "Whole organisation" : `${c.scope_type === "user" ? "User" : "Workspace"} ${c.scope_id.slice(0, 8)}…`);

  return (
    <>
      <h3>Usage limits</h3>
      <p className="aos-muted">
        Limits stop the assistant once a token amount is used in a day or month. Admins get alerts at 50%, 80%, 90% and 100%.
      </p>
      <Status error={caps.error} />
      {caps.data && caps.data.length > 0 ? (
        <table className="aos-table">
          <thead>
            <tr>
              <th>Applies to</th>
              <th>Limit</th>
              <th>Type</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {caps.data.map((c) => (
              <tr key={c.cap_id}>
                <td>{scopeLabel(c)}</td>
                <td>
                  {formatTokens(c.max_tokens)} per {c.period}
                </td>
                <td>{c.hard ? "Stops the assistant" : "Warning only"}</td>
                <td>
                  <button type="button" className="aos-btn aos-btn--link" onClick={() => void remove(c)}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        caps.data && <p className="aos-muted">No limits set.</p>
      )}
      <form
        className="aos-form aos-form--inline"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <h4>Add a limit</h4>
        <Field label="Applies to">
          <select value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value as Cap["scope_type"] })}>
            <option value="org">Whole organisation</option>
            <option value="user">One user</option>
            <option value="workspace">One workspace</option>
          </select>
        </Field>
        {form.scope !== "org" && (
          <Field label={form.scope === "user" ? "User ID" : "Workspace ID"}>
            <input value={form.scopeId} onChange={(e) => setForm({ ...form, scopeId: e.target.value })} />
          </Field>
        )}
        <Field label="Tokens">
          <input type="number" min="1" step="1" value={form.max} onChange={(e) => setForm({ ...form, max: e.target.value })} />
        </Field>
        <Field label="Per">
          <select value={form.period} onChange={(e) => setForm({ ...form, period: e.target.value as Cap["period"] })}>
            <option value="day">Day</option>
            <option value="month">Month</option>
          </select>
        </Field>
        <Field label="When reached">
          <select value={form.hard ? "hard" : "soft"} onChange={(e) => setForm({ ...form, hard: e.target.value === "hard" })}>
            <option value="hard">Stop the assistant</option>
            <option value="soft">Warn only</option>
          </select>
        </Field>
        <button type="submit" className="aos-btn aos-btn--primary">
          Add limit
        </button>
      </form>
      <Status ok={msg.ok} error={msg.error} />
    </>
  );
}

// ---------------------------------------------------------------- plan & spend limit

function PlanAndSpend({ client }: { client: ChatApiClient }) {
  const load = useCallback(
    () =>
      Promise.all([client.getSubscription(), client.listPlans()]).then(
        ([s, p]) => [s.subscription, p] as [Subscription | null, Plan[]],
      ),
    [client],
  );
  const state = useLoad(load);
  const [limit, setLimit] = useState("");
  const [msg, setMsg] = useState<{ ok?: string; error?: string }>({});

  useEffect(() => {
    if (state.data?.[0]) setLimit(fromMinor(state.data[0].spend_limit_minor));
  }, [state.data]);

  if (state.error) return <Status error={state.error} />;
  if (!state.data) return <p>Loading…</p>;
  const [sub, plans] = state.data;
  const current = plans.find((p) => p.plan_id === sub?.plan_id);

  const choose = async (plan: Plan) => {
    setMsg({});
    try {
      if (!sub) {
        await client.subscribe(plan.plan_id);
        setMsg({ ok: `Subscribed to ${plan.name}. Your monthly allowance is available now.` });
      } else {
        const r = await client.updateSubscription({ plan_id: plan.plan_id });
        setMsg({
          ok:
            r.change === "downgrade_scheduled"
              ? `${plan.name} starts at your next renewal. You keep your current plan until then.`
              : `Upgraded to ${plan.name}. The extra allowance for this period is available now, and the price difference is prorated.`,
        });
      }
      state.reload();
    } catch (e) {
      setMsg({ error: errorText(e) });
    }
  };

  const saveLimit = async (remove: boolean) => {
    setMsg({});
    try {
      if (remove) await client.updateSubscription({ remove_spend_limit: true });
      else {
        const minor = toMinor(limit);
        if (minor == null || minor < 0) return setMsg({ error: "Enter an amount, or remove the limit." });
        await client.updateSubscription({ spend_limit_minor: minor });
      }
      setMsg({ ok: remove ? "Overage switched off." : "Spend limit saved." });
      state.reload();
    } catch (e) {
      setMsg({ error: errorText(e) });
    }
  };

  return (
    <>
      <h3>Plan</h3>
      {sub ? (
        <p>
          You're on <strong>{sub.plan_name}</strong> until {new Date(sub.current_period_end).toLocaleDateString()}.
          {sub.pending_plan_id && <> Changing to {plans.find((p) => p.plan_id === sub.pending_plan_id)?.name ?? sub.pending_plan_id} at renewal.</>}
        </p>
      ) : (
        <p className="aos-muted">No subscription. You're using prepaid tokens.</p>
      )}
      <div className="aos-packs">
        {plans.map((p) => (
          <div key={p.plan_id} className={`aos-pack ${p.plan_id === sub?.plan_id ? "is-current" : ""}`}>
            <strong>{p.name}</strong>
            <span>{formatMoney(p.monthly_fee_minor, p.currency)} / month</span>
            <span className="aos-muted">{formatTokens(p.monthly_included_tokens)} included</span>
            <span className="aos-muted">
              {p.overage_allowed && p.price_per_1m_tokens_minor
                ? `Then ${formatMoney(p.price_per_1m_tokens_minor, p.currency)} per 1M tokens`
                : "No overage"}
            </span>
            {p.plan_id === sub?.plan_id ? (
              <span className="aos-badge">Current plan</span>
            ) : (
              <button type="button" className="aos-btn aos-btn--primary" onClick={() => void choose(p)}>
                {!sub ? "Subscribe" : current && p.monthly_fee_minor < current.monthly_fee_minor ? "Downgrade" : "Upgrade"}
              </button>
            )}
          </div>
        ))}
      </div>
      {sub && current?.overage_allowed && (
        <form
          className="aos-form aos-form--inline"
          onSubmit={(e) => {
            e.preventDefault();
            void saveLimit(false);
          }}
        >
          <h4>Overage spend limit</h4>
          <p className="aos-field__hint">
            When your monthly allowance runs out, the assistant keeps working and bills overage, up to this amount per month.
            Without a limit, it stops when the allowance runs out.
          </p>
          <Field label={`Monthly limit (${sub.currency})`}>
            <input type="number" min="0" step="0.01" value={limit} placeholder="No overage" onChange={(e) => setLimit(e.target.value)} />
          </Field>
          <button type="submit" className="aos-btn aos-btn--primary">
            Save limit
          </button>
          {sub.spend_limit_minor != null && (
            <button type="button" className="aos-btn" onClick={() => void saveLimit(true)}>
              Switch off overage
            </button>
          )}
        </form>
      )}
      <Status ok={msg.ok} error={msg.error} />
    </>
  );
}

// ---------------------------------------------------------------- billing details

function BillingDetails({ client }: { client: ChatApiClient }) {
  const profile = useLoad(useCallback(() => client.getBillingProfile(), [client]));
  const [form, setForm] = useState({ legal_name: "", line1: "", city: "", postcode: "", country: "", tax_id: "", billing_email: "" });
  const [msg, setMsg] = useState<{ ok?: string; error?: string }>({});

  useEffect(() => {
    const p = profile.data;
    if (p)
      setForm({
        legal_name: p.legal_name,
        line1: p.address.line1 ?? "",
        city: p.address.city ?? "",
        postcode: p.address.postcode ?? "",
        country: p.country,
        tax_id: p.tax_id ?? "",
        billing_email: p.billing_email,
      });
  }, [profile.data]);

  const save = async () => {
    setMsg({});
    if (!/^[A-Za-z]{2}$/.test(form.country)) return setMsg({ error: "Country must be a 2-letter code, e.g. IN." });
    const body: BillingProfile = {
      legal_name: form.legal_name.trim(),
      address: { line1: form.line1, city: form.city, postcode: form.postcode },
      tax_id: form.tax_id.trim() || null,
      country: form.country.toUpperCase(),
      billing_email: form.billing_email.trim(),
    };
    try {
      await client.updateBillingProfile(body);
      setMsg({ ok: "Saved. Future invoices will use these details." });
    } catch (e) {
      setMsg({ error: errorText(e) });
    }
  };

  const input = (key: keyof typeof form, label: string, props: Record<string, unknown> = {}) => (
    <Field label={label}>
      <input value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} {...props} />
    </Field>
  );

  return (
    <form
      className="aos-form"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <h3>Billing details</h3>
      <p className="aos-muted">Shown on tax invoices. Changes apply to future invoices only.</p>
      <Status error={profile.error} />
      {input("legal_name", "Legal company name", { required: true, minLength: 2 })}
      {input("line1", "Address")}
      {input("city", "City")}
      {input("postcode", "Postcode")}
      {input("country", "Country (2-letter code)", { required: true, maxLength: 2 })}
      {input("tax_id", "Tax ID (e.g. GSTIN / VAT number)")}
      {input("billing_email", "Billing email", { required: true, type: "email" })}
      <button type="submit" className="aos-btn aos-btn--primary">
        Save billing details
      </button>
      <Status ok={msg.ok} error={msg.error} />
    </form>
  );
}

// ---------------------------------------------------------------- data & privacy

function Privacy({
  client,
  settings,
  onChange,
}: {
  client: ChatApiClient;
  settings: OrgSettings;
  onChange: (s: OrgSettings) => void;
}) {
  const [msg, setMsg] = useState<{ ok?: string; error?: string }>({});
  const toggle = async (consent: boolean) => {
    setMsg({});
    try {
      await client.setTrainingConsent(consent);
      onChange({ ...settings, training_consent: consent });
      setMsg({ ok: consent ? "Thanks. Consent recorded." : "Consent withdrawn. New conversations won't be used." });
    } catch (e) {
      setMsg({ error: errorText(e) });
    }
  };
  return (
    <>
      <h3>Data &amp; privacy</h3>
      <label className="aos-check">
        <input type="checkbox" checked={settings.training_consent} onChange={(e) => void toggle(e.target.checked)} />
        <span>
          <strong>Help improve the assistant with our conversations</strong>
          <span className="aos-field__hint">
            If you opt in, conversations, tool results and feedback may be used to train AgencyOS models after personal
            data and your organisation's identifiers are removed. Your data is never shown to other clients. You can
            withdraw at any time; withdrawal applies to future data and training runs.
          </span>
        </span>
      </label>
      <Status ok={msg.ok} error={msg.error} />
    </>
  );
}
