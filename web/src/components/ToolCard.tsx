import { useState } from 'react';
import type { JsonRecord } from '../types/chat';

export function ToolCard({ tool, params }: { tool: string; params: JsonRecord }) {
  const [open, setOpen] = useState(false);
  const summary = JSON.stringify(params);
  return (
    <div className="flex justify-center my-1">
      <button type="button" className="inline-flex items-center gap-2 bg-amber-50 border border-amber-200 rounded-full px-3 py-1 text-sm cursor-pointer hover:bg-amber-100 transition-colors" onClick={() => setOpen(value => !value)} aria-expanded={open}>
        <span className="text-amber-600">⚙</span>
        <span className="text-amber-700 font-medium">{tool}</span>
        {!open && <span className="text-amber-400 font-mono text-xs truncate max-w-[200px]">{summary.slice(0, 80)}</span>}
      </button>
      {open && <div className="ml-2 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 font-mono text-sm text-amber-800 max-h-32 overflow-auto whitespace-pre-wrap">{summary}</div>}
    </div>
  );
}
