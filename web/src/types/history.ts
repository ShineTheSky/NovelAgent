export type HistoryKind = 'document' | 'conversation' | 'requirement' | 'review';

export interface HistorySummary {
  history_id: string;
  kind: HistoryKind;
  path: string;
  status: string;
  analysis_status: string;
  created_at: string;
  session_turn_no: number;
  base_revision_id: string;
  final_revision_id: string;
  previous_history_id: string;
  requirement_history_id?: string;
  review_history_id?: string;
  target_history_id?: string;
  user_input: string | null;
  task_summary: string | null;
}

export interface HistoryEvent {
  event_id: string;
  history_id: string;
  kind: string;
  content: string;
  revision_id: string;
  created_at: string;
  evidence_history_id?: string;
  event_order?: number | null;
  occurred_at?: string;
  order_status?: 'verified' | 'sequence_only' | 'unverified' | 'superseded' | 'duplicate';
  original_event_id?: string;
  canonical_event_id?: string;
  inherited_requirement?: boolean;
  inherited_review?: boolean;
  scope?: { path?: string; run_id?: string; preset?: string; source?: string; trace_id?: string; sequence_no?: number; evidence_scope?: string; review_section?: string };
}

export interface HistoryRevision {
  revision_id: string;
  parent_revision_id: string | null;
  version_no: number;
  path: string;
  body_md: string;
  dependencies: Record<string, string>;
  evidence: { kind: string; content: string; run_id: string }[];
  no_change: { opinion: string; rationale: string }[];
}

export interface HistoryUnit extends Omit<HistorySummary, 'user_input'> {
  request_id: string;
  session_id: string;
  assistant_response: string;
  context: unknown[];
  selected_turns: unknown;
  trace_ids: string[];
  events: HistoryEvent[];
  archived_events?: HistoryEvent[];
  revisions?: (HistoryRevision | null)[];
  previous_attempt_events: HistoryEvent[];
  requirement_event_ids?: string[];
  affected_files?: { history_id: string; path: string; status: string; previous_history_id: string }[];
  reviews?: { history_id: string; path: string; status: string; base_revision_id: string }[];
  result_histories?: { history_id: string; path: string; status: string }[];
}

export interface HistoryPage {
  items: HistorySummary[];
  total: number;
}

export interface HistoryFilePage {
  items: { path: string; history_count: number; updated_at: string;
           latest_history_id: string }[];
  total: number;
}
