import { type Message } from '../hooks/useChat';
import { MessageBubble } from './MessageBubble';
import { ToolCard } from './ToolCard';
import { ToolResultCard } from './ToolResultCard';
import { SubagentResultCard } from './SubagentResultCard';
import { presetColors, defaultPresetColor } from './presetColors';

export function SubagentGroup({ messages, preset }: { messages: Message[]; preset: string }) {
  const c = presetColors[preset] || defaultPresetColor;
  const isComplete = messages.some(m => m.role === 'subagent_result' || m.role === 'system' && m.content?.startsWith('✅'));

  return (
    <div className={`rounded-xl overflow-hidden border ${c.border} shadow-sm my-3`}>
      <div className={`${c.bar} text-white text-xs font-semibold px-4 py-2 tracking-wide flex items-center gap-2`}>
        <span>子Agent ({preset || '?'})</span>
        <span className="opacity-70 font-normal text-[10px]">{isComplete ? '已完成' : '工作中…'}</span>
      </div>
      <div className={`${c.bg} px-3 py-2 space-y-2`}>
        {messages.map((m, i) => (
          <div key={m.id || `sgm-${i}`}>
            {m.name === 'tool_call' ? <ToolCard tool={m.tool_calls?.[0]?.tool || ''} params={m.tool_calls?.[0]?.input || {}} />
            : m.role === 'tool_result' ? <ToolResultCard toolName={m.name || ''} content={m.content} />
            : m.role === 'subagent_result' ? <SubagentResultCard preset={m.preset || preset} content={m.content} />
            : <MessageBubble role={m.role} content={m.content} />}
          </div>
        ))}
      </div>
    </div>
  );
}
