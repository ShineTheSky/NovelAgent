import type {
  CreatedSession,
  Message,
  ProjectInfo,
  QuestionAnswer,
  SessionDetail,
  SessionInfo,
  StreamEvent,
} from '../types/chat';
import type { LLMPositionUpdate, LLMSettings, ProviderSettingsUpdate } from '../types/llm';

const BASE = '/api';

export interface FileNode {
  name: string;
  path: string;
  type: 'file' | 'directory';
  size?: number;
  children?: FileNode[];
}

export class ApiError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function resourceId(value: string) {
  return encodeURIComponent(value);
}

async function errorMessage(response: Response) {
  const fallback = `请求失败 (${response.status})`;
  try {
    const body: unknown = await response.json();
    if (typeof body === 'object' && body !== null && 'detail' in body && typeof body.detail === 'string') {
      return body.detail;
    }
  } catch {
    // Some endpoints return an empty or non-JSON error response.
  }
  return fallback;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) throw new ApiError(await errorMessage(response), response.status);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function createProject(name: string, genre = '') {
  return requestJson<ProjectInfo>('/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, genre }),
  });
}

export function listProjects() {
  return requestJson<ProjectInfo[]>('/projects');
}

export function createSession(projectId: string) {
  return requestJson<CreatedSession>(`/projects/${resourceId(projectId)}/sessions`, { method: 'POST' });
}

export function listSessions(projectId: string) {
  return requestJson<SessionInfo[]>(`/projects/${resourceId(projectId)}/sessions`);
}

export function getSession(sessionId: string) {
  return requestJson<SessionDetail>(`/sessions/${resourceId(sessionId)}`);
}

function parseStreamEvent(eventType: string, rawData: string): StreamEvent {
  const payload: unknown = JSON.parse(rawData);
  if (typeof payload !== 'object' || payload === null) {
    throw new ApiError('服务器返回了无效的流式数据');
  }
  return { ...(payload as Record<string, unknown>), type: eventType || String((payload as { type?: unknown }).type || '') } as StreamEvent;
}

async function consumeSse(response: Response, onEvent: (event: StreamEvent) => void) {
  const reader = response.body?.getReader();
  if (!reader) throw new ApiError('无响应流');

  const decoder = new TextDecoder();
  let buffer = '';
  let eventType = '';
  let dataLines: string[] = [];
  let receivedTerminalEvent = false;

  const dispatch = () => {
    if (dataLines.length === 0) {
      eventType = '';
      return;
    }
    const event = parseStreamEvent(eventType, dataLines.join('\n'));
    receivedTerminalEvent ||= event.type === 'done' || event.type === 'error';
    onEvent(event);
    eventType = '';
    dataLines = [];
  };

  const consumeLine = (line: string) => {
    if (line === '') {
      dispatch();
      return;
    }
    if (line.startsWith(':')) return;
    const separator = line.indexOf(':');
    const field = separator === -1 ? line : line.slice(0, separator);
    const value = separator === -1 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'event') eventType = value;
    if (field === 'data') dataLines.push(value);
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newlineIndex = buffer.indexOf('\n');
      while (newlineIndex !== -1) {
        consumeLine(buffer.slice(0, newlineIndex).replace(/\r$/, ''));
        buffer = buffer.slice(newlineIndex + 1);
        newlineIndex = buffer.indexOf('\n');
      }
    }

    buffer += decoder.decode();
    if (buffer) consumeLine(buffer.replace(/\r$/, ''));
    dispatch();
    if (!receivedTerminalEvent) throw new ApiError('响应流在完成前断开');
  } finally {
    reader.releaseLock();
  }
}

export function sendMessage(
  sessionId: string,
  content: string,
  acceptEdits: boolean,
  onEvent: (event: StreamEvent) => void,
  onError: (error: Error) => void,
): AbortController {
  const controller = new AbortController();
  void (async () => {
    try {
      const response = await fetch(`${BASE}/sessions/${resourceId(sessionId)}/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, action: 'send', accept_edits: acceptEdits }),
        signal: controller.signal,
      });
      if (response.status === 409) {
        onEvent({ type: 'conflict' });
        return;
      }
      if (!response.ok) throw new ApiError(await errorMessage(response), response.status);
      await consumeSse(response, onEvent);
    } catch (error) {
      if (!controller.signal.aborted) onError(error instanceof Error ? error : new ApiError('请求失败'));
    }
  })();
  return controller;
}

export function stopGeneration(sessionId: string) {
  return requestJson<void>(`/sessions/${resourceId(sessionId)}/stop`, { method: 'POST' });
}

export function permissionResponse(sessionId: string, allow: boolean) {
  return requestJson<void>(`/sessions/${resourceId(sessionId)}/permission-response`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ allow }),
  });
}

export function questionResponse(sessionId: string, answers: QuestionAnswer[]) {
  return requestJson<void>(`/sessions/${resourceId(sessionId)}/question-response`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answers }),
  });
}

export function deleteSession(sessionId: string) {
  return requestJson<void>(`/sessions/${resourceId(sessionId)}`, { method: 'DELETE' });
}

export function deleteProject(projectId: string) {
  return requestJson<void>(`/projects/${resourceId(projectId)}`, { method: 'DELETE' });
}

export function compressSession(sessionId: string) {
  return requestJson<{ token_count: number; message: string }>(`/sessions/${resourceId(sessionId)}/compress`, { method: 'POST' });
}

export function toggleAcceptEdits(sessionId: string, enabled: boolean) {
  return requestJson<{ session_id: string; accept_edits_mode: boolean }>(`/sessions/${resourceId(sessionId)}/accept-edits`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
}

export function fetchFileTree(projectId: string, signal?: AbortSignal) {
  return requestJson<{ project_id: string; tree: FileNode }>(`/projects/${resourceId(projectId)}/files`, { signal });
}

export function fetchFileContent(projectId: string, filePath: string, signal?: AbortSignal) {
  return requestJson<{ path: string; content: string; size: number }>(
    `/projects/${resourceId(projectId)}/files/content?path=${encodeURIComponent(filePath)}`,
    { signal },
  );
}

export function getLLMSettings() {
  return requestJson<LLMSettings>('/settings/llm');
}

export function saveLLMSettings(positions: Record<string, LLMPositionUpdate>) {
  return requestJson<{ status: string; message: string }>('/settings/llm', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ positions }),
  });
}

export function saveProviderSettings(providers: Record<string, ProviderSettingsUpdate>) {
  return requestJson<{ status: string; message: string }>('/settings/providers', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ providers }),
  });
}

export type { Message };
