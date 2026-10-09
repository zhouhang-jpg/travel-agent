import { readSSE } from './sse';
import type { AgentEvent, Conversation, ConversationSummary, Health } from './types';

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
  } else if (response.status === 409) {
    message = '这个会话正在执行，请等待当前结果后继续发送。';
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
  create: () => request<Conversation>('/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ timezone: 'Asia/Shanghai' }),
  }),
  async send(id: string, content: string, onEvent: (event: AgentEvent) => void) {
    const response = await fetch(`${ROOT}/conversations/${encodeURIComponent(id)}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ content }),
    });
    await checkResponse(response);
    if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) {
      throw new Error('服务未返回有效的进度连接，请重新载入会话。');
    }
    await readSSE<AgentEvent>(response.body, onEvent);
  },
};
