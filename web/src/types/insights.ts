export type InsightKind = 'project_fact' | 'preference' | 'issue';
export type PatternStatus = 'tentative' | 'ready_for_review' | 'confirmed' | 'disputed';

export interface TraceRecord {
  trace_id: string;
  project_id: string;
  session_id: string;
  status: string;
  started_at: string;
  finished_at?: string;
}

export interface Evidence {
  evidence_id: string;
  project_id: string;
  trace_id: string;
  source_event_ids: string[];
  kind: InsightKind;
  subtype: string;
  claim: string;
  scope: string;
  confidence: number;
  status: string;
  file_path: string;
  created_at: string;
  content?: string;
}

export interface TraceMemory {
  memory_id: string;
  project_id: string;
  trace_id: string;
  source_event_ids: string[];
  kind: InsightKind;
  subtype: string;
  claim: string;
  scope: string;
  confidence: number;
  status: string;
  file_path: string;
  created_at: string;
  content?: string;
}

export interface MemoryPattern {
  pattern_id: string;
  project_id: string;
  kind: 'preference' | 'issue';
  subtype: string;
  dimension: string;
  claim: string;
  scope: string;
  confidence: number;
  status: PatternStatus;
  supporting_memory_ids: string[];
  contradicting_memory_ids: string[];
  trace_ids: string[];
  support_count: number;
  contradiction_count: number;
  file_path: string;
  created_at: string;
  updated_at: string;
  content?: string;
}
