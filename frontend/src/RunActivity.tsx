import { useEffect, useState } from 'react';
import { buildActivityBlocks, TOOL_LABELS } from './activity';
import type { ToolRecord } from './activity';
import { describeModel } from './progress';
import type { ActivityEvent, ModelProgress } from './types';

function activityTime(value?: string) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function ToolRow({ record, running }: { record: ToolRecord; running: boolean }) {
  const unfinished = ['queued', 'running'].includes(record.status);
  const states: Record<string, string> = {
    queued: '排队中', running: '查询中', reused: '复用已有结果', ok: '成功', error: '失败',
  };
  const status = unfinished && !running ? '未完成' : record.status === 'ok' && record.reused ? '成功 · 已复用' : states[record.status] ?? '已返回';
  return <li className={`activity-tool ${unfinished && running ? 'is-active' : ''} ${record.status === 'error' ? 'has-error' : ''}`}>
    <span className={`activity-mark ${unfinished && running ? 'loader' : ''}`} aria-hidden="true">{unfinished && running ? '' : record.status === 'error' ? '!' : unfinished ? '−' : '✓'}</span>
    <div className="activity-tool-main"><span className="activity-tool-title">{TOOL_LABELS[record.name] ?? '查询出行信息'}</span>
      {record.summary && <span className="activity-tool-summary">{record.summary}</span>}</div>
    <span className="activity-tool-state">{status}</span>
  </li>;
}

function ToolRecords({ records, running }: { records: ToolRecord[]; running: boolean }) {
  // Keep active queries visible; earlier completed calls remain available without filling the chat.
  const active = running ? records.filter((record) => ['queued', 'running'].includes(record.status)) : [];
  const executing = active.filter((record) => record.status === 'running');
  const visible = executing.length > 0 ? executing.slice(-2) : active.length > 0 ? active.slice(-2) : records.slice(-2);
  const visibleKeys = new Set(visible.map((record) => record.key));
  const earlier = records.filter((record) => !visibleKeys.has(record.key));
  return <div className="activity-tools">
    {earlier.length > 0 && <details className="activity-earlier"><summary>查看此前 {earlier.length} 条查询记录</summary>
      <ul>{earlier.map((record) => <ToolRow key={record.key} record={record} running={running} />)}</ul>
    </details>}
    <ul>{visible.map((record) => <ToolRow key={record.key} record={record} running={running} />)}</ul>
  </div>;
}

export function ModelProgressLabel({ progress }: { progress: ModelProgress }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [progress.call_id, progress.attempt, progress.started_at]);
  return <>{describeModel(progress, now)}</>;
}

export function RunActivity({ events, running, modelProgress }: {
  events: ActivityEvent[]; running: boolean; modelProgress?: ModelProgress | null;
}) {
  const blocks = buildActivityBlocks(events);
  if (!blocks.length && !running) return null;
  return <div className={`run-activity ${running ? 'is-running' : ''}`} aria-label="本轮安排进展">
    <div className="activity-heading"><span className="activity-heading-line" /><span>{running ? '正在安排' : '本轮查询与进展'}</span></div>
    <div className="activity-timeline">{blocks.map((block) => block.kind === 'commentary'
      ? <div className="activity-commentary" key={block.key}><span className="activity-dot" aria-hidden="true" />
        <div><p>{block.content}</p>{activityTime(block.created_at) && <time dateTime={block.created_at}>{activityTime(block.created_at)}</time>}</div></div>
      : <ToolRecords key={block.key} records={block.records} running={running} />)}</div>
    {running && <div className="activity-wait" role="status"><span className="loader" /><span>{modelProgress?.status === 'running'
      ? <ModelProgressLabel progress={modelProgress} /> : blocks.length > 0 ? '正在继续安排你的旅程…' : '正在理解你的需求，安排下一步…'}</span></div>}
    {blocks.some((block) => block.kind === 'tools') && <p className="activity-note">查询状态表示工具执行结果，方案仍需结合来源核对。</p>}
  </div>;
}
