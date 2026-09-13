import type { AgentRun, AgentRunEvent, JsonRecord, Message } from './types/chat';

type StoredEvent = { type?: unknown; content?: unknown; data?: unknown };

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function asText(value: unknown) {
  return typeof value === 'string' ? value : value === undefined || value === null ? '' : JSON.stringify(value);
}

function asParams(value: unknown): JsonRecord {
  return asRecord(value) as JsonRecord;
}

export function replayDisplayEvents(rawEvents: unknown[]): Message[] {
  const messages: Message[] = [];
  let nextId = 0;
  const add = (message: Message) => messages.push({ ...message, id: ++nextId });
  const updateRun = (runId: string, update: (run: AgentRun) => AgentRun) => {
    const message = messages.find(item => item.run?.id === runId);
    if (message?.run) message.run = update(message.run);
  };

  for (const rawEvent of rawEvents) {
    const event = asRecord(rawEvent) as StoredEvent;
    const type = typeof event.type === 'string' ? event.type : '';
    const data = asRecord(event.data);
    if (type === 'user_message') {
      add({ role: 'user', content: asText(event.content) });
      continue;
    }
    if (type === 'text_delta') {
      if (data.source === 'subagent' && typeof data.run_id === 'string') {
        updateRun(data.run_id, run => ({ ...run, draft: run.draft + asText(data.delta) }));
        continue;
      }
      const last = messages.at(-1);
      if (last?.role === 'assistant' && !last.tool_calls?.length) last.content += asText(data.delta);
      else add({ role: 'assistant', content: asText(data.delta) });
      continue;
    }
    if (type === 'thinking') {
      if (data.source === 'subagent' && typeof data.run_id === 'string') {
        updateRun(data.run_id, run => {
          const last = run.events.at(-1);
          const events: AgentRunEvent[] = last?.type === 'thinking'
            ? [...run.events.slice(0, -1), { ...last, content: `${last.content ?? ''}${asText(data.content)}` }]
            : [...run.events, { id: ++nextId, type: 'thinking', content: asText(data.content) }];
          return { ...run, events };
        });
      }
      continue;
    }
    if (type === 'tool_call') {
      if (data.source === 'subagent' && typeof data.run_id === 'string') {
        updateRun(data.run_id, run => ({ ...run, events: [...run.events, { id: ++nextId, type: 'tool_call', tool: asText(data.tool), params: asParams(data.params) }] }));
      } else if (data.tool !== 'SubAgent') {
        add({ role: 'system', content: `🔧 ${asText(data.tool)}`, name: 'tool_call', tool_calls: [{ tool: asText(data.tool), params: asParams(data.params) }] });
      }
      continue;
    }
    if (type === 'tool_result') {
      if (data.source === 'subagent' && typeof data.run_id === 'string') {
        updateRun(data.run_id, run => ({ ...run, events: [...run.events, { id: ++nextId, type: 'tool_result', tool: asText(data.tool), success: Boolean(data.success) }] }));
      } else {
        const success = Boolean(data.success);
        add({ role: 'tool_result', content: success ? asText(data.data) : `❌ ${asText(data.error) || '工具执行失败'}`, name: asText(data.tool) });
      }
      continue;
    }
    if (type === 'subagent_start' && typeof data.run_id === 'string') {
      add({
        role: 'agent_run', content: '', run: {
          id: data.run_id,
          preset: asText(data.preset),
          parentRunId: typeof data.parent_run_id === 'string' ? data.parent_run_id : undefined,
          workflow: typeof data.workflow === 'string' ? data.workflow : undefined,
          taskSummary: asText(data.task_summary), status: 'running', draft: '', result: '', events: [],
          taskPrompt: asText(data.task_prompt) || asText(data.task_summary),
        },
      });
      continue;
    }
    if (type === 'subagent_result' && typeof data.run_id === 'string') {
      updateRun(data.run_id, run => ({ ...run, result: asText(data.content), draft: '' }));
      continue;
    }
    if (type === 'subagent_done' && typeof data.run_id === 'string') {
      updateRun(data.run_id, run => ({ ...run, status: 'completed', result: asText(data.result) || run.result || run.draft, draft: '' }));
      continue;
    }
    if (type === 'error') {
      if (data.source === 'subagent' && typeof data.run_id === 'string') updateRun(data.run_id, run => ({ ...run, status: 'failed', error: asText(data.message) }));
      else add({ role: 'system', content: `⚠️ ${asText(data.message)}` });
    }
  }
  return messages;
}
