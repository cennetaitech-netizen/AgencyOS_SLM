// AgencyOS Assistant test site: testers sign in with an invite link (?invite=…); staff create links in "Testers".
import { StrictMode, useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import { AdminPage, AssistantPanel, BillingPage, ChatApiClient, FEEDBACK_REASONS } from "../src";
import "./pilot.css";

const TOKEN_KEY = "aos.invite";
const STAFF_ROLE = "agencyos_staff";
const ADMIN_ROLE = "org_admin";

type Me = { user_id: string; org_id: string; display_name: string; roles: string[] };
type Page = "chat" | "billing" | "settings" | "testers" | "feedback";
type CreatedInvite = { name: string; org_name: string; role: string; expires_at: string; link: string };

let memoryToken: string | null = null; // used when the browser blocks storage

function readToken(): string | null {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("invite");
  if (fromUrl) {
    memoryToken = fromUrl;
    try {
      localStorage.setItem(TOKEN_KEY, fromUrl);
    } catch {
      /* storage blocked: keep it for this visit only */
    }
    url.searchParams.delete("invite"); // don't leave the link in the address bar or history
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    return fromUrl;
  }
  try {
    return localStorage.getItem(TOKEN_KEY) ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

function forgetToken() {
  memoryToken = null;
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* nothing stored */
  }
}

/** The organisation's name, from the invite itself (display only; the server checks the signature). */
function orgNameOf(token: string): string {
  try {
    const body = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(body + "=".repeat((4 - (body.length % 4)) % 4))).org_name ?? "";
  } catch {
    return "";
  }
}

function Message({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <main className="pilot-message">
      <h1>{title}</h1>
      {children}
    </main>
  );
}

function Testers({ token }: { token: string }) {
  const [name, setName] = useState("");
  const [org, setOrg] = useState("");
  const [role, setRole] = useState("org_admin");
  const [days, setDays] = useState("30");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<CreatedInvite[]>([]);
  const [copied, setCopied] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const resp = await fetch("/chat/admin/invites", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ name, org_name: org, role, days: Number(days) }),
      });
      const body = await resp.json();
      if (!resp.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Couldn't create the link.");
      setCreated((c) => [{ ...body, link: window.location.origin + body.path }, ...c]);
      setName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't create the link.");
    } finally {
      setBusy(false);
    }
  }

  async function copy(link: string) {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(link);
    } catch {
      setCopied("");
    }
  }

  return (
    <section className="pilot-card">
      <h2>Invite testers</h2>
      <p className="pilot-muted">
        Each link signs in one person. People with the same organisation name share a balance, settings and chat
        approvals. Creating a link also switches the assistant on for that organisation.
      </p>
      <form className="pilot-form" onSubmit={submit}>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} />
        </label>
        <label>
          Organisation
          <input value={org} onChange={(e) => setOrg(e.target.value)} required maxLength={120} placeholder="e.g. Acme Media" />
        </label>
        <label>
          Role
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="org_admin">Org admin: chat, billing and settings</option>
            <option value="planner">Planner: chat and billing</option>
            <option value="staff">AgencyOS staff: everything, and can invite others</option>
          </select>
        </label>
        <label>
          Valid for (days)
          <input type="number" min={1} max={365} value={days} onChange={(e) => setDays(e.target.value)} required />
        </label>
        <button className="aos-btn aos-btn--primary" disabled={busy}>
          {busy ? "Creating…" : "Create link"}
        </button>
      </form>
      {error && <p className="pilot-error" role="alert">{error}</p>}
      {created.length > 0 && (
        <>
          <h3>Links created now</h3>
          <p className="pilot-muted">
            Anyone with a link can sign in as that tester, so send each one privately. Links aren't shown again after
            you leave this page.
          </p>
          <ul className="pilot-links">
            {created.map((c) => (
              <li key={c.link}>
                <div>
                  <strong>{c.name}</strong> · {c.org_name} · {c.role.replace("_", " ")} · until{" "}
                  {new Date(c.expires_at).toLocaleDateString()}
                </div>
                <div className="pilot-link-row">
                  <input readOnly value={c.link} aria-label={`Invite link for ${c.name}`} onFocus={(e) => e.target.select()} />
                  <button type="button" className="aos-btn" onClick={() => copy(c.link)}>
                    {copied === c.link ? "Copied" : "Copy"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

type ReviewItem = { message_id: string; created_at: string; question: string; answer: string; reason?: string | null; comment?: string | null; flags?: Record<string, unknown> | null };
type Review = { ratings: { up: number; down: number; up_pct: number | null }; down_rated: ReviewItem[]; flagged: ReviewItem[] };

const REASON_LABEL = Object.fromEntries(FEEDBACK_REASONS);
const SHOW = 20;

function isoDay(d: Date) {
  return d.toISOString().slice(0, 10);
}

function Feedback({ token }: { token: string }) {
  const [days, setDays] = useState(7);
  const [review, setReview] = useState<Review | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const to = new Date();
    const from = new Date(to.getTime() - (days - 1) * 86400000);
    setError("");
    fetch(`/chat/admin/quality-review?date_from=${isoDay(from)}&date_to=${isoDay(to)}&sample=0`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(String(r.status));
        setReview(await r.json());
      })
      .catch(() => setError("Couldn't load feedback."));
  }, [token, days]);

  const items = (list: ReviewItem[], showReason: boolean) => (
    <ul className="pilot-feedback">
      {list.slice(0, SHOW).map((it) => (
        <li key={it.message_id}>
          <div className="pilot-muted">{new Date(it.created_at).toLocaleString()}</div>
          <div><strong>Asked:</strong> {it.question || "–"}</div>
          <div><strong>Answer:</strong> {it.answer.length > 400 ? it.answer.slice(0, 400) + "…" : it.answer}</div>
          {showReason && (it.reason || it.comment) && (
            <div className="pilot-feedback-why">
              <strong>Tester said:</strong>{" "}
              {[it.reason && (REASON_LABEL[it.reason] ?? it.reason), it.comment].filter(Boolean).join(" · ")}
            </div>
          )}
          {!showReason && it.flags && <div className="pilot-muted">Check: {Object.keys(it.flags).join(", ")}</div>}
        </li>
      ))}
    </ul>
  );

  return (
    <section className="pilot-card">
      <h2>Feedback</h2>
      <label className="pilot-inline">
        Period{" "}
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>Today</option>
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
        </select>
      </label>
      {error && <p className="pilot-error" role="alert">{error}</p>}
      {review && (
        <>
          <p>
            👍 {review.ratings.up} · 👎 {review.ratings.down}
            {review.ratings.up_pct !== null && <span className="pilot-muted"> ({review.ratings.up_pct}% positive)</span>}
          </p>
          <h3>Answers rated 👎 ({review.down_rated.length}{review.down_rated.length > SHOW ? `, newest ${SHOW} shown` : ""})</h3>
          {review.down_rated.length ? items(review.down_rated, true) : <p className="pilot-muted">None.</p>}
          <h3>
            Answers the automatic checks flagged ({review.flagged.length >= 200 ? "200+" : review.flagged.length}
            {review.flagged.length > SHOW ? `, newest ${SHOW} shown` : ""})
          </h3>
          <p className="pilot-muted">For example, a figure in the answer that doesn't match the data the assistant looked up.</p>
          {review.flagged.length ? items(review.flagged, false) : <p className="pilot-muted">None.</p>}
        </>
      )}
    </section>
  );
}

function App() {
  const [token, setToken] = useState<string | null>(() => readToken());
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<"checking" | "ready" | "invalid" | "offline">("checking");
  const [page, setPage] = useState<Page>("chat");

  const client = useMemo(
    () => new ChatApiClient({ baseUrl: "", getHeaders: (): Record<string, string> => (token ? { Authorization: `Bearer ${token}` } : {}) }),
    [token],
  );

  const check = useCallback(async () => {
    if (!token) return;
    setState("checking");
    try {
      const resp = await fetch("/chat/me", { headers: { Authorization: `Bearer ${token}` } });
      if (resp.status === 401) {
        setState("invalid");
        return;
      }
      if (!resp.ok) throw new Error(String(resp.status));
      setMe(await resp.json());
      setState("ready");
    } catch {
      setState("offline");
    }
  }, [token]);

  useEffect(() => {
    void check();
  }, [check]);

  const signOut = () => {
    forgetToken();
    setToken(null);
    setMe(null);
  };

  if (!token) {
    return (
      <Message title="AgencyOS Assistant test site">
        <p>Open the invite link you were sent to sign in. Each link is personal.</p>
        <p className="pilot-muted">No link? Ask the AgencyOS team for one.</p>
      </Message>
    );
  }
  if (state === "invalid") {
    return (
      <Message title="This invite link doesn't work">
        <p>It has expired or been withdrawn. Ask the AgencyOS team for a new one.</p>
        <button className="aos-btn" onClick={signOut}>
          Forget this link
        </button>
      </Message>
    );
  }
  if (state === "offline") {
    return (
      <Message title="Can't reach the assistant">
        <p>The test site may be starting up. This can take a minute.</p>
        <button className="aos-btn aos-btn--primary" onClick={() => void check()}>
          Try again
        </button>
      </Message>
    );
  }
  if (state === "checking" || !me) return <Message title="Signing you in…">{null}</Message>;

  const isStaff = me.roles.includes(STAFF_ROLE);
  const canSettings = isStaff || me.roles.includes(ADMIN_ROLE);
  const tabs: [Page, string][] = [
    ["chat", "Chat"],
    ["billing", "Billing"],
    ...(canSettings ? ([["settings", "Settings"]] as [Page, string][]) : []),
    ...(isStaff ? ([["testers", "Testers"], ["feedback", "Feedback"]] as [Page, string][]) : []),
  ];
  const orgName = orgNameOf(token);

  return (
    <div className="pilot">
      <header className="pilot-header">
        <div className="pilot-brand">
          AgencyOS Assistant <span className="pilot-badge">Test</span>
        </div>
        <nav className="pilot-tabs" aria-label="Sections">
          {tabs.map(([id, label]) => (
            <button
              key={id}
              className={page === id ? "is-active" : undefined}
              aria-current={page === id ? "page" : undefined}
              onClick={() => setPage(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="pilot-user">
          <span>
            {me.display_name}
            {orgName && <span className="pilot-muted"> · {orgName}</span>}
          </span>
          <button className="aos-btn aos-btn--link" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>
      <p className="pilot-banner">
        Test version: campaign figures are sample data, not your real accounts. Rate answers with 👍 / 👎 to tell us
        what worked.
      </p>
      <main className={page === "chat" ? "pilot-main pilot-main--chat" : "pilot-main"}>
        {page === "chat" && <AssistantPanel key={me.user_id} client={client} onBuyTokens={() => setPage("billing")} />}
        {page === "billing" && <BillingPage key={me.user_id} client={client} />}
        {page === "settings" && <AdminPage key={me.user_id} client={client} />}
        {page === "testers" && <Testers token={token} />}
        {page === "feedback" && <Feedback token={token} />}
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
