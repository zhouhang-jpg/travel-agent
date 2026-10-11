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

test('new conversations explicitly choose whether to use saved preferences', async () => {
  fetchMock.mockResolvedValue(new Response('{}', { headers: { 'Content-Type': 'application/json' } }));
  await api.create(false);
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ timezone: 'Asia/Shanghai', use_saved_preferences: false });
  fetchMock.mockResolvedValue(new Response('{}', { headers: { 'Content-Type': 'application/json' } }));
  await api.create();
  expect(JSON.parse(fetchMock.mock.calls[1][1].body).use_saved_preferences).toBe(true);
});

test('preference edits and clearing retain their reviewed revision', async () => {
  const profile = { owner_id: 'local-user', revision: 8, enabled: true, entries: [], updated_at: null };
  const response = () => new Response(JSON.stringify(profile), { headers: { 'Content-Type': 'application/json' } });
  fetchMock.mockImplementation(async () => response());
  const update = { base_revision: 7, enabled: false, upsert: [{ id: 'pace', label: '节奏', value: '轻松' }], remove_ids: ['hotel'] };
  expect(await api.updatePreferences(update)).toEqual(profile);
  expect(fetchMock.mock.calls[0][0]).toBe('/api/preferences');
  expect(fetchMock.mock.calls[0][1].method).toBe('PUT');
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(update);
  await api.clearPreferences(8);
  expect(fetchMock.mock.calls[1][0]).toBe('/api/preferences?base_revision=8');
  expect(fetchMock.mock.calls[1][1]).toEqual({ method: 'DELETE' });
});

test('proposal application uses explicit confirmation and safely encodes identifiers', async () => {
  fetchMock.mockImplementation(async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }));
  await api.preferenceProposals('chat/a');
  expect(fetchMock.mock.calls[0][0]).toBe('/api/conversations/chat%2Fa/preference-proposals');
  await api.applyPreferenceProposal('chat/a', 'proposal/b', 3);
  expect(fetchMock.mock.calls[1][0]).toBe('/api/conversations/chat%2Fa/preference-proposals/proposal%2Fb/apply');
  expect(fetchMock.mock.calls[1][1].method).toBe('POST');
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ base_revision: 3 });
  await api.dismissPreferenceProposal('chat/a', 'proposal/b');
  expect(fetchMock.mock.calls[2][0]).toContain('proposal%2Fb/dismiss');
  expect(fetchMock.mock.calls[2][1].method).toBe('POST');
});

test('conversation preference control does not change the global profile', async () => {
  fetchMock.mockImplementation(async () => new Response('{"use_saved":false}', { headers: { 'Content-Type': 'application/json' } }));
  expect(await api.conversationPreferences('chat')).toEqual({ use_saved: false });
  expect(await api.setConversationPreferences('chat', false)).toEqual({ use_saved: false });
  expect(fetchMock.mock.calls[1][0]).toBe('/api/conversations/chat/preferences');
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ use_saved: false });
});

test('structured preference revision conflicts preserve code and do not retry the write', async () => {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'preference_revision_conflict', message: '版本已变化' } }), {
    status: 409, headers: { 'Content-Type': 'application/json' },
  }));
  await expect(api.updatePreferences({ base_revision: 2, upsert: [], remove_ids: [] })).rejects.toEqual(
    new APIError(409, '版本已变化', 'preference_revision_conflict'),
  );
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
