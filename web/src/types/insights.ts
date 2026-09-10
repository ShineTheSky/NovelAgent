export type PatternStatus = 'tentative' | 'confirmed' | 'disputed' | 'ready_for_review';

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
  status: string;
  file_path: string;
  created_at: string;
  content?: string;
}
