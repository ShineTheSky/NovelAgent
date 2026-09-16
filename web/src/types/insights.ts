export type PatternStatus = 'tentative' | 'confirmed' | 'disputed' | 'ready_for_review';

export interface LifecycleRecord {
  id: string;
  layer: 'evidence' | 'memory' | 'pattern';
  title?: string;
  claim: string;
  category: 'user' | 'project' | 'reference' | 'agent';
  domain: 'writing' | 'outline' | 'overall';
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

export interface MemoryPattern {

  pattern_id: string;
  project_id: string;
  kind: 'preference' | 'issue';
  subtype: string;
  dimension: string;
  canonical_claim: string;
  scope: string;
  status: PatternStatus;
  confidence: number;
  support_count: number;
  contradiction_count: number;
  updated_at: string;
}

export interface TraceMemory {
  memory_id: string;
  project_id: string;
  trace_id: string;
  source_event_ids: string[];
  kind: 'project_fact' | 'preference' | 'issue';
  subtype: string;
  claim: string;
  scope: string;
  confidence: number;
  status: 'memory' | 'rule' | 'disputed' | 'trace';
  importance: number;
  support_count: number;
  contradiction_count: number;
  last_reinforced_at: string;
  target_agents: string[];
  when_text: string;
  then_text: string;
  file_path: string;
  created_at: string;
  content?: string;
}

export interface TraceEvent {
  event_id: string;
  sequence_no: number;
  event_type: string;
  actor: string;
  payload: Record<string, unknown>;
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
  event_count: number;
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
