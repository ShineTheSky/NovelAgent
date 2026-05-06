const BASE = '/api';

export async function createProject(name: string, genre = '') {
  const res = await fetch(`${BASE}/projects`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, genre }),
  });
  if (!res.ok) throw new Error('创建项目失败');
  return res.json();
}

export async function listProjects() {
  const res = await fetch(`${BASE}/projects`);
  if (!res.ok) throw new Error('获取项目列表失败');
  return res.json();
}

export async function createSession(projectId: string) {
  const res = await fetch(`${BASE}/projects/${projectId}/sessions`, { method: 'POST' });
  if (!res.ok) throw new Error('创建会话失败');
  return res.json();
}

export async function listSessions(projectId: string) {
  const res = await fetch(`${BASE}/projects/${projectId}/sessions`);
  if (!res.ok) throw new Error('获取会话列表失败');
  return res.json();
}

export async function getSession(sessionId: string) {
  const res = await fetch(`${BASE}/sessions/${sessionId}`);
  if (!res.ok) throw new Error('获取会话失败');
  return res.json();
}

export function sendMessage(
  sessionId: string, content: string, acceptEdits: boolean,
  onEvent: (type: string, data: any) => void,
  onError: (err: Error) => void,
): AbortController {
  const controller = new AbortController();
  fetch(`${BASE}/sessions/${sessionId}/message`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, action: 'send', accept_edits: acceptEdits }),
    signal: controller.signal,
  }).then(async (res) => {
    if (res.status === 409) { onEvent('conflict', {}); return; }
    if (!res.ok) { onError(new Error('请求失败')); return; }
    const reader = res.body?.getReader();
    if (!reader) { onError(new Error('无响应流')); return; }
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      let eventType = '';
      for (const line of lines) {
        if (line.startsWith('event: ')) { eventType = line.slice(7).trim(); }
        else if (line.startsWith('data: ')) {
          try { onEvent(eventType, JSON.parse(line.slice(6))); }
          catch { /* skip parse errors */ }
        }
      }
    }
  }).catch(err => { if (err.name !== 'AbortError') onError(err); });
  return controller;
}

export async function stopGeneration(sessionId: string) {
  await fetch(`${BASE}/sessions/${sessionId}/stop`, { method: 'POST' });
}

export async function permissionResponse(sessionId: string, allow: boolean) {
  await fetch(`${BASE}/sessions/${sessionId}/permission-response`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ allow }),
  });
}

export async function questionResponse(sessionId: string, answers: any[]) {
  await fetch(`${BASE}/sessions/${sessionId}/question-response`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answers }),
  });
}

export async function deleteSession(sessionId: string) {
  await fetch(`${BASE}/sessions/${sessionId}`, { method: 'DELETE' });
}

export async function deleteProject(projectId: string) {
  await fetch(`${BASE}/projects/${projectId}`, { method: 'DELETE' });
}

export async function compressSession(sessionId: string) {
  const res = await fetch(`${BASE}/sessions/${sessionId}/compress`, { method: 'POST' });
  if (!res.ok) throw new Error('压缩失败');
  return res.json();
}

export async function toggleAcceptEdits(sessionId: string, enabled: boolean) {
  await fetch(`${BASE}/sessions/${sessionId}/accept-edits`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
}
