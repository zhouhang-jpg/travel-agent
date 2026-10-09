import { describe, expect, it } from 'vitest';
import { createSSEParser, readSSE } from './sse';

describe('SSE stream parser', () => {
  it('keeps frames intact across arbitrary chunk and CRLF boundaries', () => {
    const events: string[] = [];
    const parser = createSSEParser((value) => events.push(value));
    parser.push(': heartbeat\r\ndata: {"type":"sta');
    parser.push('tus","status":"running"}\r');
    expect(events).toEqual([]);
    parser.push('\n\r');
    parser.push('\ndata: {"type":"done"}\n\n');
    parser.finish();
    expect(events.map((value) => JSON.parse(value).type)).toEqual(['status', 'done']);
  });

  it('joins data lines and ignores SSE metadata', () => {
    const events: string[] = [];
    const parser = createSSEParser((value) => events.push(value));
    parser.push('event: progress\nid: 12\nretry: 2000\ndata: {"type":\ndata: "done"}\n\n');
    parser.finish();
    expect(events).toEqual(['{"type":\n"done"}']);
  });

  it('decodes Chinese split inside a UTF-8 character and surfaces malformed JSON', async () => {
    const bytes = new TextEncoder().encode('data: {"content":"杭州"}\n\n');
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
        controller.close();
      },
    });
    const events: unknown[] = [];
    await readSSE(body, (event) => events.push(event));
    expect(events).toEqual([{ content: '杭州' }]);
    const malformed = new Response('data: invalid\n\n').body!;
    await expect(readSSE(malformed, () => undefined)).rejects.toThrow('无法解析');
  });
});
