import type { LifecycleRecord } from '../types/insights';

interface LifecycleRecordDetailDialogProps {
  record: LifecycleRecord;
  onClose: () => void;
  onOpenTrace: (traceId: string) => void;
  onOpenHistory: (historyId: string) => void;
  onOpenKnowledge?: (id: string, version: number) => void;
}

export function LifecycleRecordDetailDialog({ record, onClose, onOpenTrace, onOpenHistory, onOpenKnowledge }: LifecycleRecordDetailDialogProps) {
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
      {record.schema === 'knowledge_v1' && <section className="space-y-2 text-xs">
        <p>版本 v{record.version} · {record.status}</p>
        {!!record.previous_version && <button type="button" onClick={() => onOpenKnowledge?.(record.id, record.previous_version!)} className="text-purple-600 hover:underline">查看上一版本 v{record.previous_version}</button>}
        {!!record.replacements?.length && <div><p className="mb-2 text-amber-700">此版本已被以下知识替代，原始内容和来源仍保留：</p>{record.replacements.map(next => <button type="button" key={`${next.new_id}:${next.new_version}`} onClick={() => onOpenKnowledge?.(next.new_id, next.new_version)} className="mr-2 break-all text-purple-600 hover:underline">{next.new_id}@v{next.new_version}{next.reason ? ` · ${next.reason}` : ''}</button>)}</div>}
        {!!record.memory_sources?.length && <div className="flex flex-wrap gap-2">{record.memory_sources.map(source => <button type="button" key={`${source.memory_id}:${source.version}:${source.role}`} onClick={() => onOpenKnowledge?.(source.memory_id, source.version)} className="break-all rounded-md bg-purple-50 px-2 py-1 font-mono text-purple-700">{source.role}: {source.memory_id}@v{source.version}</button>)}</div>}
      </section>}
      {!!record.provenance_notes?.length && <section><h4 className="mb-2 text-sm font-semibold text-gray-700">来源核验说明</h4><div className="space-y-2 rounded-xl bg-amber-50 p-4 text-xs leading-5 text-amber-800">{record.provenance_notes.map(note => <p key={note}>{note}</p>)}</div></section>}
      <section>
        <h4 className="mb-2 text-sm font-semibold text-gray-700">来源 History（需求根或独立审阅节点）</h4>
        <div className="flex flex-wrap gap-2 rounded-xl border border-gray-100 p-4">
          {(record.history_ids ?? []).map(historyId => <button key={historyId} type="button" onClick={() => onOpenHistory(historyId)} className="break-all rounded-md bg-purple-50 px-2 py-1 font-mono text-xs text-purple-700 hover:bg-purple-100 hover:underline">{historyId}</button>)}
          {!record.history_ids?.length && <span className="text-xs text-gray-400">（无关联 History）</span>}
        </div>
        {record.history_source_links?.some(link => link.match === 'context') && <p className="mt-2 text-xs text-amber-700">部分关联仅作上下文，不是当前记忆的直接支持。</p>}
      </section>
      {!!record.related_history_ids?.length && <details className="text-xs text-gray-500"><summary className="cursor-pointer">相关根 History（非直接来源）· {record.related_history_ids.length} 条</summary><div className="mt-2 flex flex-wrap gap-2">{record.related_history_ids.map(id => <button key={id} type="button" onClick={() => onOpenHistory(id)} className="break-all font-mono text-purple-600 hover:underline">{id}</button>)}</div></details>}
      <section>
        <h4 className="mb-2 text-sm font-semibold text-gray-700">记录字段</h4>
        <div className="space-y-3 rounded-xl border border-gray-100 p-4">{metadata.map(([key, value]) => <div key={key} className="grid gap-1 text-xs md:grid-cols-[10rem_1fr]"><div className="font-mono text-gray-400">{key}</div><pre className="overflow-auto whitespace-pre-wrap break-words font-mono text-gray-700">{formatValue(value)}</pre></div>)}</div>
      </section>
      {traceRefs.length > 0 && <details className="text-xs text-gray-500">
        <summary className="cursor-pointer">原始 Trace（诊断与旧数据溯源）· {traceRefs.length} 条</summary>
        <div className="mt-2 flex flex-wrap gap-2 rounded-xl border border-gray-100 p-4">{traceRefs.map(ref => <button key={`${ref.trace_id}-${ref.turn ?? 'legacy'}`} type="button" onClick={() => onOpenTrace(ref.trace_id)} className="break-all rounded-md bg-gray-50 px-2 py-1 font-mono text-xs text-gray-600 hover:bg-gray-100 hover:underline">{ref.trace_id}{ref.turn ? ` · Turn ${ref.turn}` : ''}</button>)}</div>
      </details>}
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
