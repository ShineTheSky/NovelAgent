import { useCallback, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { createPortal } from 'react-dom';
import { cancelLifecycleManualReview, downgradeLifecycleRecord, fetchInsights, fetchMemories, fetchPatterns, fetchTrace } from '../api/client';
import type { LifecycleRecord, TraceEvidence } from '../types/insights';
import { LifecycleRecordDetailDialog } from './LifecycleRecordDetailDialog';
import { TraceDetailDialog } from './TraceDetailDialog';

type Layer = 'insight' | 'memory' | 'pattern';
const label: Record<Layer, string> = { insight: 'Insight', memory: 'Memory', pattern: 'Pattern' };
const errorText = (error: unknown) => error instanceof Error ? error.message : '加载详情失败';

export function FileInsightsPanel({ projectId }: { projectId: string | null }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Layer>('insight');
  const [data, setData] = useState<Record<Layer, LifecycleRecord[]>>({ insight: [], memory: [], pattern: [] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedRecord, setSelectedRecord] = useState<LifecycleRecord | null>(null);
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  const [traceDetails, setTraceDetails] = useState<Record<string, TraceEvidence>>({});
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceError, setTraceError] = useState<string | null>(null);
  const [panelOffset, setPanelOffset] = useState({ x: 0, y: 0 });
  const dragStart = useRef<{ pointerX: number; pointerY: number; offsetX: number; offsetY: number } | null>(null);

  const load = useCallback(async () => {
    if (!projectId) return;
    setBusy(true);
    setError(null);
    try {
      const [insight, memory, pattern] = await Promise.all([
        fetchInsights(projectId), fetchMemories(projectId), fetchPatterns(projectId),
      ]);
      setData({
        insight: insight.filter(item => item.category !== 'agent'),
        memory: memory.filter(item => item.category !== 'agent'),
        pattern: pattern.filter(item => item.category !== 'agent'),
      });
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setBusy(false);
    }
  }, [projectId]);

  const showPanel = () => {
    if (!projectId) return;
    setOpen(true);
    setSelectedRecord(null);
    setSelectedTraceId(null);
    void load();
  };

  const openTrace = async (traceId: string) => {
    setSelectedRecord(null);
    setSelectedTraceId(traceId);
    setTraceError(null);
    if (traceDetails[traceId]?.events) return;
    setTraceLoading(true);
    try {
      const detail = await fetchTrace(traceId);
      setTraceDetails(current => ({ ...current, [traceId]: { ...detail, event_count: detail.event_count ?? detail.events?.length ?? 0 } }));
    } catch (requestError) {
      setTraceError(errorText(requestError));
    } finally {
      setTraceLoading(false);
    }
  };

  const downgrade = async (record: LifecycleRecord) => {
    if (!projectId || record.layer === 'insight' || !confirm(`降低“${record.title || record.claim}”的层级？`)) return;
    await downgradeLifecycleRecord(projectId, record.layer, record.id);
    await load();
  };
  const cancelManualReview = async (record: LifecycleRecord) => {
    if (!projectId || record.layer === 'pattern' || !confirm(`取消“${record.title || record.claim}”的自动晋级锁定？取消后不会立即升级，但后续满足条件时可以自动晋级。`)) return;
    await cancelLifecycleManualReview(projectId, record.layer, record.id);
    await load();
  };

  const startDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if ((event.target as HTMLElement).closest('button')) return;
    dragStart.current = { pointerX: event.clientX, pointerY: event.clientY, offsetX: panelOffset.x, offsetY: panelOffset.y };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const dragPanel = (event: ReactPointerEvent<HTMLElement>) => {
    const start = dragStart.current;
    if (start) setPanelOffset({ x: start.offsetX + event.clientX - start.pointerX, y: start.offsetY + event.clientY - start.pointerY });
  };
  const stopDrag = () => { dragStart.current = null; };
  const selectedTrace = selectedTraceId ? traceDetails[selectedTraceId] ?? null : null;

  return <>
    <button type="button" onClick={showPanel} disabled={!projectId} className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 shadow-sm hover:border-purple-200 hover:text-purple-600 disabled:opacity-40">项目洞察</button>
    {open && createPortal(
      <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4" role="dialog" aria-modal="true">
        <div style={{ transform: `translate(${panelOffset.x}px, ${panelOffset.y}px)` }} className="relative flex max-h-[80vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">
          <header onPointerDown={startDrag} onPointerMove={dragPanel} onPointerUp={stopDrag} onPointerCancel={stopDrag} className="flex cursor-grab touch-none items-start justify-between border-b px-6 py-5 active:cursor-grabbing">
            <div><h2 className="font-semibold text-gray-800">项目洞察</h2><p className="mt-1 text-xs text-gray-400">点击记录 ID 查看正文；点击 Trace ID 回溯完整事件。</p></div>
            <button type="button" onClick={() => setOpen(false)} className="text-gray-400 hover:text-gray-700">✕</button>
          </header>
          <div className="flex items-center gap-1 bg-gray-50 px-6 py-3">
            {(Object.keys(label) as Layer[]).map(layer => <button type="button" key={layer} onClick={() => setTab(layer)} className={`rounded-lg px-3 py-1.5 text-sm ${tab === layer ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500'}`}>{label[layer]} ({data[layer].length})</button>)}
            <button type="button" onClick={() => void load()} className="ml-auto text-xs text-purple-600">{busy ? '刷新中…' : '刷新'}</button>
          </div>
          <main className="min-h-0 flex-1 overflow-y-auto p-5">
            {error && <div role="alert" className="mb-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</div>}
            {data[tab].map(record => <RecordCard key={record.id} record={record} tab={tab} onOpen={() => setSelectedRecord(record)} onOpenTrace={traceId => void openTrace(traceId)} onDowngrade={() => void downgrade(record)} onCancelManualReview={() => void cancelManualReview(record)} />)}
            {!busy && data[tab].length === 0 && <p className="py-16 text-center text-sm text-gray-400">暂无 {label[tab]}。</p>}
          </main>
          {selectedRecord && <LifecycleRecordDetailDialog record={selectedRecord} onClose={() => setSelectedRecord(null)} onOpenTrace={traceId => void openTrace(traceId)} />}
          {selectedTraceId && <TraceDetailDialog trace={selectedTrace} loading={traceLoading} error={traceError} onClose={() => { setSelectedTraceId(null); setTraceError(null); }} />}
        </div>
      </div>,
      document.body,
    )}
  </>;
}

function RecordCard({ record, tab, onOpen, onOpenTrace, onDowngrade, onCancelManualReview }: {
  record: LifecycleRecord;
  tab: Layer;
  onOpen: () => void;
  onOpenTrace: (traceId: string) => void;
  onDowngrade: () => void;
  onCancelManualReview: () => void;
}) {
  const traceRefs = relatedTraceRefs(record);
  return <article className={`mb-3 rounded-xl border bg-white p-4 shadow-sm ${record.promotion_status === 'manual_review' ? 'border-amber-200' : 'border-gray-100'}`}>
    <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-gray-400">
      <button type="button" onClick={onOpen} title={`查看 ${record.layer} 完整内容`} className="max-w-full break-all rounded-md bg-purple-50 px-2 py-1 font-mono text-purple-700 hover:bg-purple-100">{record.id}</button>
      <span>类别 {record.category}</span>
      <span>领域 {record.domain}</span>
      {record.kind && <span className="rounded-full bg-rose-50 px-2 py-0.5 text-rose-600">类型 {kindLabel(record.kind)}</span>}
      <span>权重 {Math.round(record.weight)}</span>
      {record.promotion_status === 'manual_review' && <span title={record.downgrade_reason} className="rounded-full bg-amber-50 px-2 py-0.5 font-medium text-amber-700">自动晋级已锁定</span>}
      <span className="ml-auto">{record.updated.slice(0, 16).replace('T', ' ')}</span>
    </div>
    <button type="button" onClick={onOpen} className="w-full text-left text-sm leading-6 text-gray-700 hover:text-purple-700">{record.title || record.claim}</button>
    {record.promotion_status === 'manual_review' && <p className="mt-2 text-xs leading-5 text-amber-700">{record.downgrade_reason || '用户曾手动降低该记录层级，后续不会自动恢复。'}</p>}
    <div className="mt-3 flex flex-wrap items-center gap-3 text-[11px] text-gray-400">
      {traceRefs.map(ref => <button type="button" key={`${ref.trace_id}-${ref.turn ?? 'legacy'}`} onClick={() => onOpenTrace(ref.trace_id)} title={ref.trace_id} className="font-mono text-purple-600 hover:text-purple-800 hover:underline">Trace {ref.trace_id}{ref.turn ? ` · Turn ${ref.turn}` : ''}</button>)}
      {traceRefs.length === 0 && <span>Trace -</span>}
      <span>事件 {record.source_event_ids?.length || 0}</span>
      {record.support_count && <span>支持 {record.support_count}</span>}
      <span className="ml-auto flex gap-3">
        {record.promotion_status === 'manual_review' && <button type="button" onClick={onCancelManualReview} className="text-purple-600 hover:text-purple-800">取消锁定</button>}
        {tab !== 'insight' && <button type="button" onClick={onDowngrade} className="text-amber-700 hover:text-amber-900">降低层级</button>}
      </span>
    </div>
  </article>;
}

function relatedTraceRefs(record: LifecycleRecord) {
  const refs = (record.trace_refs ?? []).filter(ref => ref && typeof ref.trace_id === 'string');
  const seen = new Set(refs.map(ref => ref.trace_id));
  const legacy = [record.trace_id, ...(record.trace_ids ?? [])]
    .filter((value): value is string => typeof value === 'string' && value.length > 0 && !seen.has(value))
    .map(trace_id => ({ trace_id, turn: 0 }));
  return [...refs, ...legacy];
}

function kindLabel(kind: string) {
  return ({ text_feedback: '文本反馈', review_issue: '高频写作错误' } as Record<string, string>)[kind] ?? kind;
}
