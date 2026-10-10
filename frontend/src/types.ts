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
  tool_progress?: Record<string, { name: string; status: string; reused: boolean }>;
  itinerary?: ItineraryVersion | null;
}

export interface Health {
  model_configured: boolean;
  model: string;
  provider: string;
}

export type AgentEvent = ({ event_seq?: number; run_id?: string; message_id?: string; question_id?: string } & (
  | { type: 'status'; status: ConversationStatus }
  | { type: 'tool_started'; tool_name: string; call_id?: string }
  | { type: 'tool_progress'; tool_name: string; call_id: string; status: 'queued' | 'running' | 'reused' }
  | { type: 'supplier_progress'; tool_name: string; call_id: string; request_id: string; supplier: string; status: 'queued' | 'running' | 'ok' | 'error' }
  | { type: 'tool_finished'; tool_name: string; call_id?: string; status: 'ok' | 'error' }
  | { type: 'message'; role: 'user' | 'assistant'; content: string; kind: 'message' | 'question' | 'answer' | 'error' }
  | { type: 'done'; status: ConversationStatus }
  | { type: 'itinerary_updated'; version_id: string; revision: number }
  | { type: 'error'; code: string; message: string }));

export interface ItineraryVersion {
  id: string; revision: number | null; base_version_id: string | null; created_at: string; change_reason: string;
  document: {
    title: string; party: { travelers: number | null; rooms: number | null };
    items: Array<{ id: string; title: string; start: string; end: string; kind: string;
      description: string | null; protection: { state: string } | null }>;
    lodging: Array<{ id: string; title: string | null; check_in: string; check_out: string; protection: { state: string } | null }>;
    costs: Array<{ id: string; title: string; total: { currency: string; amount: string } | null;
      kind: string; tax_basis: string; fee_basis: string;
      basis: { travelers: number | null; rooms: number | null; nights: number | null; calculation: string } | null }>;
    requirements: Array<{ id: string; statement: string; origin: string; state: string }>;
    source_catalog: Record<string, { provider: string; url: string | null; retrieved_at: string; data_time: string | null }>;
  };
  review: { status: string; known_cost_totals: Array<{ currency: string; amount: string }>; unknown_cost_ids: string[];
    pending: Array<{ category: string; id: string; message: string }>;
    checks: Array<{ category: string; status: string; message: string }>;
    evidence: Record<string, { state: string; reasons: string[]; retrieved_at: string; snapshot_only: boolean }> };
  diff: { changes: Array<{ section: string; id: string; operation: string; fields: string[]; before: unknown; after: unknown }>;
    cost_deltas: Array<{ currency: string; before: string | null; after: string | null; delta: string | null }> };
}
export interface ItinerarySummary { id: string; revision: number; created_at: string; change_reason: string; base_version_id: string | null }
