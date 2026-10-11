import { expect, test } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { activityForTurn, applyConversationEvent, buildActivityBlocks, latestModelProgress, mergeActivityEvents, mergeConversationSnapshot } from './activity';
import { RunActivity } from './RunActivity';
import type { ActivityEvent, Conversation, ModelProgress } from './types';

const base: Conversation = { id: 'chat', title: '成都', timezone: 'Asia/Shanghai', updated_at: '2026-10-11T10:00:00Z',
  status: 'running', last_error: null, event_seq: 1, active_run_id: 'run-1', transcript: [
    { role: 'user', content: '从上海去成都', kind: 'message', created_at: '2026-10-11T10:00:00Z', run_id: 'run-1', event_seq: 1, message_id: 'u1' },
  ] };
const commentary: ActivityEvent = { type: 'commentary', content: '我会比较近期航班，并查询适合散步的街区。', event_seq: 2, run_id: 'run-1', created_at: '2026-10-11T10:00:01Z' };
const started: ActivityEvent = { type: 'tool_started', tool_name: 'search_flights', call_id: 'flights', summary: '上海 → 成都 · 2026-10-16', event_seq: 3, run_id: 'run-1', created_at: '2026-10-11T10:00:02Z' };

test('replay and snapshot merge deduplicate the persisted sequence and sort actual events', () => {
  const events = mergeActivityEvents([started, commentary], [commentary, started]);
  expect(events.map((event) => event.event_seq)).toEqual([2, 3]);
  expect(events).toHaveLength(2);
});

test('a delayed GET cannot erase newer SSE transcript, status or activity', () => {
  let conversation = applyConversationEvent(base, commentary);
  conversation = applyConversationEvent(conversation, started);
  conversation = applyConversationEvent(conversation, { type: 'message', role: 'assistant', content: '这里是方案', kind: 'answer', run_id: 'run-1', event_seq: 4, message_id: 'a1', created_at: '2026-10-11T10:00:04Z' });
  conversation = applyConversationEvent(conversation, { type: 'done', status: 'completed', run_id: 'run-1', event_seq: 5 });
  const merged = mergeConversationSnapshot(conversation, { ...base, event_seq: 2, activity_events: [commentary] });
  expect(merged.status).toBe('completed');
  expect(merged.event_seq).toBe(5);
  expect(merged.transcript.map((message) => message.content)).toEqual(['从上海去成都', '这里是方案']);
  expect(merged.activity_events).toHaveLength(2);
});

test('optimistic input binds to the accepted run and canonical user event replaces its bubble', () => {
  const pending: Conversation = { ...base, active_run_id: 'local-new', event_seq: 9, transcript: [...base.transcript,
    { role: 'user', content: '继续', kind: 'message', created_at: '2026-10-11T10:01:00Z', optimistic: true, optimistic_after_seq: 9, run_id: 'local-new' },
  ] };
  let accepted = applyConversationEvent(pending, { type: 'status', status: 'running', run_id: 'run-2', event_seq: 10 });
  expect(accepted.transcript.at(-1)?.run_id).toBe('run-2');
  accepted = applyConversationEvent(accepted, { type: 'message', role: 'user', kind: 'message', content: '继续', run_id: 'run-2', event_seq: 11, message_id: 'u2', created_at: '2026-10-11T10:01:00.020Z' });
  expect(accepted.transcript).toHaveLength(2);
  expect(accepted.transcript.at(-1)?.optimistic).toBeUndefined();
  expect(accepted.transcript.at(-1)?.message_id).toBe('u2');
});

test('identical text in a later turn cannot match an older accepted user message', () => {
  const earlier: Conversation = { ...base, event_seq: 8, transcript: [
    { ...base.transcript[0], content: '继续', event_seq: 1 },
  ] };
  const pending: Conversation = { ...earlier, transcript: [...earlier.transcript,
    { role: 'user', content: '继续', kind: 'message', created_at: '2026-10-11T10:01:00Z', optimistic: true, optimistic_after_seq: 8, run_id: 'local-new' },
  ] };
  const merged = mergeConversationSnapshot(pending, earlier);
  expect(merged.transcript).toHaveLength(2);
  expect(merged.transcript[0].message_id).toBe('u1');
  expect(merged.transcript[1].optimistic).toBe(true);
  const canonical = { ...earlier, event_seq: 10, transcript: [...earlier.transcript,
    { role: 'user' as const, content: '继续', kind: 'message' as const, created_at: '2026-10-11T10:01:00.020Z', event_seq: 9, run_id: 'run-2', message_id: 'u2' },
  ] };
  expect(mergeConversationSnapshot(pending, canonical).transcript).toHaveLength(2);
});

test('progress history is anchored to its own user turn after refresh', () => {
  const conversation: Conversation = { ...base, status: 'completed', active_run_id: null, transcript: [...base.transcript,
    { role: 'assistant', content: '请补充日期', kind: 'question', created_at: '2026-10-11T10:00:03Z', run_id: 'run-1', event_seq: 4 },
    { role: 'user', content: '日期灵活', kind: 'message', created_at: '2026-10-11T10:01:00Z', run_id: 'run-2', event_seq: 5 },
  ], activity_events: [commentary, started, { ...commentary, run_id: 'run-2', event_seq: 6, content: '我会比较不同日期。' }] };
  expect(activityForTurn(conversation, 0).map((event) => event.event_seq)).toEqual([2, 3]);
  expect(activityForTurn(conversation, 2).map((event) => event.event_seq)).toEqual([6]);
  expect(activityForTurn(conversation, 1)).toEqual([]);
});

test('backfilled message sequences separate older history even when run metadata is absent', () => {
  const conversation: Conversation = { ...base, transcript: [
    { ...base.transcript[0], run_id: undefined },
    { ...base.transcript[0], content: '换日期', run_id: undefined, event_seq: 5 },
  ], activity_events: [commentary, { ...commentary, event_seq: 6, run_id: 'run-2' }] };
  expect(activityForTurn(conversation, 0).map((event) => event.event_seq)).toEqual([2]);
  expect(activityForTurn(conversation, 1).map((event) => event.event_seq)).toEqual([6]);
});

test('terminal recovery preserves success and records reuse after a terminal tool event', () => {
  const events: ActivityEvent[] = [started,
    { type: 'tool_finished', tool_name: 'search_flights', call_id: 'flights', status: 'ok', run_id: 'run-1', event_seq: 4 },
    { type: 'tool_progress', tool_name: 'search_flights', call_id: 'flights', status: 'reused', run_id: 'run-1', event_seq: 5 },
  ];
  const blocks = buildActivityBlocks(events);
  expect(blocks[0].kind).toBe('tools');
  if (blocks[0].kind === 'tools') expect(blocks[0].records[0]).toMatchObject({ status: 'ok', reused: true, summary: started.summary });
  const html = renderToStaticMarkup(<RunActivity events={events} running={false} />);
  expect(html).toContain('成功 · 已复用');
  expect(html).not.toContain('class="loader"');
});

test('a completed run marks unfinished calls without an endless activity spinner', () => {
  const html = renderToStaticMarkup(<RunActivity events={[started]} running={false} />);
  expect(html).toContain('未完成');
  expect(html).not.toContain('loader');
  expect(html).not.toContain('查询中');
});

test('26 queries keep a compact summary and actual latest context, with old rows expandable', () => {
  const events: ActivityEvent[] = Array.from({ length: 26 }, (_, index) => ({ type: 'tool_finished', tool_name: 'search_places',
    call_id: `place-${index}`, status: 'ok', run_id: 'run-1', event_seq: index + 2, summary: `成都 · 街区${index + 1}` }));
  const html = renderToStaticMarkup(<RunActivity events={[commentary, ...events]} running={false} />);
  expect(html).toContain('查看此前 24 条查询记录');
  expect(html).toContain('<details');
  expect(html).toContain('成都 · 街区26');
  expect(html).toContain(commentary.content);
  expect(html).not.toContain('百分之');
});

test('visible commentary and summaries render external strings as text', () => {
  const html = renderToStaticMarkup(<RunActivity events={[{ ...commentary, content: '<script>alert(1)</script>' }, { ...started, summary: '<img src=x>' }]} running />);
  expect(html).toContain('&lt;script&gt;');
  expect(html).toContain('&lt;img src=x&gt;');
  expect(html).not.toContain('<script>');
});

test('model status ignores an older attempt but accepts request one in a new run', () => {
  const current: ModelProgress = { type: 'model_progress', call_id: 'run-1/model/3', request_number: 3, attempt: 2,
    phase: 'reviewing_results', started_at: '2026-10-11T10:00:00Z', status: 'running' };
  expect(latestModelProgress(current, { ...current, attempt: 1, status: 'complete' })).toBe(current);
  expect(latestModelProgress(current, { ...current, call_id: 'run-2/model/1', request_number: 1 })).toMatchObject({ call_id: 'run-2/model/1', request_number: 1 });
});
