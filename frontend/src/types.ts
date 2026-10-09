export type ConversationStatus = 'idle' | 'running' | 'waiting_user' | 'completed' | 'error';

export interface TranscriptMessage {
  role: 'user' | 'assistant';
  content: string;
  kind: 'message' | 'question' | 'answer' | 'error';
  created_at: string;
}

export interface ConversationSummary {
  id: string;
  title: string;
  status: ConversationStatus;
  updated_at: string;
}

export interface Conversation extends ConversationSummary {
  timezone: string;
  transcript: TranscriptMessage[];
  last_error: null | { code: string; message: string };
}

export interface Health {
  model_configured: boolean;
  model: string;
  provider: string;
}

export type AgentEvent =
  | { type: 'status'; status: ConversationStatus }
  | { type: 'tool_started'; tool_name: string }
  | { type: 'tool_finished'; tool_name: string; status: 'ok' | 'error' }
  | { type: 'message'; role: 'assistant'; content: string; kind: 'question' | 'answer' | 'error' }
  | { type: 'done'; status: ConversationStatus }
  | { type: 'error'; code: string; message: string };
