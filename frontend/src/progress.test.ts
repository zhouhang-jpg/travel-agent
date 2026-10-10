import { expect, test } from 'vitest';
import { describeCalls, updateCalls } from './progress';

test('same-name calls keep separate queued/running/terminal states through replay', () => {
  let calls = updateCalls({}, { type: 'tool_started', tool_name: 'search_places', call_id: 'a' });
  calls = updateCalls(calls, { type: 'tool_progress', tool_name: 'search_places', call_id: 'b', status: 'queued' });
  calls = updateCalls(calls, { type: 'tool_finished', tool_name: 'search_places', call_id: 'a', status: 'ok' });
  expect(describeCalls(calls, { search_places: '地点' })).toBe('地点 1：成功；地点 2：排队中');
  calls = updateCalls(calls, { type: 'tool_progress', tool_name: 'search_places', call_id: 'a', status: 'reused' });
  expect(calls.a.status).toBe('ok');
  calls = updateCalls(calls, { type: 'tool_progress', tool_name: 'search_places', call_id: 'b', status: 'reused' });
  calls = updateCalls(calls, { type: 'tool_finished', tool_name: 'search_places', call_id: 'b', status: 'ok' });
  expect(describeCalls(calls, {})).toContain('成功（复用）');
});
