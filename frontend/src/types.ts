export type ConversationStatus = 'idle' | 'running' | 'waiting_user' | 'completed' | 'error';

export interface TranscriptMessage {
  role: 'user' | 'assistant';
  content: string;
  kind: 'message' | 'question' | 'answer' | 'error';
  created_at: string;
  message_id?: string;
  question_id?: string;
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
  question_id?: string | null;
  engine?: string;
  event_seq?: number;
}

export interface Health {
  model_configured: boolean;
  model: string;
  provider: string;
}

export type AgentEvent = ({ event_seq?: number; run_id?: string; message_id?: string; question_id?: string } & (
  | { type: 'status'; status: ConversationStatus }
  | { type: 'tool_started'; tool_name: string }
  | { type: 'tool_finished'; tool_name: string; status: 'ok' | 'error' }
  | { type: 'message'; role: 'user' | 'assistant'; content: string; kind: 'message' | 'question' | 'answer' | 'error' }
  | { type: 'done'; status: ConversationStatus }
  | { type: 'error'; code: string; message: string }));
