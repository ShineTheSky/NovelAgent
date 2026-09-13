import { useState } from 'react';
import type { JsonRecord } from '../types/chat';

const toolLabels: Record<string, { icon: string; label: string }> = {
  CreateTraceCheckpoint: { icon: '✦', label: '记录关键对话要点' },
  Read: { icon: '⌕', label: '读取文件' },
  Write: { icon: '✎', label: '写入文件' },
  Edit: { icon: '✎', label: '编辑文件' },
  Glob: { icon: '⌕', label: '查找文件' },
  Grep: { icon: '⌕', label: '搜索内容' },
  SearchRag: { icon: '⌕', label: '检索资料库' },
};

function parameterSummary(tool: string, params: JsonRecord) {
  if (tool === 'CreateTraceCheckpoint' && typeof params.reason === 'string') return params.reason;
  const firstText = Object.values(params).find(value => typeof value === 'string');
  if (typeof firstText === 'string') return firstText;
  return Object.keys(params).length ? '查看参数' : '';
}

export function ToolCard({ tool, params }: { tool: string; params: JsonRecord }) {
  const [open, setOpen] = useState(false);
  const presentation = toolLabels[tool] ?? { icon: '⚙', label: tool };
  const summary = parameterSummary(tool, params);
  const details = JSON.stringify(params, null, 2);
  return (
    <div className="my-2 flex flex-col items-center">
      <button type="button" className="inline-flex max-w-full items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 text-xs text-slate-600 transition-colors hover:border-violet-200 hover:bg-violet-50/60" onClick={() => setOpen(value => !value)} aria-expanded={open}>
        <span className="flex size-5 shrink-0 items-center justify-center rounded-md bg-violet-100 text-[11px] text-violet-600">{presentation.icon}</span>
        <span className="shrink-0 font-medium text-slate-700">{presentation.label}</span>
        {summary && <span className="max-w-64 truncate text-slate-400">{summary}</span>}
        <span className="shrink-0 text-[10px] text-slate-400">{open ? '收起' : '详情'}</span>
      </button>
      {open && <pre className="mt-2 w-full max-w-lg overflow-x-auto rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs leading-5 text-slate-600 whitespace-pre-wrap">{details}</pre>}
    </div>
  );
}
