import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { api, APIError } from './api';

const memory = new Map<string, string>();
const fetchMock = vi.fn();

beforeEach(() => {
  memory.clear();
  fetchMock.mockReset();
  vi.stubGlobal('sessionStorage', {
    getItem: (key: string) => memory.get(key) ?? null,
    setItem: (key: string, value: string) => memory.set(key, value),
    removeItem: (key: string) => memory.delete(key),
  });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function done() {
  return new Response('id: 7\ndata: {"type":"done","status":"completed","event_seq":7}\n\n', {
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

test('unknown delivery retries preserve the request and answered question IDs', async () => {
  fetchMock.mockRejectedValueOnce(new TypeError('connection lost')).mockResolvedValueOnce(done());
  await expect(api.send('chat', '你决定', () => undefined, 'question-1')).rejects.toThrow();
  const first = JSON.parse(fetchMock.mock.calls[0][1].body);
  await api.send('chat', '你决定', () => undefined);
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual(first);
  expect(first.question_id).toBe('question-1');
  expect(memory.size).toBe(0);
  fetchMock.mockResolvedValueOnce(done());
  await api.send('chat', '你决定', () => undefined, 'question-2');
  const next = JSON.parse(fetchMock.mock.calls[2][1].body);
  expect(next.request_id).not.toBe(first.request_id);
  expect(next.question_id).toBe('question-2');
});

test('an incomplete SSE connection keeps the request for safe replay', async () => {
  fetchMock.mockResolvedValueOnce(new Response('data: {"type":"status","status":"running"}\n\n', {
    headers: { 'Content-Type': 'text/event-stream' },
  }));
  await expect(api.send('chat', '需求', () => undefined)).rejects.toThrow('进度连接已断开');
  expect(memory.size).toBe(1);
});

test('a stale-question conflict displays its actual recovery guidance', async () => {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail: '问题不匹配，请刷新会话后重试。' }), {
    status: 409, headers: { 'Content-Type': 'application/json' },
  }));
  await expect(api.send('chat', '回复', () => undefined)).rejects.toEqual(
    new APIError(409, '问题不匹配，请刷新会话后重试。'),
  );
  expect(memory.size).toBe(0);
});
