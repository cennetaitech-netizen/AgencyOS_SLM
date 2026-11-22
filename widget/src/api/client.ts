/**
 * Thin client for the AgencyOS Assistant API. The host app supplies auth via `getHeaders`
 * (e.g. `{ Authorization: "Bearer <token>" }`); in local dev the demo uses the X-Dev-* headers.
 */
import { readSSE } from "./sse";
import type {
  ActionCatalogItem,
  ActionProposal,
  ActionRules,
  BillingProfile,
  OrgSettings,
  Plan,
  ActionView,
  Cap,
  Conversation,
  Invoice,
  Pack,
  StoredMessage,
  StreamEvent,
  Subscription,
  Wallet,
} from "../types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : (detail as { message?: string })?.message ?? `HTTP ${status}`);
  }
}

export interface ClientOptions {
  baseUrl: string;
  getHeaders?: () => Record<string, string> | Promise<Record<string, string>>;
  fetch?: typeof fetch;
}

export class ChatApiClient {
  private baseUrl: string;
  private getHeaders: NonNullable<ClientOptions["getHeaders"]>;
  private fetchImpl: typeof fetch;

  constructor(opts: ClientOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/$/, "");
    this.getHeaders = opts.getHeaders ?? (() => ({}));
    this.fetchImpl = opts.fetch ?? ((...args) => fetch(...args));
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = { ...(await this.getHeaders()) };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!resp.ok) {
      let detail: unknown = resp.statusText;
      try {
        detail = (await resp.json()).detail ?? detail;
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(resp.status, detail);
    }
    if (resp.status === 204) return undefined as T;
    const type = resp.headers.get("content-type") ?? "";
    return (type.includes("application/json") ? resp.json() : resp.text()) as Promise<T>;
  }

  // ---------------------------------------------------------------- conversations

  listConversations() {
    return this.request<{ conversations: Conversation[] }>("GET", "/chat/conversations").then((r) => r.conversations);
  }
  createConversation() {
    return this.request<{ id: string }>("POST", "/chat/conversations", {}).then((r) => r.id);
  }
  renameConversation(id: string, title: string) {
    return this.request("PATCH", `/chat/conversations/${id}`, { title });
  }
  deleteConversation(id: string) {
    return this.request<void>("DELETE", `/chat/conversations/${id}`);
  }
  getMessages(id: string) {
    return this.request<{ messages: StoredMessage[] }>("GET", `/chat/conversations/${id}/messages`).then(
      (r) => r.messages,
    );
  }

  /** Streams the reply. Abort with `signal` (the server then charges only text already sent). */
  async *sendMessage(conversationId: string, text: string, signal?: AbortSignal): AsyncGenerator<StreamEvent> {
    const resp = await this.fetchImpl(`${this.baseUrl}/chat/conversations/${conversationId}/messages`, {
      method: "POST",
      headers: { ...(await this.getHeaders()), "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ text }),
      signal,
    });
    if (!resp.ok || !resp.body) throw new ApiError(resp.status, resp.statusText);
    yield* readSSE(resp.body);
  }

  sendFeedback(messageId: string, rating: "up" | "down", reason?: string, comment?: string) {
    return this.request<void>("POST", `/chat/messages/${messageId}/feedback`, { rating, reason, comment });
  }

  // ---------------------------------------------------------------- actions

  confirmAction(id: string) {
    return this.request<ActionView>("POST", `/chat/actions/${id}/confirm`);
  }
  cancelAction(id: string) {
    return this.request<ActionView>("POST", `/chat/actions/${id}/cancel`);
  }
  undoAction(id: string) {
    return this.request<{ proposal: ActionProposal }>("POST", `/chat/actions/${id}/undo`).then((r) => r.proposal);
  }

  // ---------------------------------------------------------------- wallet & billing

  getWallet() {
    return this.request<Wallet>("GET", "/chat/wallet");
  }
  listPacks() {
    return this.request<{ packs: Pack[] }>("GET", "/chat/packs").then((r) => r.packs);
  }
  checkout(packId: string) {
    return this.request<{ checkout_url: string }>("POST", "/chat/billing/checkout", { pack_id: packId });
  }
  getSubscription() {
    return this.request<{ subscription: Subscription | null; capacity_tokens: number }>(
      "GET",
      "/chat/billing/subscription",
    );
  }
  listInvoices() {
    return this.request<{ invoices: Invoice[] }>("GET", "/chat/billing/invoices").then((r) => r.invoices);
  }
  invoiceUsageCsv(id: string) {
    return this.request<string>("GET", `/chat/billing/invoices/${id}/usage.csv`);
  }
  listCaps() {
    return this.request<{ caps: Cap[] }>("GET", "/chat/caps").then((r) => r.caps);
  }

  // ---------------------------------------------------------------- org admin

  setCap(cap: Omit<Cap, "cap_id">) {
    return this.request<{ cap_id: string }>("PUT", "/chat/caps", cap);
  }
  deleteCap(capId: string) {
    return this.request<void>("DELETE", `/chat/caps/${capId}`);
  }
  getOrgSettings() {
    return this.request<OrgSettings>("GET", "/chat/settings");
  }
  setTrainingConsent(consent: boolean) {
    return this.request<{ training_consent: boolean }>("PUT", "/chat/settings/training-consent", { consent });
  }
  getActionCatalog() {
    return this.request<{ actions: ActionCatalogItem[] }>("GET", "/chat/action-catalog").then((r) => r.actions);
  }
  getActionRules() {
    return this.request<ActionRules>("GET", "/chat/action-rules");
  }
  updateActionRules(rules: Partial<ActionRules>) {
    return this.request<ActionRules>("PUT", "/chat/action-rules", rules);
  }
  listActions(status?: string) {
    const q = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.request<{ actions: ActionView[] }>("GET", `/chat/actions${q}`).then((r) => r.actions);
  }
  approveAction(id: string) {
    return this.request<ActionView>("POST", `/chat/actions/${id}/approve`);
  }
  rejectAction(id: string, reason?: string) {
    return this.request<ActionView>("POST", `/chat/actions/${id}/reject`, { reason });
  }
  listPlans() {
    return this.request<{ plans: Plan[] }>("GET", "/chat/plans").then((r) => r.plans);
  }
  subscribe(planId: string, spendLimitMinor?: number | null) {
    return this.request<{ invoice_id: string | null }>("POST", "/chat/billing/subscription", {
      plan_id: planId,
      spend_limit_minor: spendLimitMinor ?? null,
    });
  }
  updateSubscription(body: { plan_id?: string; spend_limit_minor?: number; remove_spend_limit?: boolean }) {
    return this.request<{ change?: string; subscription: Subscription }>("PUT", "/chat/billing/subscription", body);
  }
  getBillingProfile() {
    return this.request<{ profile: BillingProfile | null }>("GET", "/chat/billing/profile").then((r) => r.profile);
  }
  updateBillingProfile(profile: BillingProfile) {
    return this.request("PUT", "/chat/billing/profile", profile);
  }
}
