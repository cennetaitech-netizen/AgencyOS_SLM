import { describe, expect, it } from "vitest";
import { SSEParser, readSSE } from "../src/api/sse";

describe("SSEParser", () => {
  it("handles events split across chunks and CRLF line endings", () => {
    const p = new SSEParser();
    expect(p.push('event: token\ndata: {"te')).toEqual([]);
    expect(p.push('xt": "Hel"}\n\nevent: token\r\ndata: {"text": "lo"}\r\n\r\n')).toEqual([
      { event: "token", data: { text: "Hel" } },
      { event: "token", data: { text: "lo" } },
    ]);
  });

  it("skips malformed events without breaking the stream", () => {
    const p = new SSEParser();
    expect(p.push("event: token\ndata: {not json}\n\nevent: heartbeat\ndata: {}\n\n")).toEqual([
      { event: "heartbeat", data: {} },
    ]);
  });
});

describe("readSSE", () => {
  it("decodes multi-byte characters split between chunks", async () => {
    const bytes = new TextEncoder().encode('event: token\ndata: {"text": "₹ 1,00,000 — ✓"}\n\n');
    const cut = bytes.indexOf(0xe2) + 1; // split inside the "₹" character
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(bytes.slice(0, cut));
        c.enqueue(bytes.slice(cut));
        c.close();
      },
    });
    const events = [];
    for await (const ev of readSSE(stream)) events.push(ev);
    expect(events).toEqual([{ event: "token", data: { text: "₹ 1,00,000 — ✓" } }]);
  });
});
