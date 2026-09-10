import { useCallback, useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { fetchMemoryPatterns, fetchTraceMemories, fetchTraceMemory } from '../api/client';
import type { MemoryPattern, PatternStatus, TraceMemory } from '../types/insights';

type InsightTab = 'patterns' | 'memories';

const statusMeta: Record<PatternStatus, { label: string; className: string }> = {
  confirmed: { label: '已确认', className: 'bg-emerald-100 text-emerald-700' },
  tentative: { label: '待观察', className: 'bg-amber-100 text-amber-700' },
  ready_for_review: { label: '待审核', className: 'bg-blue-100 text-blue-700' },
  disputed: { label: '有冲突', className: 'bg-rose-100 text-rose-700' },
};

const kindLabel: Record<string, string> = {
  preference: '用户偏好',
  issue: '问题规律',
  project_fact: '项目记忆',
};

function errorText(error: unknown) {
  return error instanceof Error ? error.message : '加载项目洞察失败';
}

function formatDate(value: string) {
  return value ? value.replace('T', ' ').slice(0, 16) : '';
}

interface ProjectInsightsPanelProps {
  projectId: string | null;
}

export function ProjectInsightsPanel({ projectId }: ProjectInsightsPanelProps) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<InsightTab>('patterns');
  const [patterns, setPatterns] = useState<MemoryPattern[]>([]);
  const [memories, setMemories] = useState<TraceMemory[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedMemoryId, setExpandedMemoryId] = useState<string | null>(null);
  const [memoryContents, setMemoryContents] = useState<Record<string, string>>({});
  const [contentLoadingId, setContentLoadingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    setError(null);
    try {
      const [nextPatterns, nextMemories] = await Promise.all([
        fetchMemoryPatterns(projectId),
        fetchTraceMemories(projectId),
      ]);
      setPatterns(nextPatterns);
      setMemories(nextMemories);
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  const showPanel = () => {
    if (!projectId) return;
    setOpen(true);
    setTab('patterns');
  };

  useEffect(() => {
    if (open) void load();
  }, [load, open]);

  const toggleMemory = async (memory: TraceMemory) => {
    if (expandedMemoryId === memory.memory_id) {
      setExpandedMemoryId(null);
      return;
    }
    setExpandedMemoryId(memory.memory_id);
    if (memoryContents[memory.memory_id] !== undefined || !projectId) return;
    setContentLoadingId(memory.memory_id);
    try {
      const detail = await fetchTraceMemory(projectId, memory.memory_id);
      setMemoryContents(current => ({ ...current, [memory.memory_id]: detail.content ?? memory.claim }));
    } catch (requestError) {
      setMemoryContents(current => ({ ...current, [memory.memory_id]: `⚠️ ${errorText(requestError)}` }));
    } finally {
      setContentLoadingId(null);
    }
  };

  const confirmedCount = useMemo(() => patterns.filter(pattern => pattern.status === 'confirmed').length, [patterns]);

  return (
    <>
      <button
        type="button"
        onClick={showPanel}
        disabled={!projectId}
        className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 shadow-sm hover:border-purple-200 hover:text-purple-600 transition-colors disabled:cursor-not-allowed disabled:opacity-40"
        aria-label="打开项目洞察与记忆"
      >项目洞察</button>
      {open && createPortal(
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="project-insights-title">
          <div className="relative w-full max-w-3xl max-h-[85vh] overflow-hidden rounded-2xl bg-white shadow-2xl mx-4 flex flex-col">
            <div className="px-6 pt-6 pb-4 pr-14 border-b border-gray-100 shrink-0">
              <div>
                <h2 id="project-insights-title" className="font-semibold text-gray-800">项目洞察与记忆</h2>
                <p className="mt-1 text-xs text-gray-400">Trace 保留证据；Memory 记录单次结论；规律由多条 Memory 归纳而来。</p>
              </div>
            </div>
            <button type="button" onClick={() => setOpen(false)} aria-label="关闭项目洞察" className="absolute right-4 top-4 z-10 flex h-8 w-8 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-700">✕</button>

            <div className="px-6 pt-4 shrink-0 flex items-center justify-between gap-3">
              <div className="flex gap-1 rounded-xl bg-gray-100 p-1" role="tablist" aria-label="项目洞察分类">
                <button type="button" role="tab" aria-selected={tab === 'patterns'} onClick={() => setTab('patterns')} className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${tab === 'patterns' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>学习规律 {confirmedCount ? `(${confirmedCount})` : ''}</button>
                <button type="button" role="tab" aria-selected={tab === 'memories'} onClick={() => setTab('memories')} className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${tab === 'memories' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>原子记忆 {memories.length ? `(${memories.length})` : ''}</button>
              </div>
              <button type="button" onClick={() => void load()} disabled={loading} className="text-xs text-purple-600 hover:text-purple-700 disabled:opacity-40">{loading ? '刷新中…' : '刷新'}</button>
            </div>

            <div className="flex-1 overflow-y-auto px-6 py-4">
              {loading && <div className="py-14 text-center text-sm text-gray-400">正在读取项目洞察…</div>}
              {!loading && error && <div role="alert" className="rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</div>}

              {!loading && !error && tab === 'patterns' && (
                <div className="space-y-3" role="tabpanel">
                  {patterns.map(pattern => {
                    const meta = statusMeta[pattern.status] ?? statusMeta.tentative;
                    return (
                      <article key={pattern.pattern_id} className="rounded-xl border border-gray-100 bg-gray-50 p-4">
                        <div className="flex flex-wrap items-center gap-2 mb-2">
                          <span className={`rounded-full px-2 py-0.5 text-[11px] ${meta.className}`}>{meta.label}</span>
                          <span className="text-[11px] text-gray-500">{kindLabel[pattern.kind] ?? pattern.kind}</span>
                          {pattern.subtype && <span className="text-[11px] text-gray-400">{pattern.subtype}</span>}
                          <span className="ml-auto text-[11px] text-gray-400">置信度 {Math.round(pattern.confidence * 100)}%</span>
                        </div>
                        <p className="text-sm leading-6 text-gray-700">{pattern.canonical_claim}</p>
                        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-400">
                          <span>支持 {pattern.support_count} 条</span>
                          <span>冲突 {pattern.contradiction_count} 条</span>
                          <span>范围：{pattern.scope}</span>
                          <span>更新于 {formatDate(pattern.updated_at)}</span>
                        </div>
                      </article>
                    );
                  })}
                  {patterns.length === 0 && <EmptyState text="还没有从多次交互中归纳出规律。完成几轮带有反馈的对话后，系统会在这里显示待观察或已确认的偏好与问题。" />}
                </div>
              )}

              {!loading && !error && tab === 'memories' && (
                <div className="space-y-3" role="tabpanel">
                  {memories.map(memory => (
                    <article key={memory.memory_id} className="rounded-xl border border-gray-100 bg-white p-4 shadow-sm">
                      <div className="flex flex-wrap items-center gap-2 mb-2 text-[11px]">
                        <span className="rounded-full bg-violet-100 text-violet-700 px-2 py-0.5">{kindLabel[memory.kind] ?? memory.kind}</span>
                        {memory.subtype && <span className="text-gray-500">{memory.subtype}</span>}
                        <span className="ml-auto text-gray-400">置信度 {Math.round(memory.confidence * 100)}%</span>
                      </div>
                      <button type="button" onClick={() => void toggleMemory(memory)} className="w-full text-left text-sm leading-6 text-gray-700 hover:text-purple-700">
                        {memory.claim}
                      </button>
                      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-400">
                        <span>范围：{memory.scope}</span>
                        <span title={memory.trace_id}>Trace：{memory.trace_id.slice(0, 14)}…</span>
                        <span>事件 {memory.source_event_ids.length} 个</span>
                        <span>{formatDate(memory.created_at)}</span>
                      </div>
                      {expandedMemoryId === memory.memory_id && (
                        <div className="mt-3 border-t border-gray-100 pt-3">
                          {contentLoadingId === memory.memory_id
                            ? <div className="text-xs text-gray-400">正在读取记忆正文…</div>
                            : <pre className="whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-3 text-xs leading-5 text-gray-600 font-sans">{memoryContents[memory.memory_id] ?? memory.claim}</pre>}
                          {memory.file_path && <div className="mt-2 text-[11px] text-gray-400">{memory.file_path}</div>}
                        </div>
                      )}
                    </article>
                  ))}
                  {memories.length === 0 && <EmptyState text="还没有提取到可长期复用的原子记忆。普通聊天与一次性指令不会被保存。" />}
                </div>
              )}
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="rounded-xl border border-dashed border-gray-200 bg-gray-50 px-5 py-12 text-center text-sm leading-6 text-gray-400">{text}</div>;
}
