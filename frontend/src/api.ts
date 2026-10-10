import { readSSE } from './sse';
import type { AgentEvent, Conversation, ConversationSummary, Health, ItineraryVersion, ItinerarySummary } from './types';

const ROOT = '/api';

export class APIError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'APIError';
  }
}

async function checkResponse(response: Response) {
  if (response.ok) return;
  let message = '请求失败，请稍后再试。';
  try {
    const body = await response.json();
    const detail = body.detail ?? body;
    if (typeof detail === 'string') message = detail;
    else if (typeof detail.message === 'string') message = detail.message;
  } catch {
    // Do not expose raw proxy errors or HTML pages to the user.
  }
  if (response.status === 503) {
    message = '模型服务尚未就绪。请在后端配置 DeepSeek API Key，再重试。';
  }
  throw new APIError(response.status, message);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${ROOT}${path}`, init);
  await checkResponse(response);
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>('/agent/health'),
  list: () => request<{ items: ConversationSummary[] }>('/conversations'),
  get: (id: string) => request<Conversation>(`/conversations/${encodeURIComponent(id)}`),
  itineraries: (id: string) => request<{ current_id: string | null; items: ItinerarySummary[] }>(
    `/conversations/${encodeURIComponent(id)}/itineraries`),
  itinerary: (id: string, versionId: string) => request<ItineraryVersion>(
    `/conversations/${encodeURIComponent(id)}/itineraries/${encodeURIComponent(versionId)}`),
  create: () => request<Conversation>('/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ timezone: 'Asia/Shanghai' }),
  }),
  async replay(id: string, after: number, onEvent: (event: AgentEvent) => void, signal: AbortSignal) {
    const response = await fetch(`${ROOT}/conversations/${encodeURIComponent(id)}/events?after=${after}`, {
      headers: { Accept: 'text/event-stream' }, signal,
    });
    await checkResponse(response);
    if (response.body) await readSSE<AgentEvent>(response.body, onEvent);
  },
  async send(id: string, content: string, onEvent: (event: AgentEvent) => void, questionId?: string | null) {
    const key = `travel-agent.pending.${id}`;
    let pending = { content, request_id: crypto.randomUUID(), question_id: questionId ?? undefined };
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) ?? 'null') as typeof pending | null;
      if (saved?.content === content) pending = saved;
      sessionStorage.setItem(key, JSON.stringify(pending));
    } catch { /* The request still has an idempotency key if storage is unavailable. */ }
    const response = await fetch(`${ROOT}/conversations/${encodeURIComponent(id)}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(pending),
    });
    try {
      await checkResponse(response);
    } catch (error) {
      if (error instanceof APIError && error.status >= 400 && error.status < 500) {
        try { sessionStorage.removeItem(key); } catch { /* Optional browser storage. */ }
      }
      throw error;
    }
    if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) {
      throw new Error('服务未返回有效的进度连接，请重新载入会话。');
    }
    let done = false;
    await readSSE<AgentEvent>(response.body, (event) => {
      if (event.type === 'done') done = true;
      onEvent(event);
    });
    if (!done) throw new Error('进度连接已断开，请重新载入会话查看结果。');
    try { sessionStorage.removeItem(key); } catch { /* Optional browser storage. */ }
  },
};
