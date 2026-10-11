export interface LifecycleRecord {
  schema?: 'knowledge_v1' | 'knowledge_v2' | 'legacy';
  version?: number;
  previous_version?: number;
  status?: string;
  payload?: Record<string, unknown>;
  memory_sources?: Array<{ memory_id: string; version: number; statement_ids?: string[]; fact_ids?: string[]; role: string }>;
  classifications?: KnowledgeClassification[];
  legacy_classification?: { category?: string; domain?: string; knowledge_domains?: string[] };
  replacements?: Array<{ new_id: string; new_version: number; reason: string }>;
  id: string;
  layer: 'insight' | 'memory' | 'pattern';
  title?: string;
  claim: string;
  category?: 'user' | 'project' | 'reference' | 'agent';
  domain?: 'writing' | 'outline' | 'overall';
  kind?: 'text_feedback' | 'review_issue' | string;
  trace_id?: string;
  trace_ids?: string[];
  trace_refs?: Array<{ trace_id: string; turn: number; source_event_ids?: string[] }>;
  source_event_ids?: string[];
  history_ids?: string[];
  related_history_ids?: string[];
  history_event_ids?: string[];
  provenance_notes?: string[];
  history_source_links?: Array<{ source_event_id: string; history_id: string; history_event_ids: string[]; match: 'evidence' | 'context' }>;
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

export type KnowledgeClassification =
  | { domain: 'content'; categories: Array<'character' | 'setting' | 'background' | 'organization' | 'location' | 'item' | 'event'> }
  | { domain: 'writing'; categories: Array<'outline' | 'drafting' | 'polishing' | 'reviewing'> }
  | { domain: 'agent_work'; categories: Array<'tool_use' | 'task_dispatch' | 'context_management' | 'collaboration' | 'interaction_confirmation' | 'result_verification' | 'task_recovery'> };

const domainLabels = { content: '内容', writing: '写作', agent_work: 'Agent 工作' };
const categoryLabels: Record<string, string> = {
  character: '人物', setting: '设定', background: '背景', organization: '组织', location: '地点', item: '物品', event: '事件',
  outline: '大纲规划', drafting: '正文创作', polishing: '润色修改', reviewing: '审阅检查',
  tool_use: '工具使用', task_dispatch: '任务调度', context_management: '上下文管理', collaboration: '协作流程',
  interaction_confirmation: '交互确认', result_verification: '结果核验', task_recovery: '任务恢复',
};

export function classificationLabel(record: LifecycleRecord) {
  return record.classifications?.map(c => `${domainLabels[c.domain]}：${c.categories.map(k => categoryLabels[k] ?? k).join('、') || '待分类'}`).join('；') || '待分类';
}

export function isVersionedKnowledge(record: LifecycleRecord) {
  return record.schema === 'knowledge_v1' || record.schema === 'knowledge_v2';
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
  operation_kind: 'conversation' | 'context_transition' | 'agent_run' | 'routine' | 'tool_only' | 'system' | string;
  analysis_status: 'pending' | 'complete' | 'skipped';
  token_count: number;
  event_count?: number;
  event_offset?: number;
  turn_no?: number | null;
  previous_trace_id?: string | null;
  next_trace_id?: string | null;
  source_agent?: string;
  agent_position?: string;
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

export interface TracePage {
  items: TraceEvidence[];
  total: number;
  limit: number;
  offset: number;
}

export interface TraceValidation {
  trace_id: string;
  valid: boolean;
  chain_trace_ids: string[];
  terminal_trace_id: string;
  checks: Array<{
    key: string;
    status: 'pass' | 'fail' | 'warning';
    message: string;
  }>;
}

export interface TraceAgentStats {
  sources: Array<{ agent: string; count: number }>;
  participants: Array<{ agent: string; count: number }>;
  total: number;
}
