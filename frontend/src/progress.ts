import type { AgentEvent, ModelProgress } from './types';

export type Calls = Record<string, { name: string; status: string; reused: boolean }>;

export function describeModel(progress: ModelProgress, now: number): string {
  const labels: Record<ModelProgress['phase'], string> = {
    deciding: '正在理解需求，判断下一步',
    reviewing_results: '正在分析查询结果，规划或调整方案',
    repairing_itinerary: '正在修正行程保存问题',
    reviewing_saved_itinerary: '正在整理已保存方案，准备答复',
  };
  const started = Date.parse(progress.started_at);
  const seconds = Number.isFinite(started) ? Math.max(0, Math.floor((now - started) / 1000)) : 0;
  return `${labels[progress.phase]}…（本次已等待 ${seconds} 秒）`;
}

export function updateCalls(calls: Calls, event: AgentEvent): Calls {
  if (event.type !== 'tool_started' && event.type !== 'tool_progress' && event.type !== 'tool_finished') return calls;
  const key = event.call_id ?? event.tool_name; // Compatibility with the legacy engine.
  const old = calls[key];
  const status = event.type === 'tool_started' ? 'running' : event.status;
  // Replay/recovery may report reuse after the original terminal event.
  if (old && (old.status === 'ok' || old.status === 'error') && status !== 'ok' && status !== 'error') return calls;
  return { ...calls, [key]: { name: event.tool_name, status, reused: old?.reused || status === 'reused' } };
}

export function describeCalls(calls: Calls, labels: Record<string, string>): string {
  const states: Record<string, string> = { queued: '排队中', running: '执行中', reused: '复用已取数据', ok: '成功', error: '失败' };
  return Object.values(calls).map((call, index) =>
    `${labels[call.name] ?? '查询出行信息'} ${index + 1}：${call.status === 'ok' && call.reused ? '成功（复用）' : states[call.status] ?? call.status}`
  ).join('；');
}
