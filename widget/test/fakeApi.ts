import { vi } from "vitest";
import { ChatApiClient } from "../src/api/client";

type Handler = (body: unknown) => unknown;

export function sseResponse(events: [string, unknown][]): Response {
  const text = events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");
  const bytes = new TextEncoder().encode(text);
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      // Deliver in small chunks to exercise incremental parsing.
      for (let i = 0; i < bytes.length; i += 17) c.enqueue(bytes.slice(i, i + 17));
      c.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
}

export const json = (data: unknown, status = 200) =>
  new Response(status === 204 ? null : JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });

/** Routes "METHOD /path" to handlers; records every call. */
export function fakeApi(routes: Record<string, unknown>) {
  const calls: { method: string; path: string; body: unknown; headers: Record<string, string> }[] = [];
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.replace("http://api", "");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ method, path, body, headers: (init?.headers ?? {}) as Record<string, string> });
    const key = `${method} ${path}`;
    const match = Object.keys(routes).find((k) => k === key || (k.endsWith("*") && key.startsWith(k.slice(0, -1))));
    if (!match) return json({ detail: `no route ${key}` }, 404);
    const r = routes[match];
    if (r instanceof Response) return r.clone();
    if (typeof r !== "function") return json(r);
    const out = (r as Handler)(body);
    return out instanceof Response ? out : json(out);
  });
  const client = new ChatApiClient({
    baseUrl: "http://api",
    getHeaders: () => ({ Authorization: "Bearer test" }),
    fetch: fetchImpl as unknown as typeof fetch,
  });
  return { client, calls };
}

export const WALLET = {
  tokens_available: 200000,
  tokens_reserved: 0,
  spendable: 200000,
  low_balance: false,
  billing_mode: "live",
  buckets: [{ source: "free_trial", remaining: 200000, expires_at: "2026-12-22T00:00:00Z" }],
};
