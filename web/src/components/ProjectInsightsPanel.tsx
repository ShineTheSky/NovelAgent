import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { fetchEvidence, fetchMemories, fetchMemoryPatterns, reviewMemoryPattern } from '../api/client';
import type { Evidence, MemoryPattern, TraceMemory } from '../types/insights';

type InsightTab = 'evidence' | 'memory' | 'pattern';

const kindLabel: Record<string, string> = { preference: '用户偏好', issue: '问题记录', project_fact: '项目记忆' };
const patternStatus: Record<string, string> = { tentative: '待观察', ready_for_review: '待审核', confirmed: '已确认', disputed: '有冲突' };

function errorText(error: unknown) { return error instanceof Error ? error.message : '加载项目洞察失败'; }
function formatDate(value: string) { return value ? value.replace('T', ' ').slice(0, 16) : ''; }

interface ProjectInsightsPanelProps { projectId: string | null; }

export function ProjectInsightsPanel({ projectId }: ProjectInsightsPanelProps) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<InsightTab>('evidence');
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [memories, setMemories] = useState<TraceMemory[]>([]);
  const [patterns, setPatterns] = useState<MemoryPattern[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true); setError(null);
    try {
      const [nextEvidence, nextMemories, nextPatterns] = await Promise.all([fetchEvidence(projectId), fetchMemories(projectId), fetchMemoryPatterns(projectId)]);
      setEvidence(nextEvidence); setMemories(nextMemories); setPatterns(nextPatterns);
    } catch (requestError) { setError(errorText(requestError)); }
    finally { setLoading(false); }
  }, [projectId]);

  useEffect(() => { if (open) void load(); }, [load, open]);

  const review = async (pattern: MemoryPattern, status: 'confirmed' | 'disputed') => {
    if (!projectId) return;
    try { await reviewMemoryPattern(projectId, pattern.pattern_id, status, pattern.confidence); await load(); }
    catch (reviewError) { setError(errorText(reviewError)); }
  };

  return <>
    <button type="button" onClick={() => { if (projectId) setOpen(true); }} disabled={!projectId} className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 shadow-sm transition-colors hover:border-purple-200 hover:text-purple-600 disabled:cursor-not-allowed disabled:opacity-40">项目洞察</button>
    {open && createPortal(
      <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="project-insights-title">
        <div className="relative mx-4 flex max-h-[85vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">
          <div className="shrink-0 border-b border-gray-100 px-6 pb-4 pt-6 pr-14"><h2 id="project-insights-title" className="font-semibold text-gray-800">项目洞察</h2><p className="mt-1 text-xs text-gray-400">Trace 保存原始事实；Evidence 等待支持，Memory 可复用，Pattern 经审核后注入上下文。</p></div>
          <button type="button" onClick={() => setOpen(false)} aria-label="关闭项目洞察" className="absolute right-4 top-4 z-10 flex h-8 w-8 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-700">×</button>
          <div className="flex shrink-0 items-center justify-between gap-3 px-6 pt-4"><div className="flex gap-1 rounded-xl bg-gray-100 p-1"><Tab active={tab === 'evidence'} onClick={() => setTab('evidence')}>证据 {evidence.length ? `(${evidence.length})` : ''}</Tab><Tab active={tab === 'memory'} onClick={() => setTab('memory')}>记忆 {memories.length ? `(${memories.length})` : ''}</Tab><Tab active={tab === 'pattern'} onClick={() => setTab('pattern')}>模式 {patterns.length ? `(${patterns.length})` : ''}</Tab></div><button type="button" onClick={() => void load()} disabled={loading} className="text-xs text-purple-600 hover:text-purple-700 disabled:opacity-40">{loading ? '刷新中…' : '刷新'}</button></div>
          <div className="flex-1 overflow-y-auto px-6 py-4">
            {loading && <Empty text="正在读取项目洞察…" />}{!loading && error && <div role="alert" className="rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</div>}
            {!loading && !error && tab === 'evidence' && <RecordList records={evidence} empty="没有待观察证据。只有尚不足以形成长期结论的 Trace 线索会保留在这里。" badge="待观察" />}
            {!loading && !error && tab === 'memory' && <RecordList records={memories} empty="没有已提升 Memory。" badge="已提升" />}
            {!loading && !error && tab === 'pattern' && <div className="space-y-3">{patterns.map(pattern => <article key={pattern.pattern_id} className="rounded-xl border border-gray-100 bg-white p-4 shadow-sm"><div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]"><span className="rounded-full bg-purple-100 px-2 py-0.5 text-purple-700">{patternStatus[pattern.status] ?? pattern.status}</span><span className="text-gray-500">{kindLabel[pattern.kind] ?? pattern.kind}</span><span className="ml-auto text-gray-400">置信度 {Math.round(pattern.confidence * 100)}%</span></div><p className="text-sm leading-6 text-gray-700">{pattern.claim}</p><div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-400"><span>支持 {pattern.support_count} 条</span><span>冲突 {pattern.contradiction_count} 条</span><span>Trace {pattern.trace_ids.length} 条</span></div>{pattern.status === 'ready_for_review' && <div className="mt-3 flex gap-2"><button type="button" onClick={() => void review(pattern, 'confirmed')} className="rounded bg-emerald-600 px-2 py-1 text-xs text-white hover:bg-emerald-700">确认</button><button type="button" onClick={() => void review(pattern, 'disputed')} className="rounded border border-rose-200 px-2 py-1 text-xs text-rose-600 hover:bg-rose-50">标记冲突</button></div>}</article>)}{patterns.length === 0 && <Empty text="尚未形成 Pattern。至少需要多条已提升 Memory，并经 Trace 回读校验。" />}</div>}
          </div>
        </div>
      </div>, document.body,
    )}
  </>;
}

function Tab({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) { return <button type="button" onClick={onClick} className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${active ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>{children}</button>; }
function RecordList({ records, empty, badge }: { records: Array<Evidence | TraceMemory>; empty: string; badge: string }) { return <div className="space-y-3">{records.map(record => <article key={'evidence_id' in record ? record.evidence_id : record.memory_id} className="rounded-xl border border-gray-100 bg-white p-4 shadow-sm"><div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]"><span className="rounded-full bg-violet-100 px-2 py-0.5 text-violet-700">{badge}</span><span className="text-gray-500">{kindLabel[record.kind] ?? record.kind}</span><span className="ml-auto text-gray-400">置信度 {Math.round(record.confidence * 100)}%</span></div><p className="text-sm leading-6 text-gray-700">{record.claim}</p><div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-400"><span>范围：{record.scope}</span><span title={record.trace_id}>Trace：{record.trace_id.slice(0, 14)}…</span><span>事件 {record.source_event_ids.length} 个</span><span>{formatDate(record.created_at)}</span></div></article>)}{records.length === 0 && <Empty text={empty} />}</div>; }
function Empty({ text }: { text: string }) { return <div className="rounded-xl border border-dashed border-gray-200 bg-gray-50 px-5 py-12 text-center text-sm leading-6 text-gray-400">{text}</div>; }
