import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AssistantPanel } from "../src/components/AssistantPanel";
import { BillingPage } from "../src/components/BillingPage";
import { WALLET, fakeApi, json, sseResponse } from "./fakeApi";

const base = {
  "GET /chat/conversations": { conversations: [] },
  "GET /chat/wallet": WALLET,
  "POST /chat/conversations": { id: "conv-1" },
};

const start: [string, unknown] = ["message.start", { message_id: "msg-1", conversation_id: "conv-1" }];
const end = (charged: number, mode = "live"): [string, unknown] => [
  "message.end",
  {
    message_id: "msg-1",
    billable_input_tokens: 10,
    billable_output_tokens: charged - 10,
    tokens_charged: charged,
    billing_mode: mode,
    balance_after: 200000 - charged,
    low_balance: false,
    alerts: [],
    quality_flags: {},
  },
];

async function ask(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Message the assistant"), `${text}{Enter}`);
  return user;
}

describe("AssistantPanel", () => {
  it("streams an answer, shows the token count and sends feedback", async () => {
    const { client, calls } = fakeApi({
      ...base,
      "POST /chat/conversations/conv-1/messages": () =>
        sseResponse([
          start,
          ["tool.start", { tool: "get_campaign_performance", label: "Looking up campaign performance" }],
          ["tool.end", { tool: "get_campaign_performance", ok: true }],
          ["token", { text: "Acme spent **₹1,20,000** " }],
          ["token", { text: "last week." }],
          end(352),
        ]),
      "POST /chat/messages/msg-1/feedback": () => json(null, 204),
    });
    render(<AssistantPanel client={client} />);
    const user = await ask("How did Acme do?");
    expect(await screen.findByText("352 tokens")).toBeInTheDocument();
    expect(screen.getByText("₹1,20,000").tagName).toBe("STRONG");
    expect(screen.getByRole("log")).toHaveTextContent("Acme spent ₹1,20,000 last week.");
    expect(calls.find((c) => c.path.endsWith("/messages"))?.headers.Authorization).toBe("Bearer test");

    await user.click(screen.getByLabelText("Good answer"));
    await waitFor(() => expect(screen.getByText("Thanks for the feedback")).toBeInTheDocument());
    expect(calls.find((c) => c.path === "/chat/messages/msg-1/feedback")?.body).toMatchObject({ rating: "up" });
  });

  it("never renders raw HTML or javascript: links from the model", async () => {
    const { client } = fakeApi({
      ...base,
      "POST /chat/conversations/conv-1/messages": () =>
        sseResponse([
          start,
          ["token", { text: 'Hi <script>window.hacked=1</script><img src=x onerror="alert(1)"> [click](javascript:alert(1)) [docs](https://example.com)' }],
          end(20),
        ]),
    });
    const { container } = render(<AssistantPanel client={client} />);
    await ask("hello");
    await screen.findByText("20 tokens");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.queryByRole("link", { name: "click" })).toBeNull();
    const docs = screen.getByRole("link", { name: "docs" });
    expect(docs).toHaveAttribute("href", "https://example.com");
    expect(docs).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("labels shadow-mode replies as free", async () => {
    const { client } = fakeApi({
      ...base,
      "GET /chat/wallet": { ...WALLET, billing_mode: "shadow" },
      "POST /chat/conversations/conv-1/messages": () => sseResponse([start, ["token", { text: "Hi" }], end(30, "shadow")]),
    });
    render(<AssistantPanel client={client} />);
    expect(await screen.findByText("Free during pilot")).toBeInTheDocument();
    await ask("hi");
    expect(await screen.findByText("30 tokens · free during pilot")).toBeInTheDocument();
  });

  it("blocks sending when tokens run out and offers to buy more", async () => {
    const onBuy = vi.fn();
    const { client } = fakeApi({
      ...base,
      "POST /chat/conversations/conv-1/messages": () =>
        sseResponse([["error", { code: "insufficient_tokens", message: "You've used all your tokens.", charged: false }]]),
    });
    render(<AssistantPanel client={client} onBuyTokens={onBuy} />);
    const user = await ask("hello");
    const buy = await screen.findByRole("button", { name: "Buy tokens" });
    expect(screen.getAllByText("You've used all your tokens.")).toHaveLength(2); // in the reply and the banner
    expect(screen.getByLabelText("Message the assistant")).toBeDisabled();
    await user.click(buy);
    expect(onBuy).toHaveBeenCalled();
  });

  it("shows an action card and only changes things through Confirm, with Undo", async () => {
    const proposal = {
      action_id: "act-1",
      tool: "update_campaign_budget",
      label: "Change campaign budget",
      risk_tier: 3,
      target_id: "cmp_101",
      before: { name: "Acme Diwali Search", status: "active", budget: 300000 },
      after: { name: "Acme Diwali Search", status: "active", budget: 330000 },
      warnings: ["large change (10.0%)"],
      requires_second_approval: false,
      expires_at: "2026-11-22T12:10:00Z",
      undo_of: null,
    };
    const { client, calls } = fakeApi({
      ...base,
      "POST /chat/conversations/conv-1/messages": () =>
        sseResponse([start, ["action.proposed", proposal], ["token", { text: "Please confirm the card." }], end(40)]),
      "POST /chat/actions/act-1/confirm": {
        action_id: "act-1",
        tool: "update_campaign_budget",
        label: "Change campaign budget",
        status: "executed",
        summary: "Done: Budget for Acme Diwali Search changed from 300,000 to 330,000",
        error: null,
        undo_available: true,
      },
      "POST /chat/actions/act-1/undo": { proposal: { ...proposal, action_id: "act-2", before: proposal.after, after: proposal.before, undo_of: "act-1" } },
    });
    render(<AssistantPanel client={client} />);
    const user = await ask("Raise cmp_101 budget by 10%");
    const card = await screen.findByRole("region", { name: "Proposed change: Change campaign budget" });
    expect(within(card).getByText("300,000")).toBeInTheDocument();
    expect(within(card).getByText("330,000")).toBeInTheDocument();
    expect(within(card).getByText("High risk")).toBeInTheDocument();
    expect(within(card).getByText("⚠ large change (10.0%)")).toBeInTheDocument();
    expect(calls.some((c) => c.path.includes("/actions/"))).toBe(false);

    await user.click(within(card).getByRole("button", { name: "Confirm" }));
    expect(await within(card).findByText(/Budget for Acme Diwali Search changed/)).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Undo" }));
    expect(await screen.findByText("Undo: Change campaign budget")).toBeInTheDocument();
    expect(calls.filter((c) => c.path.startsWith("/chat/actions/")).map((c) => c.path)).toEqual([
      "/chat/actions/act-1/confirm",
      "/chat/actions/act-1/undo",
    ]);
  });

  it("shows the AI disclaimer and starter questions", async () => {
    const { client, calls } = fakeApi({
      ...base,
      "POST /chat/conversations/conv-1/messages": () => sseResponse([start, ["token", { text: "ok" }], end(12)]),
    });
    render(<AssistantPanel client={client} starterPrompts={["Show paused campaigns"]} />);
    expect(screen.getByText("AI answers can be wrong. Check important figures before acting.")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Show paused campaigns" }));
    await screen.findByText("12 tokens");
    expect(calls.find((c) => c.path.endsWith("/messages"))?.body).toEqual({ text: "Show paused campaigns" });
  });
});

describe("BillingPage", () => {
  it("shows balance, packs, plan and invoices for admins", async () => {
    const onCheckout = vi.fn();
    const { client } = fakeApi({
      "GET /chat/wallet": WALLET,
      "GET /chat/packs": { packs: [{ pack_id: "p1m", tokens: 1000000, price_minor: 99900, currency: "INR", validity_days: 365 }] },
      "GET /chat/billing/subscription": {
        subscription: {
          plan_id: "pro", plan_name: "Pro", status: "active", current_period_start: "2026-11-22T00:00:00Z",
          current_period_end: "2026-12-22T00:00:00Z", spend_limit_minor: 500000, pending_plan_id: null,
          currency: "INR", monthly_fee_minor: 1999900, monthly_included_tokens: 10000000,
        },
        capacity_tokens: 15000000,
      },
      "GET /chat/billing/invoices": { invoices: [{ invoice_id: "inv-1", invoice_number: "AOS-2026-000001", kind: "cycle", status: "sent", currency: "INR", period_start: "2026-10-22T00:00:00Z", period_end: "2026-11-22T00:00:00Z", total_minor: 1999900 }] },
      "GET /chat/caps": { caps: [] },
      "POST /chat/billing/checkout": { checkout_url: "https://pay.example/checkout/1" },
    });
    render(<BillingPage client={client} onCheckout={onCheckout} />);
    expect(await screen.findByText("AOS-2026-000001")).toBeInTheDocument();
    expect(screen.getByText("Free trial")).toBeInTheDocument();
    expect(screen.getByText(/Pro \(/)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Buy" }));
    await waitFor(() => expect(onCheckout).toHaveBeenCalledWith("https://pay.example/checkout/1"));
  });

  it("shows payment instructions and the order instead of a checkout page with manual billing", async () => {
    const onCheckout = vi.fn();
    let ordered = false;
    const order = { reference: "AOS-1A2B3C4D5E", pack_id: "p1m", provider: "manual", amount_minor: 99900, currency: "INR", status: "pending", created_at: "2026-11-23T10:00:00Z" };
    const { client } = fakeApi({
      "GET /chat/wallet": WALLET,
      "GET /chat/packs": { packs: [{ pack_id: "p1m", tokens: 1000000, price_minor: 99900, currency: "INR", validity_days: 365 }] },
      "GET /chat/billing/subscription": { subscription: null, capacity_tokens: 0 },
      "GET /chat/billing/invoices": { invoices: [] },
      "GET /chat/caps": { caps: [] },
      "GET /chat/billing/orders": () => ({ orders: ordered ? [order] : [] }),
      "POST /chat/billing/checkout": () => {
        ordered = true;
        return {
          checkout_url: null, provider_payment_id: "AOS-1A2B3C4D5E", manual: true, amount_minor: 99900, currency: "INR",
          instructions: "We'll email you an invoice. Pay by bank transfer and quote the order reference.",
        };
      },
    });
    render(<BillingPage client={client} onCheckout={onCheckout} />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Buy" }));
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Order AOS-1A2B3C4D5E for ₹999.00 placed.");
    expect(notice).toHaveTextContent("Pay by bank transfer");
    expect(onCheckout).not.toHaveBeenCalled();
    const row = (await screen.findByRole("cell", { name: "AOS-1A2B3C4D5E" })).closest("tr")!;
    expect(row).toHaveTextContent("Awaiting payment");
  });

  it("hides plan details from non-admins", async () => {
    const { client } = fakeApi({
      "GET /chat/wallet": WALLET,
      "GET /chat/packs": { packs: [] },
      "GET /chat/billing/subscription": () => json({ detail: "insufficient role" }, 403),
      "GET /chat/billing/invoices": () => json({ detail: "insufficient role" }, 403),
      "GET /chat/caps": () => json({ detail: "insufficient role" }, 403),
    });
    render(<BillingPage client={client} />);
    expect(await screen.findByText("Plan, invoices and limits are visible to organisation admins.")).toBeInTheDocument();
  });
});
