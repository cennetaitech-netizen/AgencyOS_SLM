export const formatTokens = (n: number) => `${n.toLocaleString("en-US")} token${n === 1 ? "" : "s"}`;

export function formatMoney(minor: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(minor / 100);
  } catch {
    return `${(minor / 100).toFixed(2)} ${currency}`;
  }
}

export const formatDate = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString() : "–");

export const BUCKET_LABELS: Record<string, string> = {
  free_trial: "Free trial",
  plan_monthly: "Monthly allowance",
  pack: "Token pack",
  goodwill: "Credit",
};

export function formatValue(v: unknown): string {
  if (typeof v === "number") return v.toLocaleString("en-US");
  if (v === null || v === undefined) return "–";
  return String(v);
}

/** Only the fields that change, for the action card's before → after table. */
export function changedFields(before: Record<string, unknown>, after: Record<string, unknown>): string[] {
  return Object.keys(after).filter((k) => JSON.stringify(before[k]) !== JSON.stringify(after[k]));
}
