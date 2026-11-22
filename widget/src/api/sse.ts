/**
 * Incremental Server-Sent Events parser. Network chunks can split an event anywhere (even inside a UTF-8
 * character, which TextDecoder's streaming mode handles), so we buffer until a blank line ends each event.
 * We use fetch + ReadableStream instead of EventSource because EventSource can't send auth headers.
 */
import type { StreamEvent } from "../types";

export class SSEParser {
  private buffer = "";

  push(chunk: string): StreamEvent[] {
    this.buffer += chunk.replace(/\r\n/g, "\n");
    const events: StreamEvent[] = [];
    let end: number;
    while ((end = this.buffer.indexOf("\n\n")) >= 0) {
      const block = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      let name = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      if (!data.length) continue;
      try {
        events.push({ event: name, data: JSON.parse(data.join("\n")) } as StreamEvent);
      } catch {
        // Malformed event: skip it rather than breaking the whole stream.
      }
    }
    return events;
  }
}

export async function* readSSE(body: ReadableStream<Uint8Array>): AsyncGenerator<StreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const ev of parser.push(decoder.decode(value, { stream: true }))) yield ev;
    }
    for (const ev of parser.push(decoder.decode() + "\n\n")) yield ev;
  } finally {
    reader.releaseLock();
  }
}
