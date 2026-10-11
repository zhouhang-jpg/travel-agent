import type { ActivityEvent, AgentEvent, Conversation, ModelProgress, TranscriptMessage } from './types';

export const TOOL_LABELS: Record<string, string> = {
  search_places: '查找地点', search_pois: '查找地点', get_place_details: '核实地点信息',
  get_attraction_opening_hours: '查询景点开放时间', geocode: '定位出行地点',
  get_routes: '查询路线', plan_route: '查询路线', get_weather: '查询天气',
  search_web: '查找出行资料', web_search: '查找出行资料', fetch_webpage: '阅读来源资料',
  search_flights: '查询航班', search_trains: '查询火车', search_coaches: '查询大巴',
  search_hotels: '查询住宿', validate_itinerary: '检查行程安排', save_itinerary: '保存行程修改',
  compare_itinerary_options: '比较候选方案', review_itinerary: '核对需求与方案结论',
  get_user_preferences: '读取出行偏好', propose_user_preferences: '准备偏好建议',
};

export function isActivityEvent(event: AgentEvent): event is ActivityEvent {
  return ['commentary', 'tool_started', 'tool_progress', 'tool_finished', 'model_progress'].includes(event.type);
}

function eventKey(event: ActivityEvent): string {
  if (event.event_seq !== undefined) return `seq:${event.event_seq}`;
  // Legacy events have no persisted sequence. Identity includes the run, timestamp and payload.
  return JSON.stringify([event.run_id, event.type, event.created_at, event]);
}

export function mergeActivityEvents(previous: ActivityEvent[] = [], incoming: ActivityEvent[] = []): ActivityEvent[] {
  const merged = new Map<string, ActivityEvent>();
  for (const event of [...previous, ...incoming]) {
    const key = eventKey(event);
    merged.set(key, { ...merged.get(key), ...event });
  }
  return [...merged.values()].sort((a, b) => {
    if (a.event_seq !== undefined && b.event_seq !== undefined) return a.event_seq - b.event_seq;
    return (a.created_at ?? '').localeCompare(b.created_at ?? '');
  });
}

function sameMessage(a: TranscriptMessage, b: TranscriptMessage): boolean {
  if (a.message_id && b.message_id) return a.message_id === b.message_id;
  if (a.event_seq !== undefined && b.event_seq !== undefined) return a.event_seq === b.event_seq;
  const optimistic = a.optimistic ? a : b.optimistic ? b : undefined;
  const canonical = optimistic === a ? b : a;
  if (optimistic) {
    if (canonical.event_seq !== undefined && optimistic.optimistic_after_seq !== undefined && canonical.event_seq <= optimistic.optimistic_after_seq) return false;
    if (canonical.event_seq === undefined && Date.parse(canonical.created_at) < Date.parse(optimistic.created_at)) return false;
  } else if (a.run_id && b.run_id && a.run_id !== b.run_id) return false;
  return a.role === b.role && a.content === b.content && a.kind === b.kind
    && (a.optimistic === true || b.optimistic === true || a.created_at === b.created_at);
}

function reconcileTranscript(previous: TranscriptMessage[], incoming: TranscriptMessage[], stale: boolean): TranscriptMessage[] {
  const result = [...(stale ? previous : incoming)];
  const extras = stale ? incoming : previous;
  for (const message of extras) {
    const index = result.findIndex((saved) => sameMessage(saved, message));
    if (index >= 0) {
      const canonical = result[index].optimistic && !message.optimistic ? message : result[index];
      result[index] = { ...message, ...result[index], ...canonical, run_id: canonical.run_id ?? message.run_id ?? result[index].run_id, optimistic: canonical.optimistic };
    } else if (stale || message.optimistic || message.event_seq !== undefined) {
      result.push(message);
    }
  }
  return result.sort((a, b) => {
    if (a.event_seq !== undefined && b.event_seq !== undefined) return a.event_seq - b.event_seq;
    return Date.parse(a.created_at) - Date.parse(b.created_at);
  });
}

/** A delayed GET may add historical activity, but cannot erase newer SSE state. */
export function mergeConversationSnapshot(previous: Conversation | undefined, incoming: Conversation): Conversation {
  if (!previous) return { ...incoming, activity_events: mergeActivityEvents([], incoming.activity_events) };
  const stale = incoming.event_seq !== undefined && (previous.event_seq ?? 0) > incoming.event_seq;
  const merged = stale ? { ...incoming, ...previous } : { ...previous, ...incoming };
  return { ...merged,
    transcript: reconcileTranscript(previous.transcript, incoming.transcript, stale),
    activity_events: mergeActivityEvents(previous.activity_events, incoming.activity_events),
    event_seq: Math.max(previous.event_seq ?? 0, incoming.event_seq ?? 0),
  };
}

export function applyConversationEvent(conversation: Conversation, event: AgentEvent): Conversation {
  event = { ...event, run_id: event.run_id ?? conversation.active_run_id ?? undefined };
  let transcript = conversation.transcript;
  if (event.run_id && (event.type === 'status' && event.status === 'running' || event.type === 'message' && event.role === 'user')) {
    const index = transcript.reduce((last, message, i) => message.role === 'user' && message.optimistic ? i : last, -1);
    if (index >= 0) transcript = transcript.map((message, i) => i === index ? { ...message, run_id: event.run_id } : message);
  }
  if (event.type === 'message') {
    const message: TranscriptMessage = {
      role: event.role, content: event.content, kind: event.kind, created_at: event.created_at ?? new Date().toISOString(),
      message_id: event.message_id, question_id: event.question_id, run_id: event.run_id, event_seq: event.event_seq,
    };
    const existing = transcript.findIndex((entry) => sameMessage(entry, message));
    const last = transcript.at(-1);
    if (existing >= 0) transcript = transcript.map((entry, index) => index === existing ? message : entry);
    else if (event.message_id || event.event_seq !== undefined || last?.role !== event.role || last?.content !== event.content || last?.kind !== event.kind) {
      transcript = [...transcript, message];
    }
  }
  return { ...conversation, transcript,
    event_seq: Math.max(conversation.event_seq ?? 0, event.event_seq ?? 0),
    active_run_id: event.type === 'status' && event.status === 'running' ? event.run_id ?? conversation.active_run_id : conversation.active_run_id,
    status: event.type === 'status' || event.type === 'done' ? event.status : conversation.status,
    question_id: event.type === 'message' && event.kind === 'question' ? event.question_id : conversation.question_id,
    activity_events: isActivityEvent(event) ? mergeActivityEvents(conversation.activity_events, [event]) : conversation.activity_events,
  };
}

export function activityForTurn(conversation: Conversation, index: number): ActivityEvent[] {
  const message = conversation.transcript[index];
  if (message?.role !== 'user') return [];
  const nextUser = conversation.transcript.slice(index + 1).find((entry) => entry.role === 'user');
  return (conversation.activity_events ?? []).filter((event) => {
    if (event.run_id && message.run_id) return event.run_id === message.run_id;
    if (event.event_seq !== undefined && message.event_seq !== undefined) {
      return event.event_seq > message.event_seq && (!nextUser?.event_seq || event.event_seq < nextUser.event_seq);
    }
    // In older conversations only the active turn has an unbound run; never attach it to every user turn.
    return !nextUser && (!event.run_id || event.run_id === conversation.active_run_id);
  });
}

export interface ToolRecord {
  key: string; name: string; status: string; reused: boolean; summary?: string; created_at?: string;
}
export type ActivityBlock = { kind: 'commentary'; key: string; content: string; created_at?: string }
  | { kind: 'tools'; key: string; records: ToolRecord[] };

export function buildActivityBlocks(events: ActivityEvent[]): ActivityBlock[] {
  const blocks: ActivityBlock[] = [];
  const tools = new Map<string, ToolRecord>();
  for (const event of events) {
    if (event.type === 'commentary') {
      blocks.push({ kind: 'commentary', key: eventKey(event), content: event.content, created_at: event.created_at });
    } else if (event.type === 'tool_started' || event.type === 'tool_progress' || event.type === 'tool_finished') {
      const key = `${event.run_id ?? 'legacy'}:${event.call_id ?? event.tool_name}`;
      let record = tools.get(key);
      const status = event.type === 'tool_started' ? 'running' : event.status;
      if (!record) {
        record = { key, name: event.tool_name, status, reused: status === 'reused', summary: event.summary, created_at: event.created_at };
        tools.set(key, record);
        const last = blocks.at(-1);
        if (last?.kind === 'tools') last.records.push(record);
        else blocks.push({ kind: 'tools', key: eventKey(event), records: [record] });
      } else {
        record.reused ||= status === 'reused';
        if (!['ok', 'error'].includes(record.status) || ['ok', 'error'].includes(status)) record.status = status;
        if (event.summary) record.summary = event.summary;
      }
    }
  }
  return blocks;
}

export function latestModelProgress(previous: ModelProgress | null | undefined, incoming: ModelProgress): ModelProgress {
  if (!previous) return incoming;
  if (incoming.run_id && previous.run_id && incoming.run_id !== previous.run_id) return incoming;
  const previousScope = previous.call_id.split('/model/')[0];
  const incomingScope = incoming.call_id.split('/model/')[0];
  if (previous.call_id.includes('/model/') && incoming.call_id.includes('/model/') && previousScope !== incomingScope) return incoming;
  if (incoming.request_number < previous.request_number) return previous;
  if (incoming.request_number === previous.request_number && incoming.attempt < previous.attempt) return previous;
  return incoming;
}
