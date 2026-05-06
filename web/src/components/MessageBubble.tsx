import { useState } from 'react';
import { ThinkBlock } from './ThinkBlock';

export function MessageBubble({ role, content, ts }: { role: string; content: string; ts?: string }) {
  if (!content?.trim()) return null;
  const isUser = role === 'user';
  const isSystem = role === 'system';
  const [copied, setCopied] = useState(false);
  const time = ts ? new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '';

  const copy = () => { navigator.clipboard.writeText(content); setCopied(true); setTimeout(() => setCopied(false), 1500); };

  if (isSystem) {
    return (
      <div className="flex justify-center my-1">
        <span className="text-xs text-gray-400 bg-gray-50 px-3 py-1 rounded-full">{content}</span>
      </div>
    );
  }

  const thinkRegex = /<think>([\s\S]*?)<\/think>/g;
  const parts: { type: 'think' | 'text'; content: string }[] = [];
  let lastIdx = 0, match;
  while ((match = thinkRegex.exec(content)) !== null) {
    if (match.index > lastIdx) parts.push({ type: 'text', content: content.slice(lastIdx, match.index) });
    parts.push({ type: 'think', content: match[1].trim() });
    lastIdx = match.index + match[0].length;
  }
  if (lastIdx < content.length) parts.push({ type: 'text', content: content.slice(lastIdx) });
  const hasThink = parts.some(p => p.type === 'think');

  const Bubble = ({ text }: { text: string }) => (
    <div className={`rounded-2xl px-4 py-3 text-base leading-relaxed whitespace-pre-wrap relative group ${isUser ? 'bg-purple-600 text-white rounded-br-md' : 'bg-white text-gray-700 border border-gray-100 rounded-bl-md shadow-sm'}`}>
      {text}
      {!isUser && text.length > 50 && (
        <button onClick={copy} aria-label={copied ? '已复制' : '复制消息'} className="absolute top-1 right-2 opacity-0 group-hover:opacity-100 text-[10px] text-gray-400 hover:text-gray-600 bg-white/80 rounded px-1.5 py-0.5 transition-opacity">
          {copied ? '已复制' : '复制'}
        </button>
      )}
    </div>
  );

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} my-1`}>
      {!isUser && <div className="w-7 h-7 rounded-full bg-purple-100 flex items-center justify-center text-xs mr-2 mt-0.5 shrink-0 select-none" aria-hidden="true">N</div>}
      <div className="max-w-[80%]">
        {hasThink ? parts.map((p, i) =>
          p.type === 'think' ? <ThinkBlock key={i} text={p.content} /> :
          p.content.trim() ? <Bubble key={i} text={p.content} /> : null
        ) : <Bubble text={content} />}
        {time && <div className={`text-[10px] text-gray-400 mt-0.5 ${isUser ? 'text-right mr-1' : 'ml-1'}`}>{time}</div>}
      </div>
      {isUser && <div className="w-7 h-7 rounded-full bg-purple-600 flex items-center justify-center text-xs ml-2 mt-0.5 shrink-0 text-white select-none" aria-hidden="true">U</div>}
    </div>
  );
}
