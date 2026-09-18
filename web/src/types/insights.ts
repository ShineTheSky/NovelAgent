export interface LifecycleRecord {
  id: string;
  layer: 'evidence' | 'memory' | 'pattern';
  title?: string;
  claim: string;
  category: 'user' | 'project' | 'reference' | 'agent';
  domain: 'writing' | 'outline' | 'overall';
  kind?: 'text_feedback' | 'review_issue' | string;
  trace_id?: string;
  source_event_ids?: string[];
  weight: number;
  support_count?: number;
  promotion_status?: 'auto' | 'manual_review';
  downgrade_reason?: string;
  manual_downgraded_at?: string;
  manual_review_cancelled_at?: string;
  updated: string;
  file_path: string;
  content?: string;
}

export interface TraceEvent {
  event_id: string;
  trace_id?: string;
  parent_event_id?: string | null;
  sequence_no: number;
  event_type: string;
  actor: string;
  payload: Record<string, unknown>;
  duration_ms?: number | null;
  created_at: string;
}

export interface TraceClassification {
  has_error: boolean;
  has_correction: boolean;
  has_confirmation: boolean;
  has_feedback: boolean;
  items: Array<{ event_id: string; type: 'error' | 'correction' | 'confirmation' | 'feedback'; summary: string; confidence: number }>;
}

export interface TraceEvidence {
  trace_id: string;
  session_id: string;
  project_id: string;
  user_message: string;
  final_answer: string;
  status: string;
  source: 'live' | 'historical';
  operation_kind: 'conversation' | 'routine' | 'tool_only' | 'system';
  analysis_status: 'pending' | 'complete' | 'skipped';
  token_count: number;
  event_count?: number;
  turn_no?: number | null;
  previous_trace_id?: string | null;
  next_trace_id?: string | null;
  started_at: string;
  finished_at?: string;
  has_error: boolean;
  has_correction: boolean;
  has_confirmation: boolean;
  has_feedback: boolean;
  is_classified?: boolean;
  classification?: TraceClassification | null;
  events?: TraceEvent[];
}
