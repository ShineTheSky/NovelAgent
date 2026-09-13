import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import type { Message, PendingAsk, PendingQuestion, ToolCall } from '../types/chat';
import { AgentRunCard } from './AgentRunCard';
import { MessageBubble } from './MessageBubble';
import { PermissionDialog } from './PermissionDialog';
import { QuestionDialog } from './QuestionDialog';
import { ToolCard } from './ToolCard';
import { ToolResultCard } from './ToolResultCard';

interface ChatTimelineProps {
  messages: Message[];
  loading: boolean;
  currentAction: string;
  pendingAsk: PendingAsk | null;
  pendingQuestion: PendingQuestion | null;
  canRetry: boolean;
  onRetry: () => void;
}

function toolDetails(toolCall: ToolCall | undefined) {
  if (!toolCall) return { tool: '', params: {} };
  if (toolCall.tool) return { tool: toolCall.tool, params: toolCall.params ?? toolCall.input ?? {} };

  const argumentsText = toolCall.function?.arguments;
  if (!argumentsText) return { tool: toolCall.function?.name ?? '', params: {} };
  try {
    const parsed: unknown = JSON.parse(argumentsText);
    return { tool: toolCall.function?.name ?? '', params: typeof parsed === 'object' && parsed !== null ? parsed : {} };
  } catch {
    return { tool: toolCall.function?.name ?? '', params: { arguments: argumentsText } };
  }
}

function MessageItem({ message }: { message: Message }) {
  if (message.name === 'tool_call' || message.tool_calls?.length) {
    const { tool, params } = toolDetails(message.tool_calls?.[0]);
    return <ToolCard tool={tool} params={params} />;
  }
  if (message.role === 'tool_result') return <ToolResultCard toolName={message.name ?? ''} content={message.content} />;
  return <MessageBubble role={message.role} content={message.content} />;
}

export function ChatTimeline({
  messages,
  loading,
  currentAction,
  pendingAsk,
  pendingQuestion,
  canRetry,
  onRetry,
}: ChatTimelineProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);

  useEffect(() => {
    if (followLatestRef.current) endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleScroll = (event: React.UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    followLatestRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48;
  };

  const runs = messages.flatMap(message => message.run ? [message.run] : []);
  const childRuns = new Map<string, typeof runs>();
  for (const run of runs) {
    if (!run.parentRunId) continue;
    childRuns.set(run.parentRunId, [...(childRuns.get(run.parentRunId) ?? []), run]);
  }
  const elements: ReactNode[] = messages.flatMap((message, index) => {
    if (message.run) {
      if (message.run.parentRunId && runs.some(run => run.id === message.run!.parentRunId)) return [];
      return <AgentRunCard key={message.run.id} run={message.run} children={childRuns.get(message.run.id)} />;
    }
    return <div key={message.id ?? index}><MessageItem message={message} /></div>;
  });

  return (
    <div className="flex-1 overflow-y-auto" role="log" aria-live="polite" aria-label="对话消息" onScroll={handleScroll}>
      {messages.length === 0 ? (
        <div className="flex flex-col items-center justify-center h-full text-gray-300 select-none">
          <div className="text-6xl mb-4">✎</div>
          <div className="text-lg font-medium text-gray-400">NovelAgent2</div>
          <div className="text-sm text-gray-300 mt-1">选择或创建一个项目开始创作</div>
        </div>
      ) : (
        <div className="max-w-3xl mx-auto px-4 py-6 space-y-4">
          {elements}
          {loading && (
            <div className="flex w-full min-w-0 items-center gap-2 px-1 py-2">
              <div className="flex shrink-0 gap-1"><div className="w-2 h-2 bg-purple-400 rounded-full animate-bounce motion-reduce:animate-none" /><div className="w-2 h-2 bg-purple-400 rounded-full animate-bounce motion-reduce:animate-none" style={{ animationDelay: '150ms' }} /><div className="w-2 h-2 bg-purple-400 rounded-full animate-bounce motion-reduce:animate-none" style={{ animationDelay: '300ms' }} /></div>
              <span className="ml-2 min-w-0 flex-1 truncate whitespace-nowrap text-xs text-gray-400">{currentAction || 'AI 正在思考…'}</span>
            </div>
          )}
          {canRetry && <div className="flex justify-center my-2"><button type="button" onClick={onRetry} className="text-xs text-purple-600 hover:text-purple-700 border border-purple-200 rounded-full px-3 py-1">重试</button></div>}
          <div ref={endRef} />
        </div>
      )}
      {pendingAsk && <PermissionDialog ask={pendingAsk} />}
      {pendingQuestion && <QuestionDialog qa={pendingQuestion} />}
    </div>
  );
}
