export type HistoryKind = 'document' | 'conversation';

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
  revisions?: (HistoryRevision | null)[];
  previous_attempt_events: HistoryEvent[];
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
