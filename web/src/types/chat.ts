export type MessageRole =
  | 'user'
  | 'assistant'
  | 'system'
  | 'tool_result'
  | 'subagent_assistant'
  | 'subagent_result';

export type JsonRecord = Record<string, unknown>;
export type QuestionAnswer = string | string[];

export interface ToolCall {
  tool?: string;
  params?: JsonRecord;
  input?: JsonRecord;
  function?: {
    name?: string;
    arguments?: string;
  };
}

export interface Message {
  id?: number;
  role: MessageRole | string;
  content: string;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  name?: string;
  source?: 'main' | 'subagent' | string;
  preset?: string;
  reasoning_content?: string;
  _thinking?: boolean;
}

export interface ProjectInfo {
  project_id: string;
  name: string;
  genre: string;
}

export interface SessionInfo {
  session_id: string;
  title: string;
  updated_at: string;
}

export interface CreatedSession {
  session_id: string;
  project_id: string;
}

export interface SessionDetail {
  session_id: string;
  project_id: string;
  messages: Message[];
  accept_edits_mode: boolean;
  token_count: number;
}

export interface Question {
  question: string;
  header: string;
  options: Array<{ label: string; description: string }>;
  multiSelect: boolean;
}

export interface PendingAsk {
  tool: string;
  params_summary: string;
  onAllow: () => void;
  onDeny: () => void;
}

export interface PendingQuestion {
  questions: Question[];
  onSubmit: (answers: QuestionAnswer[]) => void;
}

export type StreamEvent =
  | { type: 'conflict' }
  | { type: 'text_delta'; delta: string; source?: 'main' | 'subagent' }
  | { type: 'thinking'; content: string; source?: 'main' | 'subagent' }
  | { type: 'tool_call'; tool: string; params: JsonRecord; source?: 'main' | 'subagent' }
  | { type: 'tool_result'; tool: string; success: boolean; data?: unknown; error?: string; source?: 'main' | 'subagent' }
  | { type: 'subagent_result'; preset: string; content: string }
  | { type: 'subagent_done'; result?: string }
  | { type: 'permission_ask'; tool: string; params_summary?: string }
  | { type: 'question_ask'; questions: Question[] }
  | { type: 'done'; token_count?: number; finish_reason?: string }
  | { type: 'error'; message: string };
