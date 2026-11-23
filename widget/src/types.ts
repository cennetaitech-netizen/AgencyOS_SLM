/** Types mirroring the chat service contract (docs/contracts/CONTRACTS.md). */

export type BillingMode = "shadow" | "live";

export interface Conversation {
  id: string;
  title: string | null;
  updated_at: string;
}

export interface StoredMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  tokens_charged: number | null;
  created_at: string;
}

export interface Bucket {
  source: "free_trial" | "plan_monthly" | "pack" | "goodwill";
  remaining: number;
  expires_at: string | null;
}

export interface Wallet {
  tokens_available: number;
  tokens_reserved: number;
  spendable: number;
  low_balance: boolean;
  billing_mode: BillingMode;
  buckets: Bucket[];
}

export interface UsageAlert {
  scope: string;
  period: string;
  threshold: number;
  used: number;
  limit: number;
}

export interface MessageEnd {
  message_id: string;
  billable_input_tokens: number;
  billable_output_tokens: number;
  tokens_charged: number;
  billing_mode: BillingMode;
  balance_after: number;
  low_balance: boolean;
  alerts?: UsageAlert[];
  quality_flags?: { ungrounded_numbers?: string[] };
}

export interface ChatError {
  code: string;
  message: string;
  charged: false;
}

export type ActionState = Record<string, unknown>;

export interface ActionProposal {
  action_id: string;
  tool: string;
  label: string;
  risk_tier: 1 | 2 | 3;
  target_id: string | null;
  before: ActionState;
  after: ActionState;
  warnings: string[];
  requires_second_approval: boolean;
  expires_at: string;
  undo_of: string | null;
}

export interface ActionView {
  action_id: string;
  tool: string;
  label: string;
  status:
    | "proposed"
    | "awaiting_second_approval"
    | "confirmed"
    | "executing"
    | "executed"
    | "failed"
    | "cancelled"
    | "expired"
    | "rejected"
    | "undone";
  summary: string | null;
  error: string | null;
  undo_available: boolean;
  risk_tier?: 1 | 2 | 3;
  target_id?: string | null;
  before?: ActionState | null;
  after?: ActionState | null;
  warnings?: string[];
  requested_by?: string;
  confirmed_by?: string | null;
}

export type StreamEvent =
  | { event: "message.start"; data: { message_id: string; conversation_id: string } }
  | { event: "tool.start"; data: { tool: string; label: string } }
  | { event: "tool.end"; data: { tool: string; ok: boolean } }
  | { event: "token"; data: { text: string } }
  | { event: "action.proposed"; data: ActionProposal }
  | { event: "action.executed"; data: ActionView }
  | { event: "message.end"; data: MessageEnd }
  | { event: "heartbeat"; data: Record<string, never> }
  | { event: "error"; data: ChatError };

export interface Pack {
  pack_id: string;
  tokens: number;
  price_minor: number;
  currency: string;
  validity_days: number;
}

export interface CheckoutResult {
  /** Payment provider's page; null with manual billing (pay by invoice / bank transfer instead). */
  checkout_url: string | null;
  provider_payment_id: string;
  manual?: boolean;
  instructions?: string | null;
  amount_minor?: number;
  currency?: string;
}

export interface PackOrder {
  reference: string;
  pack_id: string;
  provider: string;
  amount_minor: number;
  currency: string;
  status: "pending" | "paid" | "refunded" | "disputed" | "failed";
  created_at: string;
}

export interface Invoice {
  invoice_id: string;
  invoice_number: string | null;
  kind: string;
  status: string;
  currency: string;
  period_start: string;
  period_end: string;
  total_minor: number;
}

export interface Subscription {
  plan_id: string;
  plan_name: string;
  status: string;
  current_period_start: string;
  current_period_end: string;
  spend_limit_minor: number | null;
  pending_plan_id: string | null;
  currency: string;
  monthly_fee_minor: number;
  monthly_included_tokens: number;
}

export interface Cap {
  cap_id: string;
  scope_type: "org" | "workspace" | "user";
  scope_id: string;
  period: "day" | "month";
  max_tokens: number;
  hard: boolean;
}

export interface OrgSettings {
  org_id: string;
  chat_enabled: boolean;
  billing_mode: BillingMode;
  timezone: string;
  currency: string;
  training_consent: boolean;
}

export interface ActionCatalogItem {
  name: string;
  label: string;
  risk_tier: 1 | 2 | 3;
  reversible: boolean;
}

export interface ActionRules {
  write_actions_enabled: boolean;
  max_budget_change_pct: number;
  max_budget_change_abs_minor: number | null;
  second_approval_above_minor: number | null;
  daily_spend_change_cap_minor: number | null;
  tier1_requires_confirmation: boolean;
  roles_by_tier: Record<string, string[]>;
  blocked_tools: string[];
}

export interface Plan {
  plan_id: string;
  name: string;
  monthly_fee_minor: number;
  currency: string;
  monthly_included_tokens: number;
  overage_allowed: boolean;
  price_per_1m_tokens_minor: number | null;
}

export interface BillingProfile {
  legal_name: string;
  address: Record<string, string>;
  tax_id: string | null;
  country: string;
  billing_email: string;
}
