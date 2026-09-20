import type { LifecycleRecord } from '../types/insights';

interface LifecycleRecordDetailDialogProps {
  record: LifecycleRecord;
  onClose: () => void;
  onOpenTrace: (traceId: string) => void;
}

export function LifecycleRecordDetailDialog({ record, onClose, onOpenTrace }: LifecycleRecordDetailDialogProps) {
  const traceRefs = relatedTraceRefs(record);
  const metadata = Object.entries(record).filter(([key]) => !['content', 'claim', 'title'].includes(key));
  return <div className="absolute inset-0 z-30 flex flex-col bg-white" role="dialog" aria-modal="true" aria-labelledby="lifecycle-record-title">
    <header className="flex shrink-0 items-start gap-4 border-b border-gray-100 px-6 py-5">
      <div className="min-w-0 flex-1">
        <p className="text-xs font-medium uppercase tracking-wide text-purple-600">{record.layer} 详情</p>
        <h3 id="lifecycle-record-title" className="mt-1 break-all font-mono text-sm font-semibold text-gray-800">{record.id}</h3>
      </div>
      <button type="button" onClick={onClose} aria-label="关闭记录详情" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-700">✕</button>
    </header>
    <main className="min-h-0 flex-1 space-y-5 overflow-y-auto px-6 py-5">
      <section><h4 className="mb-2 text-sm font-semibold text-gray-700">标题</h4><p className="rounded-xl bg-gray-50 p-4 text-sm leading-6 text-gray-700">{record.title || record.claim}</p></section>
      <section><h4 className="mb-2 text-sm font-semibold text-gray-700">完整正文</h4><pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-gray-50 p-4 font-sans text-sm leading-6 text-gray-700">{record.content || record.claim}</pre></section>
      <section>
        <h4 className="mb-2 text-sm font-semibold text-gray-700">记录字段</h4>
        <div className="space-y-3 rounded-xl border border-gray-100 p-4">{metadata.map(([key, value]) => <div key={key} className="grid gap-1 text-xs md:grid-cols-[10rem_1fr]"><div className="font-mono text-gray-400">{key}</div><pre className="overflow-auto whitespace-pre-wrap break-words font-mono text-gray-700">{formatValue(value)}</pre></div>)}</div>
      </section>
      <section>
        <h4 className="mb-2 text-sm font-semibold text-gray-700">关联 Trace</h4>
        <div className="flex flex-wrap gap-2 rounded-xl border border-gray-100 p-4">{traceRefs.map(ref => <button key={`${ref.trace_id}-${ref.turn ?? 'legacy'}`} type="button" onClick={() => onOpenTrace(ref.trace_id)} className="break-all rounded-md bg-purple-50 px-2 py-1 font-mono text-xs text-purple-700 hover:bg-purple-100 hover:underline">{ref.trace_id}{ref.turn ? ` · Turn ${ref.turn}` : ''}</button>)}{traceRefs.length === 0 && <span className="text-xs text-gray-400">（无关联 Trace）</span>}</div>
      </section>
    </main>
  </div>;
}

function relatedTraceRefs(record: LifecycleRecord) {
  const refs = (record.trace_refs ?? []).filter(ref => ref && typeof ref.trace_id === 'string');
  const seen = new Set(refs.map(ref => ref.trace_id));
  const legacy = [record.trace_id, ...(record.trace_ids ?? [])]
    .filter((value): value is string => typeof value === 'string' && value.length > 0 && !seen.has(value))
    .map(trace_id => ({ trace_id, turn: 0 }));
  return [...refs, ...legacy];
}

function formatValue(value: unknown) {
  if (typeof value === 'string') return value;
  if (value === undefined) return 'undefined';
  return JSON.stringify(value, null, 2);
}
