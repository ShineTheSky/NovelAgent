import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { AgentRun } from '../types/chat';
import { defaultPresetColor, presetColors } from './presetColors';

const presetNames: Record<string, string> = {
  chapter_writer: '章节写作',
  chapter_polisher: '章节润色',
  reviewer: '自动审阅',
  outliner: '大纲规划',
  character_designer: '角色设计',
};

function statusText(status: AgentRun['status']) {
  return status === 'running' ? '进行中' : status === 'completed' ? '已完成' : '失败';
}

export function AgentRunCard({ run, children = [] }: { run: AgentRun; children?: AgentRun[] }) {
  const [showDetails, setShowDetails] = useState(false);
  const [showResult, setShowResult] = useState(false);
  const c = presetColors[run.preset] || defaultPresetColor;
  const result = run.result || run.draft;
  const longResult = result.length > 900;
  const displayEvents = run.events.reduce<AgentRun['events']>((events, event) => {
    const lastEvent = events.at(-1);
    if (event.type === 'thinking' && lastEvent?.type === 'thinking') {
      return [...events.slice(0, -1), { ...lastEvent, content: `${lastEvent.content ?? ''}${event.content ?? ''}` }];
    }
    return [...events, event];
  }, []);

  return (
    <section className={`my-3 overflow-hidden rounded-xl border ${c.border} bg-white shadow-sm`}>
      <header className={`flex items-center justify-between gap-3 ${c.bar} px-4 py-2 text-xs text-white`}>
        <span className="font-semibold">{presetNames[run.preset] || run.preset || '子 Agent'}</span>
        <span className="rounded-full bg-white/20 px-2 py-0.5 text-[11px]">{statusText(run.status)}</span>
      </header>
      <div className="space-y-2 px-4 py-3">
        {run.taskSummary && <p className="text-xs leading-5 text-gray-500">{run.taskSummary}</p>}
        {run.status === 'running' && !result && <p className="text-xs text-gray-400">正在处理任务…</p>}
        {run.error && <p className="rounded-md bg-red-50 px-2 py-1.5 text-xs text-red-600">{run.error}</p>}
        {result && (
          <div className="text-sm text-gray-700">
            <div className={`${longResult && !showResult ? 'max-h-44 overflow-hidden' : ''} prose prose-sm max-w-none prose-p:leading-6`}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{result}</ReactMarkdown>
            </div>
            {longResult && <button type="button" onClick={() => setShowResult(value => !value)} className="mt-1 text-xs text-purple-600 hover:text-purple-700">{showResult ? '收起结果' : '展开完整结果'}</button>}
          </div>
        )}
        {displayEvents.length > 0 && (
          <div>
            <button type="button" onClick={() => setShowDetails(value => !value)} className="text-xs text-gray-500 hover:text-gray-700">{showDetails ? '隐藏执行详情' : `查看执行详情（${displayEvents.length}）`}</button>
            {showDetails && <div className="mt-2 space-y-1 border-l border-gray-200 pl-3 text-xs text-gray-500">
              {displayEvents.map(event => <p key={event.id} className={event.type === 'thinking' ? 'whitespace-pre-wrap break-words leading-5' : ''}>{event.type === 'thinking' ? (event.content || '分析中') : <>{event.type === 'tool_call' ? `调用 ${event.tool}` : `${event.tool} ${event.success ? '完成' : '失败'}`}{event.content ? `：${event.content}` : ''}</>}</p>)}
            </div>}
          </div>
        )}
        {children.length > 0 && <div className="space-y-2 border-t border-gray-100 pt-2">{children.map(child => <AgentRunCard key={child.id} run={child} />)}</div>}
      </div>
    </section>
  );
}
