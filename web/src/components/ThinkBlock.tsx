import { useState } from 'react';

export function ThinkBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (!text?.trim()) return null;
  return (
    <div className="my-1 w-full min-w-0">
      <button onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-[11px] text-amber-600 bg-amber-50 hover:bg-amber-100 border border-amber-200 rounded-full px-3 py-0.5 transition-colors">
        <span>{open ? '▲' : '▼'}</span>
        <span>思考过程</span>
      </button>
      {open && (
        <div className="mt-1 max-h-48 overflow-y-auto break-words rounded-lg border border-amber-100 bg-amber-50/50 p-2.5 text-xs leading-relaxed text-amber-800 whitespace-pre-wrap italic">
          {text}
        </div>
      )}
    </div>
  );
}
