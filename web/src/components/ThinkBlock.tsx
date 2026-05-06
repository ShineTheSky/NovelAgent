import { useState } from 'react';

export function ThinkBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (!text?.trim()) return null;
  return (
    <div className="my-1">
      <button onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-[11px] text-amber-600 bg-amber-50 hover:bg-amber-100 border border-amber-200 rounded-full px-3 py-0.5 transition-colors">
        <span>{open ? '▲' : '▼'}</span>
        <span>思考过程</span>
      </button>
      {open && (
        <div className="mt-1 bg-amber-50/50 border border-amber-100 rounded-lg p-2.5 text-xs text-amber-800 leading-relaxed whitespace-pre-wrap max-h-48 overflow-y-auto italic">
          {text}
        </div>
      )}
    </div>
  );
}
