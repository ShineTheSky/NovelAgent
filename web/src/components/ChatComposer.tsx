import { useRef } from 'react';

interface ChatComposerProps {
  input: string;
  loading: boolean;
  onInputChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
}

export function ChatComposer({ input, loading, onInputChange, onSend, onStop }: ChatComposerProps) {
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const resize = () => {
    const element = inputRef.current;
    if (!element) return;
    element.style.height = 'auto';
    element.style.height = `${Math.min(element.scrollHeight, 160)}px`;
  };

  return (
    <div className="border-t border-gray-200 bg-white px-4 py-3 shrink-0">
      <div className="max-w-3xl mx-auto flex gap-3 items-end">
        <textarea
          ref={inputRef}
          value={input}
          onChange={event => { onInputChange(event.target.value); resize(); }}
          onKeyDown={event => { if (event.key === 'Enter' && event.ctrlKey) { event.preventDefault(); onSend(); } }}
          placeholder="输入消息… (Ctrl+Enter 发送)"
          rows={1}
          autoComplete="off"
          className="flex-1 resize-none rounded-xl border border-gray-200 px-4 py-3 text-sm outline-none focus-visible:border-purple-300 focus-visible:ring-2 focus-visible:ring-purple-100 placeholder:text-gray-400 transition-all"
          disabled={loading}
        />
        {loading ? (
          <button type="button" onClick={onStop} aria-label="停止生成" className="shrink-0 w-10 h-10 flex items-center justify-center rounded-xl bg-red-50 text-red-500 hover:bg-red-100">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="1" /></svg>
          </button>
        ) : (
          <button type="button" onClick={onSend} disabled={!input.trim()} aria-label="发送消息" className="shrink-0 w-10 h-10 flex items-center justify-center rounded-xl bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-30">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M1.5 1.5L14.5 8L1.5 14.5L4 8L1.5 1.5Z" /></svg>
          </button>
        )}
      </div>
    </div>
  );
}
