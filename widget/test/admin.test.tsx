import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AdminPage } from "../src/components/AdminPage";
import { fakeApi, json } from "./fakeApi";

const ORG = "11111111-1111-1111-1111-111111111111";
const SETTINGS = { org_id: ORG, chat_enabled: true, billing_mode: "live", timezone: "Asia/Kolkata", currency: "INR", training_consent: false };
const RULES = {
  write_actions_enabled: false,
  max_budget_change_pct: 20,
  max_budget_change_abs_minor: null,
  second_approval_above_minor: null,
  daily_spend_change_cap_minor: null,
  tier1_requires_confirmation: true,
  roles_by_tier: {},
  blocked_tools: [],
};
const CATALOG = [
  { name: "update_campaign_budget", label: "Change campaign budget", risk_tier: 3, reversible: true },
  { name: "pause_campaign", label: "Pause campaign", risk_tier: 2, reversible: true },
  { name: "rename_campaign", label: "Rename campaign", risk_tier: 1, reversible: true },
];

const base = { "GET /chat/settings": SETTINGS, "GET /chat/actions?status=awaiting_second_approval": { actions: [] } };

async function openTab(name: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("tab", { name }));
  return user;
}

describe("AdminPage", () => {
  it("approves a change waiting for a second approver", async () => {
    const pending = {
      action_id: "a1", tool: "update_campaign_budget", label: "Change campaign budget", status: "awaiting_second_approval",
      summary: null, error: null, undo_available: false, risk_tier: 3, target_id: "cmp_101",
      before: { name: "Acme Diwali Search", budget: 300000 }, after: { name: "Acme Diwali Search", budget: 360000 },
      warnings: ["needs a second approver"], requested_by: "u1",
    };
    let approved = false;
    const { client, calls } = fakeApi({
      ...base,
      "GET /chat/actions?status=awaiting_second_approval": () => ({ actions: approved ? [] : [pending] }),
      "POST /chat/actions/a1/approve": () => {
        approved = true;
        return { ...pending, status: "executed", summary: "Done: Budget for Acme Diwali Search changed from 300,000 to 360,000" };
      },
    });
    render(<AdminPage client={client} />);
    const card = await screen.findByRole("article", { name: "Approval request: Change campaign budget" });
    expect(within(card).getByText("→ 360,000")).toBeInTheDocument();
    await userEvent.setup().click(within(card).getByRole("button", { name: "Approve and apply" }));
    expect(await screen.findByText(/changed from 300,000 to 360,000/)).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/chat/actions/a1/approve")).toBe(true);
    expect(await screen.findByText("Nothing is waiting for approval.")).toBeInTheDocument();
  });

  it("saves chat action rules with money in minor units and blocked actions", async () => {
    const { client, calls } = fakeApi({
      ...base,
      "GET /chat/action-rules": RULES,
      "GET /chat/action-catalog": { actions: CATALOG },
      "PUT /chat/action-rules": (b: unknown) => ({ ...RULES, ...(b as object) }),
    });
    render(<AdminPage client={client} />);
    const user = await openTab("Chat actions");
    const enable = await screen.findByRole("checkbox", { name: /Let the assistant propose changes/ });
    expect(screen.getByLabelText("Maximum budget change per action (%)")).toBeDisabled();
    await user.click(enable);
    const pct = screen.getByLabelText("Maximum budget change per action (%)");
    await user.clear(pct);
    await user.type(pct, "15");
    await user.type(screen.getByLabelText("Second approver needed above (INR)"), "50000");
    await user.click(screen.getByRole("checkbox", { name: /Rename campaign/ }));
    await user.type(screen.getByLabelText("High risk"), "org_admin, senior_planner");
    await user.click(screen.getByRole("button", { name: "Save chat action settings" }));
    expect(await screen.findByText(/Saved\. New rules apply/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
      write_actions_enabled: true,
      max_budget_change_pct: 15,
      max_budget_change_abs_minor: null,
      second_approval_above_minor: 5000000,
      daily_spend_change_cap_minor: null,
      tier1_requires_confirmation: true,
      blocked_tools: ["rename_campaign"],
      roles_by_tier: { "3": ["org_admin", "senior_planner"] },
    });
  });

  it("rejects an out-of-range percentage before sending", async () => {
    const { client, calls } = fakeApi({
      ...base,
      "GET /chat/action-rules": { ...RULES, write_actions_enabled: true },
      "GET /chat/action-catalog": { actions: CATALOG },
    });
    render(<AdminPage client={client} />);
    const user = await openTab("Chat actions");
    const pct = await screen.findByLabelText("Maximum budget change per action (%)");
    await user.clear(pct);
    await user.type(pct, "0");
    await user.click(screen.getByRole("button", { name: "Save chat action settings" }));
    expect(pct).toBeInvalid(); // the browser's own validation blocks the form (min 1, max 100)
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("adds an organisation-wide limit and removes it", async () => {
    let caps: unknown[] = [];
    const { client, calls } = fakeApi({
      ...base,
      "GET /chat/caps": () => ({ caps }),
      "PUT /chat/caps": (b: { max_tokens: number }) => {
        caps = [{ cap_id: "c1", ...(b as object) }];
        return { cap_id: "c1" };
      },
      "DELETE /chat/caps/c1": () => {
        caps = [];
        return json(null, 204);
      },
    });
    render(<AdminPage client={client} />);
    const user = await openTab("Usage limits");
    await user.type(await screen.findByLabelText("Tokens"), "5000000");
    await user.click(screen.getByRole("button", { name: "Add limit" }));
    expect(await screen.findByText("5,000,000 tokens per month")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
      scope_type: "org", scope_id: ORG, period: "month", max_tokens: 5000000, hard: true,
    });
    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(await screen.findByText("No limits set.")).toBeInTheDocument();
  });

  it("explains upgrades and scheduled downgrades", async () => {
    const plans = [
      { plan_id: "starter", name: "Starter", monthly_fee_minor: 499900, currency: "INR", monthly_included_tokens: 2000000, overage_allowed: false, price_per_1m_tokens_minor: null },
      { plan_id: "pro", name: "Pro", monthly_fee_minor: 1999900, currency: "INR", monthly_included_tokens: 10000000, overage_allowed: true, price_per_1m_tokens_minor: 100000 },
    ];
    const sub = { plan_id: "pro", plan_name: "Pro", status: "active", current_period_start: "2026-11-22T00:00:00Z", current_period_end: "2026-12-22T00:00:00Z", spend_limit_minor: 500000, pending_plan_id: null, currency: "INR", monthly_fee_minor: 1999900, monthly_included_tokens: 10000000 };
    const { client, calls } = fakeApi({
      ...base,
      "GET /chat/billing/subscription": { subscription: sub, capacity_tokens: 1 },
      "GET /chat/plans": { plans },
      "PUT /chat/billing/subscription": (b: { plan_id?: string }) => ({
        change: b.plan_id === "starter" ? "downgrade_scheduled" : undefined, subscription: sub,
      }),
    });
    render(<AdminPage client={client} />);
    const user = await openTab("Plan & spend");
    expect(await screen.findByText("Current plan")).toBeInTheDocument();
    expect(screen.getByLabelText("Monthly limit (INR)")).toHaveValue(5000);
    await user.click(screen.getByRole("button", { name: "Downgrade" }));
    expect(await screen.findByText(/Starter starts at your next renewal/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Switch off overage" }));
    await waitFor(() => expect(calls.some((c) => (c.body as { remove_spend_limit?: boolean })?.remove_spend_limit)).toBe(true));
  });

  it("validates and saves billing details", async () => {
    const { client, calls } = fakeApi({ ...base, "GET /chat/billing/profile": { profile: null }, "PUT /chat/billing/profile": { ok: true } });
    render(<AdminPage client={client} />);
    const user = await openTab("Billing details");
    await user.type(await screen.findByLabelText("Legal company name"), "Acme Foods Pvt Ltd");
    await user.type(screen.getByLabelText("Country (2-letter code)"), "i");
    await user.type(screen.getByLabelText("Billing email"), "billing@acme.example");
    await user.click(screen.getByRole("button", { name: "Save billing details" }));
    expect(await screen.findByText("Country must be a 2-letter code, e.g. IN.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Country (2-letter code)"), "n");
    await user.type(screen.getByLabelText("Tax ID (e.g. GSTIN / VAT number)"), "27ABCDE1234F1Z5");
    await user.click(screen.getByRole("button", { name: "Save billing details" }));
    expect(await screen.findByText(/Future invoices will use these details/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toMatchObject({ country: "IN", tax_id: "27ABCDE1234F1Z5" });
  });

  it("records training consent", async () => {
    const { client, calls } = fakeApi({ ...base, "PUT /chat/settings/training-consent": { training_consent: true } });
    render(<AdminPage client={client} />);
    const user = await openTab("Data & privacy");
    const box = await screen.findByRole("checkbox", { name: /Help improve the assistant/ });
    expect(box).not.toBeChecked();
    await user.click(box);
    expect(await screen.findByText("Thanks. Consent recorded.")).toBeInTheDocument();
    expect(box).toBeChecked();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ consent: true });
  });

  it("tells non-admins the page is for admins", async () => {
    const { client } = fakeApi({ "GET /chat/settings": () => json({ detail: "insufficient role" }, 403) });
    render(<AdminPage client={client} />);
    expect(await screen.findByText("Only organisation admins can change this.")).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).toBeNull();
  });
});
