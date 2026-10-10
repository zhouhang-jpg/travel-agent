import { expect, test } from 'vitest';
import { describeCalls, describeModel, updateCalls } from './progress';

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

test('model phase and elapsed time stay factual across refreshed timestamps', () => {
  const event = { type: 'model_progress' as const, call_id: 'm', request_number: 3,
    phase: 'reviewing_saved_itinerary' as const, status: 'running' as const,
    started_at: '2026-10-10T10:00:00Z', attempt: 1 };
  expect(describeModel(event, Date.parse('2026-10-10T10:00:12Z'))).toContain('12 秒');
  expect(describeModel(event, Date.parse('2026-10-10T10:00:12Z'))).toContain('准备答复');
  expect(describeModel({ ...event, phase: 'repairing_itinerary' }, Date.parse(event.started_at))).toContain('修正');
});
