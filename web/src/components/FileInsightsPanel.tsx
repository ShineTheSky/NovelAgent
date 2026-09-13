import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { createPortal } from 'react-dom';
import { downgradeLifecycleRecord, fetchEvidence, fetchMemories, fetchPatterns } from '../api/client';
import type { LifecycleRecord } from '../types/insights';

type Layer = 'evidence' | 'memory' | 'pattern';
const label: Record<Layer, string> = { evidence: 'Evidence', memory: 'Memory', pattern: 'Pattern' };

export function FileInsightsPanel({ projectId }: { projectId: string | null }) {
  const [open, setOpen] = useState(false); const [tab, setTab] = useState<Layer>('evidence');
  const [data, setData] = useState<Record<Layer, LifecycleRecord[]>>({ evidence: [], memory: [], pattern: [] });
  const [busy, setBusy] = useState(false);
  const [panelOffset, setPanelOffset] = useState({ x: 0, y: 0 });
  const dragStart = useRef<{ pointerX: number; pointerY: number; offsetX: number; offsetY: number } | null>(null);
  const load = useCallback(async () => {
    if (!projectId) return; setBusy(true);
    try { const [evidence, memory, pattern] = await Promise.all([fetchEvidence(projectId), fetchMemories(projectId), fetchPatterns(projectId)]); setData({ evidence: evidence.filter(item => item.category !== 'agent'), memory: memory.filter(item => item.category !== 'agent'), pattern: pattern.filter(item => item.category !== 'agent') }); }
    finally { setBusy(false); }
  }, [projectId]);
  useEffect(() => { if (open) void load(); }, [open, load]);
  const downgrade = async (record: LifecycleRecord) => { if (!projectId || record.layer === 'evidence' || !confirm(`降低“${record.title || record.claim}”的层级？`)) return; await downgradeLifecycleRecord(projectId, record.layer, record.id); await load(); };
  const startDrag = (event: ReactPointerEvent<HTMLElement>) => { if ((event.target as HTMLElement).closest('button')) return; dragStart.current = { pointerX: event.clientX, pointerY: event.clientY, offsetX: panelOffset.x, offsetY: panelOffset.y }; event.currentTarget.setPointerCapture(event.pointerId); };
  const dragPanel = (event: ReactPointerEvent<HTMLElement>) => { const start = dragStart.current; if (start) setPanelOffset({ x: start.offsetX + event.clientX - start.pointerX, y: start.offsetY + event.clientY - start.pointerY }); };
  const stopDrag = () => { dragStart.current = null; };
  return <>
    <button type="button" onClick={() => setOpen(true)} disabled={!projectId} className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 shadow-sm hover:border-purple-200 hover:text-purple-600 disabled:opacity-40">项目洞察</button>
    {open && createPortal(<div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4" role="dialog" aria-modal="true"><div style={{ transform: `translate(${panelOffset.x}px, ${panelOffset.y}px)` }} className="flex max-h-[80vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl"><header onPointerDown={startDrag} onPointerMove={dragPanel} onPointerUp={stopDrag} onPointerCancel={stopDrag} className="flex cursor-grab touch-none items-start justify-between border-b px-6 py-5 active:cursor-grabbing"><div><h2 className="font-semibold text-gray-800">项目洞察</h2><p className="mt-1 text-xs text-gray-400">Evidence → Memory → Pattern；列表只显示简要标题，Trace 保留在后台可回溯。</p></div><button onClick={() => setOpen(false)} className="text-gray-400 hover:text-gray-700">✕</button></header><div className="flex items-center gap-1 bg-gray-50 px-6 py-3">{(Object.keys(label) as Layer[]).map(layer => <button key={layer} onClick={() => setTab(layer)} className={`rounded-lg px-3 py-1.5 text-sm ${tab === layer ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500'}`}>{label[layer]} ({data[layer].length})</button>)}<button onClick={() => void load()} className="ml-auto text-xs text-purple-600">{busy ? '刷新中…' : '刷新'}</button></div><main className="min-h-0 flex-1 overflow-y-auto p-5">{data[tab].map(record => <article key={record.id} className="mb-3 rounded-xl border border-gray-100 bg-white p-4 shadow-sm"><div className="mb-2 flex gap-2 text-[11px] text-gray-400"><span>{record.category}</span><span>{record.domain}</span><span>权重 {Math.round(record.weight)}</span><span className="ml-auto">{record.updated.slice(0, 16).replace('T', ' ')}</span></div><p className="text-sm leading-6 text-gray-700">{record.title || record.claim}</p><div className="mt-3 flex gap-3 text-[11px] text-gray-400"><span>Trace {record.trace_id?.slice(0, 14) || '-'}</span><span>事件 {record.source_event_ids?.length || 0}</span>{record.support_count && <span>支持 {record.support_count}</span>}{tab !== 'evidence' && <button onClick={() => void downgrade(record)} className="ml-auto text-amber-700 hover:text-amber-900">降低层级</button>}</div></article>)}{!busy && data[tab].length === 0 && <p className="py-16 text-center text-sm text-gray-400">暂无 {label[tab]}。</p>}</main></div></div>, document.body)}
  </>;
}
